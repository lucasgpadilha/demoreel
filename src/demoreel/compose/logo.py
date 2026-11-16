"""Turn a flat logo image into separate pieces that can be animated independently.

Brand kits ship one PNG. To animate "the logo being assembled" without asking anyone for layered artwork,
the image is split automatically:

1. the background is keyed out (real alpha if the file has it, otherwise distance from the corner colour);
2. the remaining pixels are clustered into the logo's few flat colours;
3. each colour is split into connected shapes, and specks are folded into their nearest neighbour.

The result for a typical esports mark is a handful of meaningful pieces (letters, emblem, accents), each
with its own soft-edged alpha, bounding box and centroid.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image

MAX_COLORS = 4
MIN_PART_FRACTION = 0.004  # shapes smaller than this share of the logo are merged into a neighbour
MAX_PARTS = 9


@dataclass
class LogoPart:
    rgba: np.ndarray  # (h, w, 4) uint8, premultiplied=False, cropped to the part's bounding box
    x: int  # top-left of the crop inside the logo canvas
    y: int
    cx: float  # centroid inside the logo canvas
    cy: float
    area: int
    color: tuple[int, int, int]
    is_accent: bool = False  # belongs to a minority colour (used for the "ignite" beat)


@dataclass
class Logo:
    size: int  # logo canvas is size x size
    rgba: np.ndarray  # whole logo, background removed
    parts: list[LogoPart]
    bbox: tuple[int, int, int, int]  # x0, y0, x1, y1 of the visible mark
    main_color: tuple[int, int, int]
    accent_color: tuple[int, int, int] | None


def _hex(c: str) -> np.ndarray:
    c = c.lstrip("#")
    return np.array([int(c[i:i + 2], 16) for i in (0, 2, 4)], dtype=np.float32)


def _key_alpha(rgb: np.ndarray, alpha: np.ndarray, background: str | None) -> np.ndarray:
    if alpha.min() < 250:  # the file has real transparency
        return alpha.astype(np.float32) / 255.0
    if background is None:
        corners = np.concatenate([rgb[:6, :6].reshape(-1, 3), rgb[:6, -6:].reshape(-1, 3),
                                  rgb[-6:, :6].reshape(-1, 3), rgb[-6:, -6:].reshape(-1, 3)])
        bg = np.median(corners, axis=0)
    else:
        bg = _hex(background)
    # The corners may not match the declared colour exactly (JPEG-ish noise, slightly different black).
    corner_bg = np.median(rgb[:8, :8].reshape(-1, 3), axis=0)
    if np.linalg.norm(corner_bg - bg) < 40:
        bg = corner_bg
    dist = np.linalg.norm(rgb - bg, axis=2)
    lo, hi = 28.0, 96.0
    t = np.clip((dist - lo) / (hi - lo), 0.0, 1.0)
    return t * t * (3 - 2 * t)


def _cluster(colors: np.ndarray, k: int) -> np.ndarray:
    """Tiny k-means with farthest-point initialisation; returns the cluster centres."""
    centres = [colors.mean(axis=0)]
    for _ in range(k - 1):
        d = np.min([np.linalg.norm(colors - c, axis=1) for c in centres], axis=0)
        centres.append(colors[int(d.argmax())])
    c = np.array(centres, dtype=np.float32)
    for _ in range(12):
        lab = np.linalg.norm(colors[:, None, :] - c[None], axis=2).argmin(axis=1)
        for i in range(len(c)):
            if (lab == i).any():
                c[i] = colors[lab == i].mean(axis=0)
    merged: list[np.ndarray] = []
    for centre in c:  # flat logos have few colours: collapse near-duplicates
        if all(np.linalg.norm(centre - m) > 48 for m in merged):
            merged.append(centre)
    return np.array(merged, dtype=np.float32)


def _components(mask: np.ndarray) -> tuple[np.ndarray, int]:
    labels = np.zeros(mask.shape, dtype=np.int32)
    h, w = mask.shape
    n = 0
    for sy, sx in zip(*np.nonzero(mask), strict=False):
        if labels[sy, sx]:
            continue
        n += 1
        labels[sy, sx] = n
        q = deque([(int(sy), int(sx))])
        while q:
            y, x = q.popleft()
            for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not labels[ny, nx]:
                    labels[ny, nx] = n
                    q.append((ny, nx))
    return labels, n


def _grow(labels: np.ndarray, into: np.ndarray, rounds: int = 3) -> np.ndarray:
    """Spread labels into neighbouring unlabeled pixels of ``into`` (captures soft anti-aliased edges)."""
    out = labels.copy()
    for _ in range(rounds):
        for shift, axis in ((1, 0), (-1, 0), (1, 1), (-1, 1)):
            moved = np.roll(out, shift, axis=axis)
            take = (out == 0) & into & (moved > 0)
            out[take] = moved[take]
    return out


@lru_cache(maxsize=8)
def _load_cached(path: str, mtime: float, background: str | None, size: int) -> Logo:
    img = Image.open(path).convert("RGBA")
    if max(img.size) != size:
        img = img.resize((size, size), Image.LANCZOS)
    arr = np.asarray(img).astype(np.float32)
    rgb, a = arr[..., :3], arr[..., 3]
    alpha = _key_alpha(rgb, a, background)
    solid = alpha > 0.5
    if solid.sum() < 50:
        raise ValueError("The logo image is empty after removing its background.")

    sample = rgb[solid]
    if len(sample) > 20000:
        sample = sample[:: len(sample) // 20000]
    centres = _cluster(sample, MAX_COLORS)
    dist = np.linalg.norm(rgb[:, :, None, :] - centres[None, None], axis=3)
    color_of = dist.argmin(axis=2)
    counts = np.array([(solid & (color_of == i)).sum() for i in range(len(centres))])
    main = int(counts.argmax())

    total = int(solid.sum())
    raw: list[tuple[np.ndarray, int]] = []  # (boolean mask, colour index)
    for ci in range(len(centres)):
        labels, n = _components(solid & (color_of == ci))
        for i in range(1, n + 1):
            raw.append((labels == i, ci))
    raw.sort(key=lambda p: -int(p[0].sum()))
    big = [p for p in raw if p[0].sum() >= total * MIN_PART_FRACTION][:MAX_PARTS]
    small = [p for p in raw if not any(p[0] is b[0] for b in big)]
    masks = [m.copy() for m, _ in big]
    cents = [np.array(np.nonzero(m)).mean(axis=1) for m in masks]
    for m, _ci in small:  # fold specks into the nearest big piece
        c = np.array(np.nonzero(m)).mean(axis=1)
        j = int(np.argmin([np.linalg.norm(c - k) for k in cents]))
        masks[j] |= m

    labels = np.zeros(alpha.shape, dtype=np.int32)
    for i, m in enumerate(masks, start=1):
        labels[m] = i
    labels = _grow(labels, alpha > 0.2)

    full = np.dstack([rgb, alpha * 255.0]).clip(0, 255).astype(np.uint8)
    parts: list[LogoPart] = []
    for i, (_m, ci) in enumerate(big, start=1):
        sel = labels == i
        ys, xs = np.nonzero(sel)
        if len(ys) == 0:
            continue
        y0, y1, x0, x1 = int(ys.min()), int(ys.max()) + 1, int(xs.min()), int(xs.max()) + 1
        crop = full[y0:y1, x0:x1].copy()
        crop[..., 3] = np.where(sel[y0:y1, x0:x1], crop[..., 3], 0)
        parts.append(LogoPart(rgba=crop, x=x0, y=y0, cx=float(xs.mean()), cy=float(ys.mean()), area=len(ys),
                              color=tuple(int(v) for v in centres[ci]), is_accent=ci != main))
    ys, xs = np.nonzero(alpha > 0.1)
    accent = next((tuple(int(v) for v in centres[i]) for i in np.argsort(-counts) if i != main), None)
    return Logo(size=size, rgba=full, parts=parts,
                bbox=(int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1),
                main_color=tuple(int(v) for v in centres[main]), accent_color=accent)


def load_logo(path: Path, background: str | None = None, size: int = 512) -> Logo:
    return _load_cached(str(path), path.stat().st_mtime, background, size)
