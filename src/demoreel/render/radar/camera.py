"""The follow camera. Pure numpy: takes positions in radar pixels, returns one view per output frame.

A view is ``(cx, cy, k)``: the radar pixel shown at the centre of the layout's focus rectangle and the
scale in screen pixels per radar pixel. Because a demo is known in full, the camera is planned for a whole
segment before a frame is drawn: it frames the watched player together with whoever he is about to fight,
and the path is smoothed in both directions of time, so it eases without ever lagging behind.
"""

from __future__ import annotations

import numpy as np

RADAR = 1024.0


def fit_scale(focus_w: float, focus_h: float, fill: float = 0.96) -> float:
    """Scale at which the whole radar fits the focus rectangle."""
    return min(focus_w, focus_h) * fill / RADAR


def full_view(focus_w: float, focus_h: float) -> np.ndarray:
    return np.array([RADAR / 2, RADAR / 2, fit_scale(focus_w, focus_h)])


def frame_box(lo: np.ndarray, hi: np.ndarray, focus_w: float, focus_h: float, margin: float, k_min: float,
              k_max: float) -> np.ndarray:
    """View that shows the box lo..hi (radar px) with ``margin`` radar px of air around it."""
    span_x = max(1.0, float(hi[0] - lo[0]) + 2 * margin)
    span_y = max(1.0, float(hi[1] - lo[1]) + 2 * margin)
    k = min(focus_w / span_x, focus_h / span_y)
    k = max(k_min, min(k_max, k))
    return np.array([(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, k])


def _fill_nan(a: np.ndarray) -> np.ndarray:
    """Forward- then backward-fill NaN rows of an (n, 2) array. All-NaN stays NaN."""
    a = a.copy()
    ok = ~np.isnan(a[:, 0])
    if not ok.any():
        return a
    idx = np.where(ok, np.arange(len(a)), 0)
    np.maximum.accumulate(idx, out=idx)
    a = a[idx]
    first = int(np.argmax(ok))
    a[:first] = a[first]
    return a


def smooth(values: np.ndarray, sigma: float) -> np.ndarray:
    """Gaussian smoothing along axis 0 with the ends held (no drift towards zero at the borders)."""
    if sigma <= 0.3 or len(values) < 3:
        return values.astype(np.float64)
    radius = int(min(len(values) - 1, max(1, round(sigma * 3))))
    x = np.arange(-radius, radius + 1)
    kernel = np.exp(-0.5 * (x / sigma) ** 2)
    kernel /= kernel.sum()
    v = values.astype(np.float64)
    flat = v.reshape(len(v), -1)
    padded = np.pad(flat, ((radius, radius), (0, 0)), mode="edge")
    out = np.stack([np.convolve(padded[:, c], kernel, mode="valid") for c in range(flat.shape[1])], axis=1)
    return out.reshape(v.shape)


def follow_path(pov: np.ndarray, others: list[np.ndarray], focus_w: float, focus_h: float, *, k_max: float,
                margin: float, fps: int) -> np.ndarray:
    """One view per frame that keeps ``pov`` and every non-NaN point of ``others`` in the focus rectangle.

    ``pov`` is (n, 2) in radar px with NaN where the player is unknown (it then holds his last position);
    each entry of ``others`` is (n, 2) with NaN on the frames where that player is not part of the action."""
    n = len(pov)
    k_fit = fit_scale(focus_w, focus_h)
    k_max = max(k_fit, k_max)
    p = _fill_nan(pov)
    if n == 0 or np.isnan(p[:, 0]).all():
        return np.tile(full_view(focus_w, focus_h), (max(n, 1), 1))
    stack = np.stack([p, *others], axis=0) if others else p[None]
    lo = np.nanmin(stack, axis=0)
    hi = np.nanmax(stack, axis=0)
    span_x = np.maximum(1.0, hi[:, 0] - lo[:, 0] + 2 * margin)
    span_y = np.maximum(1.0, hi[:, 1] - lo[:, 1] + 2 * margin)
    k = np.clip(np.minimum(focus_w / span_x, focus_h / span_y), k_fit, k_max)
    centre = (lo + hi) / 2
    out = np.empty((n, 3))
    out[:, :2] = smooth(centre, 0.55 * fps)
    out[:, 2] = np.exp(smooth(np.log(k), 0.7 * fps))
    return out


def ease_in_out(x: float) -> float:
    x = 0.0 if x < 0 else 1.0 if x > 1 else x
    return x * x * x * (x * (6 * x - 15) + 10)


def blend_views(a: np.ndarray, b: np.ndarray, t: float) -> np.ndarray:
    """View ``t`` of the way from a to b: position moves linearly, scale geometrically."""
    return np.array([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] * (b[2] / a[2]) ** t])


def majority(flags: np.ndarray, radius: int) -> np.ndarray:
    """Boolean majority filter: removes flicker shorter than about ``radius`` frames."""
    if radius < 1 or len(flags) < 3:
        return flags.astype(bool)
    padded = np.pad(flags.astype(np.float64), radius, mode="edge")
    return np.convolve(padded, np.ones(2 * radius + 1) / (2 * radius + 1), mode="valid") > 0.5
