"""Animated intro / outro bumpers generated from a brand kit — no motion designer, no project files.

The "assemble" style builds the client's logo on screen: the logo image is split into its pieces
(:mod:`.logo`), the main pieces fly in with perspective and motion blur, the last one slams home on an
impact (flash, shockwave, sparks, camera shake, chromatic aberration), the accent pieces ignite one by one,
a glint runs across the mark and the name is revealed. Sound is synthesised to the same timeline
(:mod:`.sfx`), so picture and audio can never drift.

Bumpers are rendered once per (brand, size, fps) and cached.
"""

from __future__ import annotations

import math
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import skia

from ..brand import Brand
from ..config import get_settings
from ..render import ffmpeg
from . import sfx
from .logo import Logo, LogoPart, load_logo

FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
T_HIT_FRACTION = 0.365  # where in the intro the last piece lands
GLOW_DIV = 4  # the glow pass runs at 1/4 resolution
MOTION_SAMPLES = 5  # sub-frames averaged per frame while things move


def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _col(hex_color: str, alpha: float = 1.0) -> int:
    r, g, b = _rgb(hex_color)
    return skia.ColorSetARGB(int(max(0.0, min(1.0, alpha)) * 255), r, g, b)


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


def _span(t: float, start: float, dur: float) -> float:
    """Progress 0..1 of an event that starts at ``start`` and lasts ``dur``."""
    return _clamp((t - start) / dur) if dur > 0 else float(t >= start)


def ease_out(x: float, p: float = 3.0) -> float:
    return 1 - (1 - x) ** p


def ease_in(x: float, p: float = 2.4) -> float:
    return x ** p


def ease_out_back(x: float, s: float = 1.6) -> float:
    x -= 1
    return 1 + x * x * ((s + 1) * x + s)


def typeface(name: str) -> skia.Typeface:
    tf = skia.Typeface.MakeFromFile(str(FONT_DIR / name))
    return tf or skia.Typeface("")


@dataclass
class _Piece:
    part: LogoPart
    image: skia.Image
    start: float  # entrance start (s)
    dur: float
    dx: float  # unit direction it comes from
    dy: float
    slam: bool = False
    ignite_at: float | None = None  # accents: when they switch on


