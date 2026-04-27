"""SC Ripper — web app."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
load_dotenv()  # MUST come before auth is imported (reads env)

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import auth
from ripper import check_ffmpeg, parse_timestamp, run_rip
from soundcloud import upload_track

log = logging.getLogger(__name__)

OUT_DIR = Path.cwd() / "trimmed-rips"
CLEANUP_INTERVAL_SEC = 5 * 60
FILE_MAX_AGE_SEC     = 30 * 60

STATIC_DIR = Path(__file__).parent / "static"
_INDEX_HTML = (STATIC_DIR / "index.html").read_text()


async def _cleanup_loop() -> None:
    while True:
        try:
            cutoff = time.time() - FILE_MAX_AGE_SEC
            for f in OUT_DIR.glob("*.mp3"):
                if f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
            for job_id, job in list(jobs.items()):
                path = job.get("path")
                if path is None or not Path(path).exists():
                    jobs.pop(job_id, None)
        except Exception as exc:
            print(f"cleanup error: {exc}")
        await asyncio.sleep(CLEANUP_INTERVAL_SEC)


@asynccontextmanager
async def lifespan(app: FastAPI):
    check_ffmpeg()
    OUT_DIR.mkdir(exist_ok=True, parents=True)
    cleanup_task = asyncio.create_task(_cleanup_loop())
    yield
    cleanup_task.cancel()


app = FastAPI(title="SC Ripper", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")

# In-memory job store. Single-process dev — a plain dict is fine.
jobs: dict[str, dict] = {}


class RipRequest(BaseModel):
    url: str
    start: str = ""
    end: str = ""




# ---------- page ----------

@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _INDEX_HTML


# ---------- auth ----------

@app.get("/me")
def me(request: Request, response: Response) -> dict:
    sid = auth.get_or_create_session_id(request, response)
    sess = auth.get_session(sid)
    if not sess:
        return {"connected": False}
    return {
        "connected":  True,
        "username":   sess["sc_username"],
        "avatar_url": sess["sc_avatar_url"],
    }


@app.get("/connect")
def connect(request: Request, response: Response) -> RedirectResponse:
    sid = auth.get_or_create_session_id(request, response)
    url = auth.build_authorize_url(sid)
    # Propagate any Set-Cookie headers from `response` into the redirect.
    redirect = RedirectResponse(url)
    for k, v in response.headers.items():
        if k.lower() == "set-cookie":
            redirect.headers.append(k, v)
    return redirect


@app.get("/callback")
async def callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
):
    if error:
        return HTMLResponse(
            f"<h1>OAuth error</h1><p>{error}</p><p><a href='/'>back</a></p>",
            status_code=400,
        )
    if not code or not state:
        return HTMLResponse("<h1>missing code or state</h1>", status_code=400)
    try:
        result = await auth.exchange_code(code, state)
    except Exception:
        log.exception("OAuth callback failed")
        return HTMLResponse(
            "<h1>auth failed</h1><p>Check server logs.</p><p><a href='/'>back</a></p>",
            status_code=500,
        )
    auth.create_session(result["session_id"], result)
    return RedirectResponse("/")


@app.post("/logout")
def logout(request: Request, response: Response) -> dict:
    sid = auth.get_or_create_session_id(request, response)
    auth.delete_session(sid)
    return {"ok": True}


# ---------- rip ----------

@app.post("/rip")
def create_rip(req: RipRequest, bg: BackgroundTasks) -> dict:
    start = 0
    end: int | None = None
    try:
        if req.start.strip():
            start = parse_timestamp(req.start)
        if req.end.strip():
            end = parse_timestamp(req.end)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if end is not None and end <= start:
        raise HTTPException(400, "end must be after start")

    url = req.url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(400, "url must start with http(s)")
    if "soundcloud.com" not in parsed.netloc:
        raise HTTPException(400, "url must be a soundcloud.com link")

    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {"status": "queued", "source_url": url}
    bg.add_task(_run_rip_job, job_id, url, start, end)
    return {"job_id": job_id}


def _run_rip_job(job_id: str, url: str, start: int, end: int | None) -> None:
    def on_phase(phase: str) -> None:
        jobs[job_id]["status"] = phase

    def on_progress(pct: int) -> None:
        jobs[job_id]["progress"] = pct

    try:
        result = run_rip(
            url, start, end, OUT_DIR,
            on_phase=on_phase, on_progress=on_progress,
        )
        jobs[job_id].update({
            "status":   "ready",
            "filename": result.path.name,
            "path":     str(result.path),
            "title":    result.title,
            "uploader": result.uploader,
            "warnings": result.warnings,
        })
    except Exception as e:
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["error"] = str(e)


@app.get("/status/{job_id}")
def status(job_id: str) -> dict:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    return job


@app.get("/download/{job_id}")
def download(job_id: str) -> FileResponse:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    if job.get("status") != "ready":
        raise HTTPException(409, "not ready")
    path = Path(job["path"])
    if not path.exists():
        raise HTTPException(410, "file gone")
    return FileResponse(path, media_type="audio/mpeg", filename=path.name)


# ---------- upload ----------

@app.post("/upload/{job_id}")
async def upload(
    job_id: str,
    request: Request,
    response: Response,
    bg: BackgroundTasks,
    title: str = Form(...),
    private: bool = Form(True),
    artwork: UploadFile | None = File(None),
) -> dict:
    sid = auth.get_or_create_session_id(request, response)
    if not auth.get_session(sid):
        raise HTTPException(401, "not connected to SoundCloud")

    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    if job.get("status") != "ready":
        raise HTTPException(400, "clip not ready")
    if job.get("upload_status") in ("uploading", "uploaded"):
        raise HTTPException(409, f"already {job['upload_status']}")

    artwork_data: bytes | None = None
    artwork_filename: str = "artwork.jpg"
    if artwork and artwork.filename:
        artwork_data = await artwork.read()
        artwork_filename = artwork.filename

    job["upload_status"] = "uploading"
    job.pop("upload_error", None)
    bg.add_task(_run_upload_job, job_id, sid, title, private, artwork_data, artwork_filename)
    return {"ok": True}


async def _run_upload_job(
    job_id: str,
    session_id: str,
    title: str,
    private: bool,
    artwork_data: bytes | None,
    artwork_filename: str,
) -> None:
    job = jobs[job_id]
    try:
        access_token = await auth.ensure_access_token(session_id)
        description = (
            f"Ripped from \"{job['title']}\" by {job['uploader']}\n\n{job['source_url']}\n\nvia SC Ripper"
        )
        track = await upload_track(
            access_token,
            Path(job["path"]),
            title=title,
            description=description,
            private=private,
            artwork_data=artwork_data,
            artwork_filename=artwork_filename,
        )
        job["upload_status"] = "uploaded"
        job["upload_url"]    = track.get("permalink_url") or track.get("uri", "")
    except Exception as e:
        job["upload_status"] = "failed"
        job["upload_error"]  = str(e)
