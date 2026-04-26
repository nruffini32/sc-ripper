"""Sessions + SoundCloud OAuth 2.1 (PKCE).

- Session: random uuid stored in a signed cookie, maps to a row in SQLite.
- Tokens: stored per session. Refreshed automatically when near expiry.
- Pending OAuth state: in-memory dict, short-lived.

For a personal tool this is fine as-is. If you ever make it public-facing,
encrypt the tokens at rest (cryptography.Fernet + a key from env).
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
import sqlite3
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode

import httpx
from itsdangerous import BadSignature, URLSafeSerializer

DB_PATH = Path(os.environ.get("DATA_DIR", Path(__file__).parent)) / "sessions.db"
AUTHORIZE_URL = "https://secure.soundcloud.com/authorize"
TOKEN_URL = "https://secure.soundcloud.com/oauth/token"
API_BASE = "https://api.soundcloud.com"

COOKIE_NAME = "sid"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days


# ---------- db ----------

def _init_db() -> None:
    with sqlite3.connect(DB_PATH) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id    TEXT PRIMARY KEY,
                sc_user_id    INTEGER,
                sc_username   TEXT,
                sc_avatar_url TEXT,
                access_token  TEXT,
                refresh_token TEXT,
                expires_at    INTEGER,
                created_at    INTEGER
            )
        """)


_init_db()


def get_session(session_id: str) -> dict | None:
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return dict(row) if row else None


def create_session(session_id: str, data: dict) -> None:
    with sqlite3.connect(DB_PATH) as c:
        c.execute("""
            INSERT INTO sessions (
                session_id, sc_user_id, sc_username, sc_avatar_url,
                access_token, refresh_token, expires_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                sc_user_id    = excluded.sc_user_id,
                sc_username   = excluded.sc_username,
                sc_avatar_url = excluded.sc_avatar_url,
                access_token  = excluded.access_token,
                refresh_token = excluded.refresh_token,
                expires_at    = excluded.expires_at
        """, (
            session_id,
            data["sc_user_id"], data["sc_username"], data.get("sc_avatar_url"),
            data["access_token"], data["refresh_token"], data["expires_at"],
            int(time.time()),
        ))


def _update_tokens(session_id: str, access: str, refresh: str, expires_at: int) -> None:
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            "UPDATE sessions SET access_token=?, refresh_token=?, expires_at=? "
            "WHERE session_id=?",
            (access, refresh, expires_at, session_id),
        )


def delete_session(session_id: str) -> None:
    with sqlite3.connect(DB_PATH) as c:
        c.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))


# ---------- cookie ----------

def _make_serializer() -> URLSafeSerializer:
    secret = os.environ.get("SESSION_SECRET")
    if not secret:
        raise RuntimeError("SESSION_SECRET env var required (see .env.example)")
    return URLSafeSerializer(secret, salt="session")

_serializer = _make_serializer()


def get_or_create_session_id(request, response) -> str:
    """Read the signed 'sid' cookie, or mint a new session id + set cookie."""
    raw = request.cookies.get(COOKIE_NAME)
    if raw:
        try:
            return _serializer.loads(raw)
        except BadSignature:
            pass
    sid = uuid.uuid4().hex
    response.set_cookie(
        COOKIE_NAME, _serializer.dumps(sid),
        httponly=True, samesite="lax", max_age=COOKIE_MAX_AGE,
    )
    return sid


# ---------- oauth ----------

# state -> {"verifier": ..., "session_id": ..., "created": ts}
_pending: dict[str, dict] = {}


def _pkce_pair() -> tuple[str, str]:
    """PKCE: server generates a verifier, sends SHA256 hash as the challenge."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _prune_pending() -> None:
    cutoff = time.time() - 600  # 10 minutes
    for s in list(_pending):
        if _pending[s]["created"] < cutoff:
            del _pending[s]


def build_authorize_url(session_id: str) -> str:
    _prune_pending()
    state = secrets.token_urlsafe(32)
    verifier, challenge = _pkce_pair()
    _pending[state] = {
        "verifier": verifier,
        "session_id": session_id,
        "created": time.time(),
    }
    params = {
        "client_id":              os.environ["SC_CLIENT_ID"],
        "redirect_uri":           os.environ["SC_REDIRECT_URI"],
        "response_type":          "code",
        "code_challenge":         challenge,
        "code_challenge_method":  "S256",
        "state":                  state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


async def exchange_code(code: str, state: str) -> dict:
    """Swap auth code for tokens, fetch user info, return combined blob."""
    pending = _pending.pop(state, None)
    if not pending:
        raise ValueError("invalid or expired state")

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(TOKEN_URL, data={
            "grant_type":    "authorization_code",
            "client_id":     os.environ["SC_CLIENT_ID"],
            "client_secret": os.environ["SC_CLIENT_SECRET"],
            "redirect_uri":  os.environ["SC_REDIRECT_URI"],
            "code_verifier": pending["verifier"],
            "code":          code,
        })
        if r.status_code != 200:
            raise RuntimeError(f"token exchange failed ({r.status_code}): {r.text}")
        tokens = r.json()

        me = await client.get(
            f"{API_BASE}/me",
            headers={"Authorization": f"OAuth {tokens['access_token']}"},
        )
        if me.status_code != 200:
            raise RuntimeError(f"fetching user failed ({me.status_code}): {me.text}")
        user = me.json()

    return {
        "session_id":    pending["session_id"],
        "sc_user_id":    user["id"],
        "sc_username":   user.get("username") or user.get("permalink") or "user",
        "sc_avatar_url": user.get("avatar_url"),
        "access_token":  tokens["access_token"],
        "refresh_token": tokens.get("refresh_token", ""),
        "expires_at":    int(time.time()) + int(tokens.get("expires_in", 3600)),
    }


async def ensure_access_token(session_id: str) -> str:
    """Return a valid access token, refreshing if necessary."""
    sess = get_session(session_id)
    if not sess:
        raise ValueError("not connected")

    # 60s safety margin
    if sess["expires_at"] > int(time.time()) + 60:
        return sess["access_token"]

    if not sess["refresh_token"]:
        raise ValueError("access token expired and no refresh token — reconnect")

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(TOKEN_URL, data={
            "grant_type":    "refresh_token",
            "client_id":     os.environ["SC_CLIENT_ID"],
            "client_secret": os.environ["SC_CLIENT_SECRET"],
            "refresh_token": sess["refresh_token"],
        })
        if r.status_code != 200:
            raise RuntimeError(f"refresh failed ({r.status_code}): {r.text}")
        tokens = r.json()

    new_access = tokens["access_token"]
    new_refresh = tokens.get("refresh_token", sess["refresh_token"])
    new_exp = int(time.time()) + int(tokens.get("expires_in", 3600))
    _update_tokens(session_id, new_access, new_refresh, new_exp)
    return new_access
