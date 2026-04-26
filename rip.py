#!/usr/bin/env python3
"""SC Ripper — CLI.

Usage:
    python rip.py <url> <start> <end> [-o OUTPUT]

Timestamps accept HH:MM:SS, MM:SS, or plain seconds.
"""
import argparse
import shutil
import sys
from pathlib import Path

from ripper import check_ffmpeg, parse_timestamp, run_rip


PHASE_LABELS = {
    "downloading": "→ downloading",
    "cutting":     "→ cutting",
}


def main() -> None:
    ap = argparse.ArgumentParser(description="Rip a clip from a SoundCloud link.")
    ap.add_argument("url", help="SoundCloud URL")
    ap.add_argument("start", nargs="?", default="", help="start timestamp (HH:MM:SS / MM:SS / seconds)")
    ap.add_argument("end", nargs="?", default="", help="end timestamp (HH:MM:SS / MM:SS / seconds)")
    ap.add_argument("-o", "--output", help="output path (default: trimmed-rips/...)")
    args = ap.parse_args()

    try:
        check_ffmpeg()
        start = parse_timestamp(args.start) if args.start else 0
        end = parse_timestamp(args.end) if args.end else None
    except (ValueError, RuntimeError) as e:
        sys.exit(f"error: {e}")

    out_dir = Path.cwd() / "trimmed-rips"

    try:
        result = run_rip(
            args.url, start, end, out_dir,
            on_phase=lambda p: print(PHASE_LABELS.get(p, f"→ {p}")),
        )
    except Exception as e:
        sys.exit(f"error: {e}")

    for w in result.warnings:
        print(f"  warning: {w}")

    if args.output:
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(result.path), str(target))
        result.path = target

    print(f"✓ done: {result.path}")


if __name__ == "__main__":
    main()
