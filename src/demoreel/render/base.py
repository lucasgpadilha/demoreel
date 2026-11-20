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
    pov_steamid: str | None = None
    speed: float = 1.0
    caption: str | None = None
    role: str = "play"  # "replay": the same moment from another point of view (HUD shows only the caption)


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
