"""Which demo tick each output frame shows. Pure numpy.

A segment plays at ``speed``; on top of that every kill of the watched player gets a *hit-stop*: game time
freezes for a few frames at the impact and then catches up, so the kill lands like an event. The freeze is
paid back within about half a second: a segment keeps exactly the length ``ticks / tickrate / speed``, and
whoever maps video time to demo ticks (the site's kill-marked tape) stays right everywhere but in that
half second.
"""

from __future__ import annotations

import math

import numpy as np

HIT_STOP_S = 0.10  # how long the picture holds on a kill (output seconds)
CATCH_UP_S = 0.55  # how long it takes to be back on the linear clock
MIN_HIT_STOP_SPEED = 0.75  # slower segments are already slow motion: no freeze on top


def segment_frames(start_tick: int, end_tick: int, speed: float, tickrate: float, fps: int) -> int:
    speed = max(0.05, float(speed or 1.0))
    return max(1, int(math.ceil((end_tick - start_tick) / tickrate / speed * fps)))


def _smoothstep(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3 - 2 * x)


def frame_ticks(start_tick: int, end_tick: int, speed: float, tickrate: float, fps: int,
                impact_ticks: list[int] | tuple[int, ...] = ()) -> tuple[np.ndarray, np.ndarray]:
    """(shown, linear): per output frame, the tick that is drawn and the tick of the plain linear clock.

    ``shown`` never runs backwards, starts at ``start_tick`` and never passes ``end_tick``. The HUD (kill
    feed, counters) runs on ``linear`` so its animations keep moving while the map holds."""
    speed = max(0.05, float(speed or 1.0))
    n = segment_frames(start_tick, end_tick, speed, tickrate, fps)
    step = tickrate * speed / fps  # ticks per frame
    linear = np.minimum(start_tick + np.arange(n, dtype=np.float64) * step, float(end_tick))
    shown = linear.copy()
    if speed >= MIN_HIT_STOP_SPEED:
        t = np.arange(n, dtype=np.float64) / fps
        for k in impact_ticks:
            if not start_tick < k < end_tick:
                continue
            u = t - (k + 1 - start_tick) / (tickrate * speed)  # seconds since the frame that shows the kill
            lag = np.where(u < 0, 0.0, np.where(u < HIT_STOP_S, u,
                                                HIT_STOP_S * (1 - _smoothstep((u - HIT_STOP_S) / CATCH_UP_S))))
            shown -= lag * tickrate * speed
        shown = np.maximum.accumulate(np.clip(shown, float(start_tick), float(end_tick)))
    return shown, linear


def first_frame_at(ticks: np.ndarray, tick: float) -> int:
    """Index of the first frame that shows ``tick`` or later (the last frame if none does)."""
    return int(min(np.searchsorted(ticks, tick, side="left"), len(ticks) - 1))
