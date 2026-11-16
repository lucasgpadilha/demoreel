"""Turn a captured body video into the finished, branded video."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

from ..brand import Brand
from .assemble import Part, assemble, probe
from .bumper import ensure_bumper

_DISSOLVE_S = 0.22


def is_branded(brand: Brand) -> bool:
    """True when a real brand kit is in use (the built-in default identity adds no bumpers)."""
    return brand.dir.joinpath("brand.json").is_file()


def finish(body: Path, out: Path, brand: Brand, *, width: int, height: int, fps: int, crf: int = 20,
           body_duration: float | None = None, intro: bool = True, outro: bool = True, music: bool = True,
           progress: Callable[[float, str], None] = lambda fraction, message: None) -> tuple[float, dict]:
    """Wrap ``body`` with the brand's intro / outro and music. Returns (duration_s, details)."""
    details: dict = {"brand": brand.slug, "intro": False, "outro": False, "music": False, "body_offset_s": 0.0}
    branded = is_branded(brand)
    parts: list[Part] = []
    if branded and intro:
        progress(0.90, "Preparing intro")
        p = ensure_bumper(brand, "intro", width, height, fps)
        if p is not None:
            parts.append(Part(p))
            details["intro"] = True
            # where the play starts inside the finished video: the intro minus the dissolve that overlaps it
            details["body_offset_s"] = round(max(0.0, probe(p).duration - _DISSOLVE_S), 3)
    # The intro ends on a bright flash and the outro starts on one, so both cuts dissolve through white.
    parts.append(Part(body, transition="fadewhite" if parts else "cut", transition_s=_DISSOLVE_S))
    if branded and outro:
        progress(0.93, "Preparing outro")
        p = ensure_bumper(brand, "outro", width, height, fps)
        if p is not None:
            parts.append(Part(p, transition="fadewhite", transition_s=_DISSOLVE_S))
            details["outro"] = True
    track = brand.music_path if (branded and music) else None
    details["music"] = track is not None
    if len(parts) == 1 and track is None:
        if body.resolve() != out.resolve():
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(body), str(out))
        return (body_duration if body_duration is not None else probe(out).duration), details
    progress(0.95, "Assembling final video")
    duration = assemble(parts, out, width=width, height=height, fps=fps, crf=crf, music=track)
    return duration, details