class _Scene:
    def __init__(self, brand: Brand, kind: str, width: int, height: int, fps: int):
        self.brand, self.kind, self.w, self.h, self.fps = brand, kind, width, height, fps
        self.s = min(width, height) / 720  # vertical frames scale with their width
        self.dur = brand.bumper.intro_seconds if kind == "intro" else brand.bumper.outro_seconds
        self.n_frames = max(1, round(self.dur * fps))
        self.c = brand.colors
        self.rng = np.random.default_rng(11)
        self.logo: Logo | None = None
        if brand.logo_path is not None:
            try:
                self.logo = load_logo(brand.logo_path, brand.logo_background)
            except (OSError, ValueError):
                self.logo = None
        self.accent = "#{:02x}{:02x}{:02x}".format(*self.logo.accent_color) if self.logo and self.logo.accent_color else self.c.accent
        self.t_hit = self.dur * T_HIT_FRACTION if kind == "intro" else 0.16
        self.t_exit = self.dur - (0.30 if kind == "intro" else 0.55)
        self.pieces = self._plan_pieces()
        self.sparks = self._plan_sparks()
        self.f_name = skia.Font(typeface("BarlowCondensed-ExtraBold.ttf"), 86 * self.s)
        self.f_tag = skia.Font(typeface("Barlow-Medium.ttf"), 31 * self.s)
        self.f_cta = skia.Font(typeface("BarlowCondensed-Bold.ttf"), 62 * self.s)
        for f in (self.f_name, self.f_tag, self.f_cta):
            f.setSubpixel(True)
            f.setEdging(skia.Font.Edging.kAntiAlias)
        self.surface = skia.Surface(width, height)
        self.layer = skia.Surface(width, height)
        self.glow_small = skia.Surface(max(1, width // GLOW_DIV), max(1, height // GLOW_DIV))
        self.bg_surface = skia.Surface(width, height)
        yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
        d = np.hypot((xx - width / 2) / (width / 2), (yy - height / 2) / (height / 2))
        self.vignette = (1 - 0.42 * np.clip(d - 0.35, 0, 1) ** 1.6)[..., None]
        # A few pre-rolled film-grain plates, cycled per frame (generating fresh noise every frame is wasteful).
        grain_rng = np.random.default_rng(5)
        self.grain = [grain_rng.normal(0, 2.2, (height, width, 1)).astype(np.float32) for _ in range(8)]

    # ------------------------------------------------------------------ layout
    @property
    def logo_px(self) -> float:
        return min(self.h * (0.50 if self.kind == "intro" else 0.46), self.w * 0.60)

    def logo_origin(self) -> tuple[float, float, float]:
        """(x, y, scale) mapping logo-canvas pixels to the frame, centring the visible mark."""
        lg = self.logo
        k = self.logo_px / lg.size
        bx = (lg.bbox[0] + lg.bbox[2]) / 2
        by = (lg.bbox[1] + lg.bbox[3]) / 2
        cy = self.h * (0.415 if self.kind == "intro" else 0.40)
        return self.w / 2 - bx * k, cy - by * k, k

    def _plan_pieces(self) -> list[_Piece]:
        if self.logo is None:
            return []
        lg = self.logo
        mid_x = (lg.bbox[0] + lg.bbox[2]) / 2
        mid_y = (lg.bbox[1] + lg.bbox[3]) / 2
        mains = sorted((p for p in lg.parts if not p.is_accent), key=lambda p: -abs(p.cx - mid_x))
        accents = sorted((p for p in lg.parts if p.is_accent), key=lambda p: p.cx)
        if not mains:  # single-colour logo: treat every piece as a main piece
            mains, accents = sorted(lg.parts, key=lambda p: -abs(p.cx - mid_x)), []
        out: list[_Piece] = []
        first = self.t_hit - 0.62
        for i, p in enumerate(mains):
            last = i == len(mains) - 1
            off_x, off_y = p.cx - mid_x, p.cy - mid_y
            if last and len(mains) > 1:
                dx, dy = 0.0, -1.0  # the centre piece drops in from above and slams
            elif abs(off_x) >= abs(off_y) * 0.6:
                dx, dy = (1.0 if off_x > 0 else -1.0), 0.0
            else:
                dx, dy = 0.0, (1.0 if off_y > 0 else -1.0)
            dur = 0.40 if not last else 0.34
            start = (self.t_hit - dur) if last else first + i * 0.085
            out.append(_Piece(p, self._image(p), start, dur, dx, dy, slam=last))
        for i, p in enumerate(accents):
            out.append(_Piece(p, self._image(p), 0, 0, 0, 0, ignite_at=self.t_hit + 0.10 + i * 0.085))
        return out

    @staticmethod
    def _image(p: LogoPart) -> skia.Image:
        return skia.Image.fromarray(np.ascontiguousarray(p.rgba), colorType=skia.kRGBA_8888_ColorType,
                                    alphaType=skia.kUnpremul_AlphaType)

    def _plan_sparks(self) -> list[tuple[float, float, float, float, bool]]:
        out = []
        for _ in range(70):
            ang = self.rng.uniform(-math.pi, 0)  # upward hemisphere
            speed = self.rng.uniform(260, 980) * self.s
            out.append((math.cos(ang) * speed * 1.5, math.sin(ang) * speed, self.rng.uniform(0.25, 0.75),
                        self.rng.uniform(1.2, 3.0) * self.s, bool(self.rng.random() < 0.35)))
        return out

    # ------------------------------------------------------------------ drawing
    def _draw_background(self, cv: skia.Canvas, t: float) -> None:
        w, h, s = self.w, self.h, self.s
        cv.clear(_col(self.c.background))
        heat = (0.10 + 0.34 * math.exp(-max(0.0, t - self.t_hit) * 2.2)) if t >= self.t_hit else 0.05 * _span(t, 0, 0.5)
        ar, ag, ab = _rgb(self.accent)
        glow = skia.Paint(Shader=skia.GradientShader.MakeRadial(
            skia.Point(w / 2, h * 0.42), w * 0.55,
            [skia.ColorSetARGB(int(255 * heat), ar, ag, ab), skia.ColorSetARGB(0, ar, ag, ab)]))
        cv.drawRect(skia.Rect.MakeWH(w, h), glow)
        # Slanted bars drifting across: the logo's own angular language, kept very quiet.
        p = skia.Paint(AntiAlias=True)
        slant = 0.36
        for i in range(7):
            speed = (60 + 38 * i) * s
            x = ((i * 0.171 + 0.07) * w * 1.6 + t * speed) % (w * 1.6) - w * 0.3
            bw = (26 + 46 * ((i * 7) % 5)) * s
            alpha = (0.035 + 0.02 * (i % 3)) * _span(t, 0.0, 0.4)
            p.setColor(_col(self.c.text if i % 3 else self.accent, alpha))
            path = skia.Path()
            path.moveTo(x, h)
            path.lineTo(x + bw, h)
            path.lineTo(x + bw + h * slant, 0)
            path.lineTo(x + h * slant, 0)
            path.close()
            cv.drawPath(path, p)

    def _piece_matrix(self, pc: _Piece, t: float) -> tuple[skia.Matrix, float] | None:
        """Transform (around the piece's own centre) and opacity at time ``t``; None while not visible."""
        ox, oy, k = self.logo_origin()
        cx = ox + (pc.part.x + pc.part.rgba.shape[1] / 2) * k
        cy = oy + (pc.part.y + pc.part.rgba.shape[0] / 2) * k
        m = skia.Matrix()
        if self.kind == "outro":
            m.setTranslate(cx, cy)
            return m, 1.0
        if pc.ignite_at is not None:
            x = _span(t, pc.ignite_at, 0.22)
            if x <= 0:
                return None
            m.setTranslate(cx, cy)
            sc = 0.55 + 0.45 * ease_out_back(x, 2.4)
            m.preScale(sc, sc)
            return m, min(1.0, x * 5)
        x = _span(t, pc.start, pc.dur)
        if x <= 0:
            return None
        e = ease_in(x, 2.6) if pc.slam else ease_out_back(x, 1.15)
        dist = (self.h * 0.95 if pc.slam else self.w * 0.62) * (1 - e)
        angle = math.radians(78) * (1 - ease_out(x, 2.2))
        persp = 1.0 / (900 * self.s)
        m.setTranslate(cx + pc.dx * dist, cy + pc.dy * dist)
        tilt = skia.Matrix()
        if pc.dx:  # swing in around the vertical axis
            tilt.setAll(math.cos(angle), 0, 0, 0, 1, 0, -pc.dx * math.sin(angle) * persp, 0, 1)
        else:  # tip around the horizontal axis
            tilt.setAll(1, 0, 0, 0, math.cos(angle), 0, 0, -pc.dy * math.sin(angle) * persp, 1)
        m.preConcat(tilt)
        sc = 1 + 0.55 * (1 - e) if pc.slam else 1 + 0.25 * (1 - ease_out(x))
        m.preScale(sc, sc)
        return m, min(1.0, x * 3.5)

    def _draw_logo_layer(self, t: float) -> skia.Image:
        cv = self.layer.getCanvas()
        cv.clear(skia.ColorTRANSPARENT)
        if self.logo is None:
            return self.layer.makeImageSnapshot()
        _ox, _oy, k = self.logo_origin()
        sampling = skia.SamplingOptions(skia.FilterMode.kLinear, skia.MipmapMode.kLinear)
        punch = 1.0
        if t >= self.t_hit:
            punch = 1 + 0.07 * math.exp(-(t - self.t_hit) * 11)
        if self.kind == "outro":
            punch *= 1 + 0.16 * (1 - ease_out(_span(t, 0, 0.32), 3))
        hold = 1 + 0.035 * _span(t, self.t_hit, max(0.1, self.t_exit - self.t_hit))  # slow push-in
        cv.save()
        cv.translate(self.w / 2, self.h * 0.42)
        cv.scale(punch * hold, punch * hold)
        cv.translate(-self.w / 2, -self.h * 0.42)
        for pc in self.pieces:
            tm = self._piece_matrix(pc, t)
            if tm is None:
                continue
            m, alpha = tm
            ph, pw = pc.part.rgba.shape[:2]
            paint = skia.Paint(AntiAlias=True, Alphaf=alpha)
            if pc.ignite_at is not None and self.kind == "intro":
                hot = 1 - _span(t, pc.ignite_at, 0.30)
                if hot > 0:  # starts white-hot, cools to its colour
                    paint.setColorFilter(skia.ColorFilters.Matrix([
                        1, 0, 0, 0, hot, 0, 1, 0, 0, hot * 0.92, 0, 0, 1, 0, hot * 0.78, 0, 0, 0, 1, 0]))
            cv.save()
            cv.concat(m)
            cv.drawImageRect(pc.image, skia.Rect.MakeXYWH(-pw * k / 2, -ph * k / 2, pw * k, ph * k), sampling, paint)
            cv.restore()
        # Glint: a slanted band of light clipped to the mark.
        g0 = self.t_hit + (0.42 if self.kind == "intro" else 0.38)
        g = _span(t, g0, 0.55)
        if 0 < g < 1:
            x = self.w / 2 + (ease_out(g, 1.6) * 2 - 1) * self.logo_px * 0.95
            band = skia.Paint(AntiAlias=True, BlendMode=skia.BlendMode.kSrcATop, Shader=skia.GradientShader.MakeLinear(
                [skia.Point(x - 70 * self.s, 0), skia.Point(x + 70 * self.s, 0)],
                [0x00FFFFFF, skia.ColorSetARGB(int(210 * math.sin(g * math.pi)), 255, 255, 255), 0x00FFFFFF]))
            cv.save()
            cv.skew(-0.36, 0)
            cv.translate(self.h * 0.42 * 0.36, 0)
            cv.drawRect(skia.Rect.MakeXYWH(x - 90 * self.s, 0, 180 * self.s, self.h), band)
            cv.restore()
        cv.restore()
        return self.layer.makeImageSnapshot()

    def _draw_text(self, cv: skia.Canvas, t: float) -> None:
        s, w, c = self.s, self.w, self.c
        intro = self.kind == "intro"
        # the word sits a fixed distance under the mark, whatever the shape of the frame
        logo_cy = self.h * (0.415 if intro else 0.40)
        base = logo_cy + self.logo_px / 2 + 118 * s if self.logo is not None else self.h * 0.5
        t0 = self.t_hit + (0.30 if intro else 0.22)
        line = ease_out(_span(t, t0, 0.32), 3)
        out = 1 - _span(t, self.t_exit, 0.2) if not intro else 1.0
        if line <= 0 or out <= 0:
            return
        headline = self.brand.name.upper() if intro else (self.brand.bumper.cta or self.brand.url or self.brand.name)
        font = self.f_name if intro else self.f_cta
        track = (7 if intro else 2.5) * s
        glyphs = list(headline if not intro else headline)
        widths = [font.measureText(ch) for ch in glyphs]
        total = sum(widths) + track * (len(glyphs) - 1)
        if total > w * 0.86:  # very long names: scale the type down instead of clipping
            font = skia.Font(font.getTypeface(), font.getSize() * w * 0.86 / total)
            font.setSubpixel(True)
            track *= w * 0.86 / total
            widths = [font.measureText(ch) for ch in glyphs]
            total = sum(widths) + track * (len(glyphs) - 1)
        p = skia.Paint(AntiAlias=True)
        # accent rule grows from the centre, the word rises from behind it
        half = total / 2 * line
        rule_y = base + 14 * s
        p.setColor(_col(self.accent, out))
        cv.drawRect(skia.Rect.MakeXYWH(w / 2 - half, rule_y, half * 2, 3.5 * s), p)
        rise = ease_out(_span(t, t0 + 0.10, 0.36), 3.2)
        if rise > 0:
            cv.save()
            cv.clipRect(skia.Rect.MakeXYWH(0, base - font.getSize() * 1.1, w, font.getSize() * 1.1 + 12 * s))
            x = w / 2 - total / 2
            for i, (ch, cw) in enumerate(zip(glyphs, widths, strict=False)):
                stagger = _clamp(rise * 1.5 - (abs(i - len(glyphs) / 2) / max(1, len(glyphs))) * 0.5)
                p.setColor(_col(c.text, out * min(1.0, stagger * 1.6)))
                cv.drawString(ch, x, base + (1 - ease_out(stagger, 3)) * font.getSize() * 0.9, font, p)
                x += cw + track
            cv.restore()
        sub = self.brand.tagline
        fade = _span(t, t0 + 0.42, 0.4)
        if sub and fade > 0:
            p.setColor(_col(c.text, 0.72 * fade * out))
            tw = self.f_tag.measureText(sub)
            cv.drawString(sub, w / 2 - tw / 2, rule_y + 52 * s, self.f_tag, p)

    def _draw_impact(self, cv: skia.Canvas, t: float) -> None:
        dt = t - self.t_hit
        if dt < 0 or dt > 1.1:
            return
        w, h, s = self.w, self.h, self.s
        cx, cy = w / 2, h * 0.42
        ring = _span(dt, 0, 0.55)
        if ring < 1:
            p = skia.Paint(AntiAlias=True, Style=skia.Paint.kStroke_Style, StrokeWidth=(10 * (1 - ring) + 1.5) * s,
                           Color=_col(self.accent, 0.75 * (1 - ring) ** 1.5))
            cv.drawCircle(cx, cy, ease_out(ring, 2.5) * w * 0.62, p)
            p.setColor(_col(self.c.text, 0.35 * (1 - ring) ** 2))
            p.setStrokeWidth(2 * s)
            cv.drawCircle(cx, cy, ease_out(ring, 3.2) * w * 0.48, p)
        p = skia.Paint(AntiAlias=True, Style=skia.Paint.kStroke_Style, StrokeCap=skia.Paint.kRound_Cap,
                       BlendMode=skia.BlendMode.kPlus)
        gravity = 1500 * s
        for vx, vy, life, size, pale in self.sparks:
            x = dt / life
            if x >= 1:
                continue
            px = cx + vx * dt * (1 - 0.35 * x)
            py = cy + vy * dt + 0.5 * gravity * dt * dt
            tail = 0.028
            qx = cx + vx * (dt - tail) * (1 - 0.35 * x)
            qy = cy + vy * (dt - tail) + 0.5 * gravity * (dt - tail) ** 2
            p.setStrokeWidth(size * (1 - 0.5 * x))
            p.setColor(_col(self.c.text if pale else self.accent, (1 - x) ** 1.3))
            cv.drawLine(qx, qy, px, py, p)

    def _background_image(self, t: float) -> skia.Image:
        """The backdrop changes slowly, so it is drawn once per frame, not once per motion-blur sample."""
        self._draw_background(self.bg_surface.getCanvas(), t)
        return self.bg_surface.makeImageSnapshot()

    def _glow_image(self, t: float) -> skia.Image | None:
        """The mark blurred in the accent colour, at quarter size (~16x cheaper than a full-frame blur and
        indistinguishable once scaled up). Also computed once per frame."""
        if self.logo is None:
            return None
        ar, ag, ab = _rgb(self.accent)
        gc = self.glow_small.getCanvas()
        gc.clear(skia.ColorTRANSPARENT)
        gc.drawImageRect(self._draw_logo_layer(t), skia.Rect.MakeWH(self.w / GLOW_DIV, self.h / GLOW_DIV),
                         skia.SamplingOptions(skia.FilterMode.kLinear),
                         skia.Paint(ImageFilter=skia.ImageFilters.Blur(22 * self.s / GLOW_DIV, 22 * self.s / GLOW_DIV),
                                    ColorFilter=skia.ColorFilters.Blend(skia.ColorSetARGB(255, ar, ag, ab),
                                                                        skia.BlendMode.kSrcIn)))
        return self.glow_small.makeImageSnapshot()

    def subframe(self, t: float, background: skia.Image, glow: skia.Image | None) -> np.ndarray:
        cv = self.surface.getCanvas()
        cv.save()
        dt = t - self.t_hit
        if 0 <= dt < 0.5:  # camera shake, decaying
            amp = 16 * self.s * math.exp(-dt * 9)
            cv.translate(amp * math.sin(dt * 95), amp * math.cos(dt * 123 + 1.3))
        if self.kind == "intro" and t > self.t_exit:  # punch through the logo on the way out
            z = 1 + 2.4 * ease_in(_span(t, self.t_exit, self.dur - self.t_exit), 2.2)
            cv.translate(self.w / 2, self.h * 0.42)
            cv.scale(z, z)
            cv.translate(-self.w / 2, -self.h * 0.42)
        cv.clear(_col(self.c.background))
        cv.drawImage(background, 0, 0)
        logo = self._draw_logo_layer(t)
        if glow is not None:
            heat = 0.55 * math.exp(-max(0.0, dt) * 3.0) + 0.16 if dt >= 0 else 0.10
            cv.drawImageRect(glow, skia.Rect.MakeWH(self.w, self.h), skia.SamplingOptions(skia.FilterMode.kLinear),
                             skia.Paint(BlendMode=skia.BlendMode.kPlus, Alphaf=min(1.0, heat)))
        cv.drawImage(logo, 0, 0)
        self._draw_impact(cv, t)
        self._draw_text(cv, t)
        cv.restore()
        return self.surface.toarray(colorType=skia.kRGBA_8888_ColorType)

    def frame(self, i: int) -> np.ndarray:
        t = i / self.fps
        moving = (self.kind == "intro" and (self.t_hit - 0.75 <= t <= self.t_hit + 0.55 or t >= self.t_exit - 0.05)) \
            or (self.kind == "outro" and (t <= 0.4 or t >= self.t_exit))
        k = MOTION_SAMPLES if moving else 1  # motion blur: average sub-frames across the shutter
        background = self._background_image(t)
        glow = self._glow_image(t + 0.5 / self.fps if moving else t)
        if k == 1:
            img = self.subframe(t, background, glow).astype(np.float32)
        else:
            acc = np.zeros((self.h, self.w, 4), dtype=np.uint32)
            for j in range(k):
                acc += self.subframe(t + j / (k * self.fps), background, glow)
            img = acc.astype(np.float32) / k
        dt = t - self.t_hit
        # chromatic aberration around the hit and on the exit punch
        ca = 0.0
        if 0 <= dt < 0.45:
            ca = 9 * self.s * math.exp(-dt * 8)
        if self.kind == "intro" and t > self.t_exit:
            ca = max(ca, 12 * self.s * _span(t, self.t_exit, self.dur - self.t_exit))
        shift = int(round(ca))
        if shift:
            img[..., 0] = np.roll(img[..., 0], shift, axis=1)
            img[..., 2] = np.roll(img[..., 2], -shift, axis=1)
        flash = 0.0
        if 0 <= dt < 0.16:
            flash = 0.40 * (1 - dt / 0.16) ** 2
        if self.kind == "intro" and t > self.t_exit:
            flash = max(flash, ease_in(_span(t, self.t_exit + 0.08, self.dur - self.t_exit - 0.08), 2.0))
        if self.kind == "outro" and t < 0.22:
            flash = max(flash, (1 - t / 0.22) ** 2)
        rgb = img[..., :3]
        rgb *= self.vignette
        if flash > 0:
            tint = np.array([255, 246, 232], dtype=np.float32)
            rgb += (tint - rgb) * min(1.0, flash)
        if self.kind == "outro" and t > self.t_exit:  # fade to black
            rgb *= 1 - ease_in(_span(t, self.t_exit + 0.15, self.dur - self.t_exit - 0.15), 1.6)
        rgb += self.grain[(i * 7) % len(self.grain)]
        out = np.empty((self.h, self.w, 4), dtype=np.uint8)
        np.clip(rgb, 0, 255, out=rgb)
        out[..., :3] = rgb
        out[..., 3] = 255
        return out

    # ------------------------------------------------------------------ sound
    def soundtrack(self) -> sfx.Mix:
        mix = sfx.Mix(self.dur)
        mix.add(sfx.drone(self.dur, seed=21), 0, 0.5)
        if self.kind == "intro":
            lead = min(0.9, self.t_hit)
            mix.add(sfx.riser(lead, seed=22), self.t_hit - lead, 0.5)
            for i, pc in enumerate(p for p in self.pieces if p.ignite_at is None):
                pan = -0.9 * pc.dx if pc.dx else 0.0
                mix.add(sfx.whoosh(pc.dur + 0.12, pan_from=-pan, pan_to=pan * 0.15, seed=30 + i,
                                   low=220 if pc.slam else 380, high=3000 if pc.slam else 5200), pc.start - 0.05, 0.55)
            mix.add(sfx.impact(seed=40), self.t_hit - 0.004, 1.0)
            for i, pc in enumerate(p for p in self.pieces if p.ignite_at is not None):
                x = (pc.part.cx / self.logo.size - 0.5) * 1.6 if self.logo else 0.0
                mix.add(sfx.tick(freq=1900 * (1.26 ** i), seed=50 + i, pan=x), pc.ignite_at, 0.6)
            mix.add(sfx.tick(freq=5200, seed=60), self.t_hit + 0.55, 0.22)
            mix.add(sfx.whoosh(0.4, low=500, high=2400, pan_from=0, pan_to=0, seed=61), self.t_hit + 0.28, 0.22)
            mix.add(sfx.sweep_down(0.42, seed=62), self.t_exit - 0.10, 0.85)
        else:
            mix.add(sfx.impact(seed=70, weight=0.7, metal=False), self.t_hit - 0.004, 0.8)
            mix.add(sfx.tick(freq=2400, seed=71), self.t_hit + 0.30, 0.45)
            mix.add(sfx.tick(freq=5200, seed=72), self.t_hit + 0.50, 0.2)
            mix.add(sfx.sweep_down(0.55, seed=73), self.t_exit, 0.6)
        return mix


def render_bumper(brand: Brand, kind: str, out: Path, width: int, height: int, fps: int) -> Path:
    """Render one bumper (``kind`` = "intro" | "outro") to ``out`` as H.264 + AAC."""
    scene = _Scene(brand, kind, width - width % 2, height - height % 2, fps)
    tmp_video = out.with_suffix(".video.mp4")
    wav = out.with_suffix(".wav")
    writer = ffmpeg.open_rgb_writer(tmp_video, scene.w, scene.h, fps, crf=16, pix_fmt="rgba")
    try:
        for i in range(scene.n_frames):
            writer.stdin.write(scene.frame(i).tobytes())
    except BaseException:
        writer.kill()
        writer.wait()
        raise
    ffmpeg.close_writer(writer, "bumper encode")
    try:
        if brand.bumper.sound:
            scene.soundtrack().write(wav)
            audio = ["-i", str(wav)]
        else:
            audio = ["-f", "lavfi", "-t", f"{scene.dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        ffmpeg.run(["-i", str(tmp_video), *audio, "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                    "-shortest", "-movflags", "+faststart", str(out)], "bumper mux")
    finally:
        tmp_video.unlink(missing_ok=True)
        wav.unlink(missing_ok=True)
    return out


def ensure_bumper(brand: Brand, kind: str, width: int, height: int, fps: int) -> Path | None:
    """Cached bumper for this brand/size/fps; None when the brand opts out or the kind has zero length."""
    seconds = brand.bumper.intro_seconds if kind == "intro" else brand.bumper.outro_seconds
    if brand.bumper.style == "none" or seconds <= 0:
        return None
    cache = get_settings().data_dir / "cache" / "bumpers" / brand.slug
    target = cache / f"{brand.fingerprint()}-{kind}-{width}x{height}-{fps}.mp4"
    if target.exists() and target.stat().st_size > 0:
        return target
    cache.mkdir(parents=True, exist_ok=True)
    # Several workers may want the same bumper at once: each renders to its own temporary file and the first
    # one to finish wins with an atomic rename (the others just discard theirs).
    tmp = cache / f".{target.stem}.{os.getpid()}-{secrets.token_hex(3)}.partial.mp4"
    try:
        render_bumper(brand, kind, tmp, width, height, fps)
        if not (target.exists() and target.stat().st_size > 0):
            os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    for old in cache.glob(f"*-{kind}-{width}x{height}-{fps}.mp4"):  # drop bumpers of older kit versions
        if old != target and not old.name.startswith("."):
            try:
                old.unlink(missing_ok=True)
            except OSError:
                pass
    return target
