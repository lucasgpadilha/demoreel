"""Pictures prepared once per render: the re-toned radar image and the sprites for smoke, fire and light.

Valve's radar images each have their own palette (Nuke is teal and sand, Inferno is slate). Drawn as they
are, they fight the brand and the side colours. Here every map is re-toned onto one ramp taken from the
brand kit, keeping its light/dark structure and a trace of its hues (bomb sites stay findable), so the two
side colours are the only saturated things in the frame.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np
import skia
from PIL import Image, ImageFilter

from ...brand import BrandColors
from .canvas import argb, blend

MAP_TEXTURE = 2048  # the radar is 1024 px; the camera zooms in, so it is resampled once with a good filter


def _rgb(hex_color: str) -> np.ndarray:
    h = hex_color.lstrip("#")
    return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], dtype=np.float32)


def tone_radar(rgba: np.ndarray, colors: BrandColors) -> np.ndarray:
    """Re-tone a radar image (h, w, 4 uint8, straight alpha) onto the brand ramp. Returns premultiplied RGBA."""
    px = rgba.astype(np.float32)
    alpha = px[..., 3] / 255.0
    edge = max(2, rgba.shape[0] // 200)  # some radar images carry a hairline frame on their border
    alpha[:edge] = alpha[-edge:] = 0.0
    alpha[:, :edge] = alpha[:, -edge:] = 0.0
    rgb = px[..., :3]
    lum = rgb @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    solid = alpha > 0.6
    if solid.any():
        ref = lum[solid]
        mid = float(np.median(ref))
        spread = max(18.0, float(np.percentile(ref, 96) - np.percentile(ref, 4)))
    else:
        mid, spread = 128.0, 64.0
    d = np.clip((lum - mid) / spread, -1.0, 1.0)  # -1 darker than the floor .. +1 lighter
    t = np.clip(0.3 + 0.4 * d, 0.05, 0.74)[..., None]
    lo, hi = _rgb(colors.surface_raised), _rgb(colors.muted)
    tone = lo + (hi - lo) * t
    tone_lum = tone @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    hue = rgb * (tone_lum / np.maximum(lum, 1.0))[..., None]  # the original colour at the new brightness
    out = tone * 0.74 + np.clip(hue, 0, 255) * 0.26
    # a lighter rim just inside the outline of the map: it reads as a cut-out plan, not a blurry blob
    a_img = Image.fromarray((alpha * 255).astype(np.uint8))
    inner = np.asarray(a_img.filter(ImageFilter.MinFilter(5)), dtype=np.float32) / 255.0
    rim = np.clip(alpha - inner, 0, 1)[..., None]
    out = out * (1 - 0.55 * rim) + _rgb(blend(colors.muted, colors.text, 0.35)) * 0.55 * rim
    result = np.empty(rgba.shape, dtype=np.uint8)
    result[..., :3] = np.clip(out * alpha[..., None], 0, 255).astype(np.uint8)
    result[..., 3] = (alpha * 255).astype(np.uint8)
    return result


def _board(colors: BrandColors, size: int, radar: skia.Image | None) -> skia.Image:
    """The ground texture at ``size`` px: the brand's background with the faint crosses of a plotting table
    (one every 64 radar px), and the re-toned radar on top when there is one.

    Opaque and in the surface's own pixel format on purpose: skia's raster backend has a fast path for
    exactly that, and anything else (RGBA arrays, mipmapped images, a tiled shader for the crosses) is
    converted on every draw: 100 ms per frame instead of 3."""
    surface = skia.Surface.MakeRaster(skia.ImageInfo.MakeN32(size, size, skia.kOpaque_AlphaType))
    cv = surface.getCanvas()
    cv.clear(argb(colors.background))
    unit = size / 1024
    paint = skia.Paint(AntiAlias=False, Color=argb(colors.border, 0.3 if radar is not None else 0.6))
    arm, thick = 3.5 * unit, max(1.0, unit)
    for i in range(1, 16):
        for j in range(1, 16):
            x, y = i * 64 * unit, j * 64 * unit
            cv.drawRect(skia.Rect.MakeXYWH(x - arm, y - thick / 2, 2 * arm, thick), paint)
            cv.drawRect(skia.Rect.MakeXYWH(x - thick / 2, y - arm, thick, 2 * arm), paint)
    if radar is not None:
        cv.drawImageRect(radar, skia.Rect.MakeWH(size, size),
                         skia.SamplingOptions(skia.FilterMode.kLinear, skia.MipmapMode.kLinear))
    return surface.makeImageSnapshot()


@lru_cache(maxsize=8)
def _toned(path: str | None, mtime: float, colors_key: tuple[str, ...]) -> tuple[skia.Image, ...]:
    colors = BrandColors(surface_raised=colors_key[0], muted=colors_key[1], text=colors_key[2],
                         background=colors_key[3], border=colors_key[4])
    radar = None
    if path is not None:
        try:
            img = Image.open(path).convert("RGBA").resize((MAP_TEXTURE, MAP_TEXTURE), Image.Resampling.BICUBIC)
            arr = np.ascontiguousarray(tone_radar(np.asarray(img), colors))
            radar = skia.Image.fromarray(arr, colorType=skia.kRGBA_8888_ColorType,
                                         alphaType=skia.kPremul_AlphaType).withDefaultMipmaps()
        except (OSError, ValueError):
            radar = None
    return tuple(_board(colors, size, radar) for size in (MAP_TEXTURE, MAP_TEXTURE // 2, MAP_TEXTURE // 4))


def map_texture(path: Path | None, colors: BrandColors) -> tuple[skia.Image, ...]:
    """The ground of a map for this brand as a short mip chain (2048, 1024, 512 px): the radar image
    re-toned, or just the plotting table when there is no image (``path`` None). Cached in memory."""
    key = (colors.surface_raised, colors.muted, colors.text, colors.background, colors.border)
    if path is None or not path.is_file():
        return _toned(None, 0.0, key)
    return _toned(str(path), path.stat().st_mtime, key)


def pick_level(levels: tuple[skia.Image, ...], scale: float) -> skia.Image:
    """The mip level to draw when one radar pixel (1/1024 of the image) covers ``scale`` screen pixels."""
    for image in reversed(levels):  # smallest first
        if image.width() >= 1024 * scale * 0.85:
            return image
    return levels[0]


def smoke_sprite(colors: BrandColors, seed: int, size: int = 224) -> skia.Image:
    """A soft, mottled cloud: overlapping blurred puffs, dense in the middle and gone at the edge."""
    rng = np.random.default_rng(seed)
    surface = skia.Surface(size, size)
    cv = surface.getCanvas()
    cv.clear(skia.ColorTRANSPARENT)
    r = size / 2
    paint = skia.Paint(AntiAlias=True, MaskFilter=skia.MaskFilter.MakeBlur(skia.kNormal_BlurStyle, size * 0.035))
    paint.setColor(argb(blend(colors.text, colors.muted, 0.7), 0.5))
    cv.drawCircle(r, r, r * 0.7, paint)
    for _ in range(26):
        ang, dist = rng.uniform(0, 6.283), rng.uniform(0.0, 0.56) * r
        shade = blend(colors.text, colors.muted, float(rng.uniform(0.15, 1.0)))
        paint.setColor(argb(shade, float(rng.uniform(0.12, 0.3))))
        cv.drawCircle(r + np.cos(ang) * dist, r + np.sin(ang) * dist, float(rng.uniform(0.16, 0.36)) * r, paint)
    fade = skia.Paint(AntiAlias=True, BlendMode=skia.BlendMode.kDstIn)
    fade.setShader(skia.GradientShader.MakeRadial(
        skia.Point(r, r), r, [0xFFFFFFFF, 0xFFFFFFFF, 0x00FFFFFF], [0.0, 0.62, 1.0]))
    cv.drawPaint(fade)
    return surface.makeImageSnapshot()


def radial_sprite(stops: list[tuple[float, int]], size: int = 96) -> skia.Image:
    """A round gradient sprite (flames, glows): ``stops`` = [(position 0..1, ARGB colour), ...]."""
    surface = skia.Surface(size, size)
    cv = surface.getCanvas()
    cv.clear(skia.ColorTRANSPARENT)
    paint = skia.Paint(AntiAlias=True)
    paint.setShader(skia.GradientShader.MakeRadial(skia.Point(size / 2, size / 2), size / 2,
                                                   [c for _, c in stops], [p for p, _ in stops]))
    cv.drawPaint(paint)
    return surface.makeImageSnapshot()


def flame_sprite() -> skia.Image:
    return radial_sprite([(0.0, skia.ColorSetARGB(255, 255, 244, 205)), (0.28, skia.ColorSetARGB(235, 255, 176, 64)),
                          (0.62, skia.ColorSetARGB(120, 232, 84, 28)), (1.0, skia.ColorSetARGB(0, 190, 40, 10))])


def glow_sprite(hex_color: str = "#ffffff") -> skia.Image:
    return radial_sprite([(0.0, argb(hex_color, 1.0)), (0.35, argb(hex_color, 0.55)), (1.0, argb(hex_color, 0.0))])


def spotlight(colors: BrandColors, size: int = 256) -> skia.Image:
    """Darkness with a soft hole in the middle: scaled up over the frame in slow motion (a gradient shader
    over a whole frame is ten times slower than stretching this)."""
    return radial_sprite([(0.0, argb(colors.background, 0.0)), (0.1, argb(colors.background, 0.0)),
                          (0.42, argb(colors.background, 0.78)), (1.0, argb(colors.background, 0.9))], size)


def vignette(width: int, height: int, colors: BrandColors, strength: float = 0.5) -> skia.Image:
    """Darkens the frame towards its corners so the HUD at the edges sits on a calmer map."""
    surface = skia.Surface(width, height)
    cv = surface.getCanvas()
    cv.clear(skia.ColorTRANSPARENT)
    paint = skia.Paint(AntiAlias=True)
    radius = float(np.hypot(width, height)) / 2
    paint.setShader(skia.GradientShader.MakeRadial(
        skia.Point(width / 2, height / 2), radius,
        [argb(colors.background, 0.0), argb(colors.background, 0.0), argb(colors.background, strength)],
        [0.0, 0.48, 1.0]))
    cv.drawPaint(paint)
    return surface.makeImageSnapshot()
