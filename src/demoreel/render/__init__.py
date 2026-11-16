"""Renderer registry."""

from __future__ import annotations

from functools import cache

from .base import (
    Availability,
    Clip,
    ClipSegment,
    Renderer,
    RenderError,
    RenderJob,
    RenderOptions,
    RenderResult,
)

__all__ = ["Availability", "Clip", "ClipSegment", "RenderError", "RenderJob", "RenderOptions", "RenderResult",
           "Renderer", "get_renderer", "renderer_names", "describe_renderers"]

# name -> worker capability required to run it
CAPABILITY = {"radar": "cpu", "ingame": "ingame"}


@cache
def get_renderer(name: str) -> Renderer:
    if name == "radar":
        from .radar import RadarRenderer

        return RadarRenderer()
    if name == "ingame":
        from .ingame import IngameRenderer

        return IngameRenderer()
    raise RenderError(f"Unknown renderer {name!r}. Available: {', '.join(renderer_names())}")


def renderer_names() -> list[str]:
    return list(CAPABILITY)


def describe_renderers() -> list[dict]:
    out = []
    for name in renderer_names():
        try:
            r = get_renderer(name)
            av = r.availability()
            out.append({"name": name, "description": r.description, "available": av.ok, "reason": av.reason,
                        "details": av.details})
        except Exception as exc:  # noqa: BLE001 - a broken optional renderer must not break the API
            out.append({"name": name, "description": "", "available": False, "reason": str(exc), "details": {}})
    return out
