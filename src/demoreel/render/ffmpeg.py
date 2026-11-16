"""ffmpeg discovery and small helpers shared by all renderers.

ffmpeg is looked up in this order: ``DEMOREEL_FFMPEG`` env var, ``ffmpeg`` on PATH, then the static binary
bundled with the ``imageio-ffmpeg`` wheel (so a plain ``pip install`` works on Windows and Linux).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

from .base import RenderError

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@lru_cache(maxsize=1)
def ffmpeg_path() -> str:
    env = os.environ.get("DEMOREEL_FFMPEG")
    if env and Path(env).exists():
        return env
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # pragma: no cover - only without the wheel and without ffmpeg
        raise RenderError("ffmpeg not found. Install ffmpeg or `pip install imageio-ffmpeg`.") from exc


def run(args: list[str], what: str = "ffmpeg") -> None:
    proc = subprocess.run([ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y", *args],
                          capture_output=True, text=True, creationflags=_NO_WINDOW)
    if proc.returncode != 0:
        raise RenderError(f"{what} failed: {proc.stderr.strip()[-800:]}")


def open_rgb_writer(out_path: Path, width: int, height: int, fps: int, crf: int = 20,
                    pix_fmt: str = "rgb24") -> subprocess.Popen:
    """Start ffmpeg reading raw frames (``pix_fmt``) from stdin and writing a web-friendly H.264 mp4."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
           "-f", "rawvideo", "-pix_fmt", pix_fmt, "-s", f"{width}x{height}", "-r", str(fps), "-i", "-",
           "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p",
           "-movflags", "+faststart", str(out_path)]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=_NO_WINDOW)


def close_writer(proc: subprocess.Popen, what: str = "video encode") -> None:
    assert proc.stdin is not None
    try:
        proc.stdin.close()
    except OSError:
        pass
    err = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
    if proc.wait() != 0:
        raise RenderError(f"{what} failed: {err.strip()[-800:]}")


def concat(parts: list[Path], out_path: Path, reencode: bool = False) -> None:
    """Join mp4 files that share codec/size/fps. ``reencode=True`` is the slow but always-safe path."""
    if not parts:
        raise RenderError("Nothing to concatenate.")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if len(parts) == 1 and not reencode:
        shutil.copyfile(parts[0], out_path)
        return
    listing = out_path.with_suffix(".concat.txt")
    listing.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in parts), encoding="utf-8")
    try:
        codec = (["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac"]
                 if reencode else ["-c", "copy"])
        run(["-f", "concat", "-safe", "0", "-i", str(listing), *codec, "-movflags", "+faststart", str(out_path)],
            "video concat")
    finally:
        listing.unlink(missing_ok=True)


def thumbnail(video: Path, out_path: Path, at_s: float = 1.0, width: int = 640) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    run(["-ss", f"{max(0.0, at_s):.2f}", "-i", str(video), "-frames:v", "1", "-vf", f"scale={width}:-2",
         "-q:v", "4", str(out_path)], "thumbnail")
