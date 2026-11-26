"""Renderer contract.

A *renderer* turns clips (lists of demo tick ranges + POV) into one mp4 file. Two implementations ship:

* ``radar``  – 2D tactical replay drawn from demo data. Pure Python + ffmpeg, runs anywhere (no game, no GPU).
* ``ingame`` – drives the real CS2 client to record first-person footage. Needs CS2 + a GPU + a desktop session.

New renderers only need to implement :class:`Renderer` and register themselves in ``render/__init__.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..analysis.schema import MatchAnalysis


class RenderError(RuntimeError):
    """Raised when a render cannot be produced. The message is shown to API users."""


class RenderCancelled(RenderError):
    pass


class RecorderBusy(RenderError):
    """This machine's recorder is taken by another process. Nothing is wrong with the render itself: it can
    run when the recorder is free (a queued job goes back to the queue instead of failing)."""


@dataclass
class ClipSegment:
    start_tick: int
    end_tick: int
    # Who or where the segment is watched from: a player's SteamID, a fixed camera (``camera_view``) or None.
    pov_steamid: str | None = None
    speed: float = 1.0
    caption: str | None = None
    role: str = "play"  # "replay": the same moment from another point of view (HUD shows only the caption)


# A fixed camera travels through the pipeline as a point of view of its own, so stored footage, capture planning
# and composition tell it apart from every player and from other cameras without a field of their own.
CAMERA_VIEW_PREFIX = "cam:"


def camera_view(camera: Any) -> str | None:
    """``cam:x,y,z,pitch,yaw`` for a camera given as a mapping or an object with those attributes."""
    if camera is None:
        return None
    get = camera.get if isinstance(camera, dict) else lambda k: getattr(camera, k)
    return CAMERA_VIEW_PREFIX + ",".join(f"{float(get(k)):.2f}" for k in ("x", "y", "z", "pitch", "yaw"))


def parse_camera_view(view: str | None) -> tuple[float, float, float, float, float] | None:
    """The five numbers of a ``cam:`` point of view, or None for a player / no point of view."""
    if not view or not view.startswith(CAMERA_VIEW_PREFIX):
        return None
    x, y, z, pitch, yaw = (float(v) for v in view[len(CAMERA_VIEW_PREFIX):].split(","))
    return x, y, z, pitch, yaw


def player_view(view: str | None) -> str | None:
    """The SteamID of a player point of view; None for a fixed camera or the free camera."""
    return None if not view or view.startswith(CAMERA_VIEW_PREFIX) else view


def is_camera_view(view: str | None) -> bool:
    """True for a fixed camera point of view (``cam:…``)."""
    return bool(view) and view.startswith(CAMERA_VIEW_PREFIX)


def segment_view(seg: Any) -> str | None:
    """Point of view of a stored segment (dict) or a ``HighlightSegment``: its player, else its fixed camera."""
    get = seg.get if isinstance(seg, dict) else lambda k: getattr(seg, k, None)
    return get("pov_steamid") or camera_view(get("camera"))


@dataclass
class Clip:
    """One highlight: an ordered list of segments shown back to back."""

    segments: list[ClipSegment]
    title: str | None = None
    subtitle: str | None = None
    highlight_id: str | None = None
    # Ticks at which the player shot, hit or was hit: composition speeds up the quiet stretches between them.
    action_ticks: list[int] = field(default_factory=list)


@dataclass
class RenderOptions:
    width: int = 1280
    height: int = 720
    fps: int = 30
    crf: int = 20  # x264 quality, lower = better
    title_cards: bool = True  # short title card before each clip in a reel
    extra: dict[str, Any] = field(default_factory=dict)  # renderer specific knobs


@dataclass
class RenderJob:
    demo_path: Path
    analysis: MatchAnalysis
    clips: list[Clip]
    out_path: Path  # final .mp4
    work_dir: Path  # scratch directory owned by this job; safe to fill and delete
    options: RenderOptions = field(default_factory=RenderOptions)
    progress: Callable[[float, str], None] = lambda fraction, message: None
    cancelled: Callable[[], bool] = lambda: False
    thumbnail_path: Path | None = None  # if set, the renderer should write a .jpg poster frame here


@dataclass
class RenderResult:
    duration_s: float
    width: int
    height: int
    fps: int
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class Availability:
    ok: bool
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)


class Renderer(Protocol):
    name: str
    description: str

    def availability(self) -> Availability:
        """Can this renderer run on this machine right now? Must be cheap and never raise."""
        ...

    def render(self, job: RenderJob) -> RenderResult:
        """Produce ``job.out_path``. Raise :class:`RenderError` with a helpful message on failure."""
        ...
