"""SC Ripper — shared pipeline.

Every real piece of logic lives here. rip.py (CLI) and main.py (web) both
call run_rip() and do nothing but wrap it in their respective UI.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yt_dlp


# ---------- timestamp helpers ----------

def parse_timestamp(s: str) -> int:
    """'1:23:45' -> 5025, '23:45' -> 1425, '45' -> 45."""
    parts = s.strip().split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        raise ValueError(f"not a timestamp: {s!r}")
    if len(nums) == 1:
        return nums[0]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    raise ValueError(f"not a timestamp: {s!r}")


def format_timestamp(seconds: int) -> str:
    """5025 -> '1h23m45s', 330 -> '5m30s'. Filename-safe."""
    if seconds >= 3600:
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        return f"{h}h{m:02d}m{s:02d}s"
    m, s = divmod(seconds, 60)
    return f"{m}m{s:02d}s"


def sanitize_filename(s: str) -> str:
    """Strip characters filesystems hate. Cap length."""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", s)
    s = s.strip(". ")
    return s[:180] or "clip"


# ---------- pipeline pieces ----------

def check_ffmpeg() -> None:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        raise RuntimeError(
            "ffmpeg not found on PATH "
            "(macOS: `brew install ffmpeg`, ubuntu: `apt install ffmpeg`)"
        )


def download_source(
    url: str,
    workdir: Path,
    on_progress: Callable[[int], None] | None = None,
) -> tuple[Path, dict]:
    """Pull bestaudio + thumbnail via yt-dlp. Returns (audio_path, info_dict)."""
    def _hook(d: dict) -> None:
        if on_progress and d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes", 0)
            if total:
                on_progress(int(downloaded / total * 100))

    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(workdir / "source.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "progress_hooks": [_hook],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        expected = Path(ydl.prepare_filename(info))

    if expected.exists():
        audio_path = expected
    else:
        audio_files = [
            p for p in workdir.glob("source.*")
            if p.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp")
        ]
        if not audio_files:
            raise FileNotFoundError("yt-dlp finished but no audio file landed on disk")
        audio_path = audio_files[0]

    return audio_path, info


def cut_clip(
    source: Path,
    start: int,
    end: int | None,
    out: Path,
    title: str,
    artist: str,
    comment: str,
) -> None:
    """Re-encode to MP3 320k. If start/end given, does a sample-accurate cut."""
    cmd = ["ffmpeg", "-y"]

    if start > 0 or end is not None:
        pre = max(start - 5, 0)
        fine = start - pre
        cmd += ["-ss", str(pre), "-i", str(source), "-ss", str(fine)]
        if end is not None:
            cmd += ["-t", str(end - start)]
    else:
        cmd += ["-i", str(source)]

    cmd += [
        "-c:a", "libmp3lame",
        "-b:a", "320k",
        "-metadata", f"title={title}",
        "-metadata", f"artist={artist}",
        "-metadata", f"comment={comment}",
        str(out),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{result.stderr.strip()}")


# ---------- orchestration ----------

@dataclass
class RipResult:
    path: Path
    title: str
    uploader: str
    warnings: list[str] = field(default_factory=list)


def run_rip(
    url: str,
    start: int,
    end: int | None,
    out_dir: Path,
    on_phase: Callable[[str], None] | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> RipResult:
    """Full pipeline. end=None means encode the full track.
    Calls on_phase('downloading' | 'cutting') before each stage,
    and on_progress(0-100) during download.
    Raises RuntimeError or ValueError on failure."""
    def phase(name: str) -> None:
        if on_phase:
            on_phase(name)

    if end is not None and end <= start:
        raise ValueError("end must be after start")

    warnings: list[str] = []

    with tempfile.TemporaryDirectory(prefix="scripper_") as tmp:
        workdir = Path(tmp)

        phase("downloading")
        source, info = download_source(url, workdir, on_progress=on_progress)

        title = info.get("title", "unknown")
        uploader = info.get("uploader", "unknown")
        track_duration = int(d) if (d := info.get("duration")) else None

        if end is not None and track_duration and end > track_duration:
            warnings.append(
                f"end ({end}s) was past track end ({track_duration}s); clamped."
            )
            end = track_duration
            if end <= start:
                raise ValueError("start is past the end of the track")

        out_dir.mkdir(exist_ok=True, parents=True)
        stem = sanitize_filename(title)

        if start == 0 and end is None:
            out = out_dir / f"{stem}.mp3"
            clip_title = title
        elif end is None:
            ts = format_timestamp(start)
            out = out_dir / f"{stem} [{ts}-end].mp3"
            clip_title = f"{title} [{ts}-end]"
        else:
            ts = f"{format_timestamp(start)}-{format_timestamp(end)}"
            out = out_dir / f"{stem} [{ts}].mp3"
            clip_title = f"{title} [{ts}]"

        comment = f"Ripped from {url} — all credit to {uploader} — via SC Ripper"

        phase("cutting")
        cut_clip(source, start, end, out, clip_title, uploader, comment)

    return RipResult(path=out, title=title, uploader=uploader, warnings=warnings)
