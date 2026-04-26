"""SoundCloud API client. Just what we need: upload a track."""
from __future__ import annotations

from pathlib import Path

import httpx

API_BASE = "https://api.soundcloud.com"


async def upload_track(
    access_token: str,
    file_path: Path,
    title: str,
    description: str = "",
    private: bool = True,
    artwork_data: bytes | None = None,
    artwork_filename: str = "artwork.jpg",
) -> dict:
    """POST /tracks. Returns the created track (includes permalink_url)."""
    audio_bytes = file_path.read_bytes()
    files: dict = {"track[asset_data]": (file_path.name, audio_bytes, "audio/mpeg")}
    if artwork_data:
        mime = "image/png" if artwork_filename.lower().endswith(".png") else "image/jpeg"
        files["track[artwork_data]"] = (artwork_filename, artwork_data, mime)
    data = {
        "track[title]":        title,
        "track[description]":  description,
        "track[sharing]":      "private" if private else "public",
        "track[downloadable]": "false",
    }
    # Long timeout — uploads can take a while for big clips.
    async with httpx.AsyncClient(timeout=600) as client:
        r = await client.post(
            f"{API_BASE}/tracks",
            headers={"Authorization": f"OAuth {access_token}"},
            data=data, files=files,
        )

    if r.status_code >= 400:
        raise RuntimeError(
            f"SoundCloud upload failed ({r.status_code}): {r.text[:500]}"
        )
    return r.json()
