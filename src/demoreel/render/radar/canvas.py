"""Small drawing toolkit shared by the 2D replay's world and HUD code: colours, easing, slabs."""

from __future__ import annotations

import skia

from ..killfeed import Type, argb, blend, clean_name, shared_type

__all__ = ["SLANT", "Type", "argb", "blend", "clamp", "clean_name", "ease_in", "ease_out", "ease_out_back",
           "shared_type", "slab", "slab_path", "span"]

SLANT = 0.36  # the lean of the brand's slabs: same value as compose/hud.py and the bumpers


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


def span(t: float, start: float, dur: float) -> float:
    """Progress 0..1 of something that starts at ``start`` and lasts ``dur``."""
    return clamp((t - start) / dur) if dur > 0 else float(t >= start)


def ease_out(x: float, p: float = 3.0) -> float:
    return 1 - (1 - x) ** p


def ease_in(x: float, p: float = 2.4) -> float:
    return x ** p


def ease_out_back(x: float, s: float = 1.6) -> float:
    x -= 1
    return 1 + x * x * ((s + 1) * x + s)


def slab_path(x: float, y: float, w: float, h: float, *, left: bool = True, right: bool = True) -> skia.Path:
    """Parallelogram leaning like the brand's strokes. ``left`` / ``right`` = False keeps that end upright
    (for a slab that hangs on the edge of the frame)."""
    k = h * SLANT
    path = skia.Path()
    path.moveTo(x + (k if left else 0), y)
    path.lineTo(x + w + (k if right else 0), y)
    path.lineTo(x + w, y + h)
    path.lineTo(x, y + h)
    path.close()
    return path


def slab(canvas: skia.Canvas, paint: skia.Paint, x: float, y: float, w: float, h: float, color: int, *,
         left: bool = True, right: bool = True) -> None:
    paint.setColor(color)
    canvas.drawPath(slab_path(x, y, w, h, left=left, right=right), paint)
