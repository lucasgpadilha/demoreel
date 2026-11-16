"""The game's kill feed, redrawn for videos.

Players read a kill feed without thinking, so this one keeps the game's grammar instead of inventing one: a
dark translucent row per kill, newest at the bottom; the attacker in the colour of his side, ``+ assister``,
the weapon as the game's own icon, then the marks in the game's order (no-scope, through smoke, wallbang,
headshot), the victim in the colour of his side. The watched player's kills are outlined in red like the
local player's, his deaths get a red row. Blind and airborne kills carry their mark where the game puts it
(before the attacker, before the weapon).

Shared by the 2D replay and by the layouts drawn over first-person footage. Works without the game's icons
(``render/icons.py`` returns None on a machine without CS2): weapons and marks fall back to short text.

Also home of :class:`Type`, the text helper with per-glyph font fallback that player names need.
"""

from __future__ import annotations

import time
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import skia

from ..analysis import weapons
from ..analysis.schema import KillEvent
from ..brand import Brand
from . import icons

FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"

# role -> bundled file. A brand that names another family gets it from the system when it is installed.
FACES = {
    "name": "BarlowCondensed-SemiBold.ttf",
    "strong": "BarlowCondensed-Bold.ttf",
    "number": "BarlowCondensed-ExtraBold.ttf",
    "body": "Barlow-SemiBold.ttf",
    "body_light": "Barlow-Medium.ttf",
}
_WEIGHTS = {"name": 600, "strong": 700, "number": 800, "body": 600, "body_light": 500}
_INVISIBLE = {0xFE0E, 0xFE0F, 0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF}

ROW_H = 27.0  # at scale 1.0 (720p)
ROW_GAP = 4.0
ENTER_S = 0.16
EXIT_S = 0.32


def argb(hex_color: str, alpha: float = 1.0) -> int:
    h = hex_color.lstrip("#")
    a = 0.0 if alpha < 0 else 1.0 if alpha > 1 else alpha
    return skia.ColorSetARGB(int(a * 255 + 0.5), int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def blend(hex_a: str, hex_b: str, t: float) -> str:
    """Colour ``t`` of the way from a to b."""
    a, b = hex_a.lstrip("#"), hex_b.lstrip("#")
    return "#" + "".join(f"{round(int(a[i:i + 2], 16) + (int(b[i:i + 2], 16) - int(a[i:i + 2], 16)) * t):02x}"
                         for i in (0, 2, 4))


def clean_name(name: str | None, fallback: str = "?") -> str:
    """Player names are hostile input: control characters, line breaks, bidi overrides, walls of spaces."""
    out = []
    for ch in name or "":
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cs", "Co", "Cn", "Zl", "Zp") or (cat == "Cf" and ord(ch) not in _INVISIBLE):
            out.append(" ")
        else:
            out.append(ch)
    text = " ".join("".join(out).split())
    return text or fallback


class Type:
    """Text drawing with the brand's fonts and per-glyph fallback (names are full of symbols and emoji)."""

    def __init__(self, display_family: str = "Barlow Condensed", body_family: str = "Barlow") -> None:
        self.mgr = skia.FontMgr()
        self._tf: dict[str, skia.Typeface] = {}
        for role, file in FACES.items():
            family = body_family if role.startswith("body") else display_family
            bundled = "Barlow" if role.startswith("body") else "Barlow Condensed"
            tf = None
            if family and family != bundled and self.mgr.matchFamily(family).count() > 0:
                tf = self.mgr.matchFamilyStyle(family, skia.FontStyle(_WEIGHTS[role], skia.FontStyle.kNormal_Width,
                                                                      skia.FontStyle.kUpright_Slant))
            self._tf[role] = tf or skia.Typeface.MakeFromFile(str(FONT_DIR / file)) or skia.Typeface("")
        self._fonts: dict[tuple[str, float], skia.Font] = {}
        self._fallback: dict[tuple[int, float], skia.Font | None] = {}
        self._runs: dict[tuple[str, str, float], tuple[tuple[tuple[str, skia.Font, float], ...], float]] = {}
        self._fits: dict[tuple[str, str, float, float], str] = {}
        self._paint = skia.Paint(AntiAlias=True)

    def font(self, face: str, size: float) -> skia.Font:
        key = (face, round(size, 1))
        f = self._fonts.get(key)
        if f is None:
            f = skia.Font(self._tf.get(face) or self._tf["name"], key[1])
            f.setSubpixel(True)
            f.setEdging(skia.Font.Edging.kAntiAlias)
            self._fonts[key] = f
        return f

    def _fallback_font(self, ch: str, size: float) -> skia.Font | None:
        key = (ord(ch), round(size, 1))
        if key not in self._fallback:
            tf = self.mgr.matchFamilyStyleCharacter("", skia.FontStyle.Normal(), ["en"], ord(ch))
            f = None
            if tf is not None:
                f = skia.Font(tf, key[1])
                f.setSubpixel(True)
                f.setEdging(skia.Font.Edging.kAntiAlias)
            self._fallback[key] = f
        return self._fallback[key]

    def runs(self, text: str, face: str, size: float) -> tuple[tuple[tuple[str, skia.Font, float], ...], float]:
        """Split ``text`` into (string, font, width) runs, each drawable with one font. Cached."""
        key = (text, face, round(size, 1))
        hit = self._runs.get(key)
        if hit is not None:
            return hit
        base = self.font(face, size)
        out: list[tuple[str, skia.Font]] = []
        for ch in text:
            if ord(ch) in _INVISIBLE:
                continue
            f = base
            if not ch.isspace() and base.unicharToGlyph(ord(ch)) == 0:
                f = self._fallback_font(ch, size) or base
            if out and out[-1][1] is f:
                out[-1] = (out[-1][0] + ch, f)
            else:
                out.append((ch, f))
        measured = tuple((t, f, f.measureText(t)) for t, f in out)
        result = (measured, sum(w for _, _, w in measured))
        if len(self._runs) > 4000:
            self._runs.clear()
        self._runs[key] = result
        return result

    def measure(self, text: str, face: str, size: float) -> float:
        return self.runs(text, face, size)[1]

    def fit(self, text: str, face: str, size: float, max_width: float) -> str:
        """``text`` shortened with an ellipsis until it fits ``max_width``."""
        key = (text, face, round(size, 1), round(max_width, 1))
        hit = self._fits.get(key)
        if hit is not None:
            return hit
        out = text
        if self.measure(out, face, size) > max_width:
            lo, hi = 0, len(text)
            while lo < hi:  # longest prefix that fits with the ellipsis
                mid = (lo + hi + 1) // 2
                if self.measure(text[:mid].rstrip() + "…", face, size) <= max_width:
                    lo = mid
                else:
                    hi = mid - 1
            out = text[:lo].rstrip() + "…"
        if len(self._fits) > 4000:
            self._fits.clear()
        self._fits[key] = out
        return out

    def draw(self, canvas: skia.Canvas, text: str, x: float, y: float, face: str, size: float, color: int, *,
             align: str = "left", max_width: float | None = None, shadow: float = 0.0) -> float:
        """Draw ``text`` with its baseline at ``y``. ``shadow`` is the opacity of a soft drop shadow (0 = none).
        Returns the drawn width."""
        if max_width is not None:
            text = self.fit(text, face, size, max_width)
        runs, width = self.runs(text, face, size)
        if align == "center":
            x -= width / 2
        elif align == "right":
            x -= width
        if shadow > 0:
            self._paint.setColor(skia.ColorSetARGB(int(255 * shadow * skia.ColorGetA(color) / 255), 0, 0, 0))
            d = max(1.0, size * 0.07)
            cx = x
            for t, f, w in runs:
                canvas.drawString(t, cx + d * 0.6, y + d, f, self._paint)
                cx += w
        self._paint.setColor(color)
        for t, f, w in runs:
            canvas.drawString(t, x, y, f, self._paint)
            x += w
        return width


@lru_cache(maxsize=4)
def shared_type(display_family: str = "Barlow Condensed", body_family: str = "Barlow") -> Type:
    return Type(display_family, body_family)


# --------------------------------------------------------------------------------------------- row model
@dataclass(frozen=True)
class Part:
    """One item of a kill-feed row, left to right."""

    kind: str  # "name" | "icon" | "plus"
    value: str  # player name, icon name (see render/icons.py) or "+"
    side: str | None = None  # names: "CT" | "T" | None
    label: str = ""  # icons: the text drawn when the icon is not available


_MARK_TEXT = {"headshot": "HS", "wallbang": "wall", "smoke": "smoke", "noscope": "noscope", "blind": "blind",
              "airborne": "air", "flashbang_assist": "flash", "suicide": "✕"}


def row_parts(kill: KillEvent) -> list[Part]:
    """The row for a kill in the game's own order:
    [blind] attacker [+ [flash] assister] [airborne] weapon [noscope] [smoke] [wallbang] [headshot] victim."""
    parts: list[Part] = []
    attacker = kill.attacker
    has_attacker = attacker is not None and not kill.is_suicide and attacker.steamid != kill.victim.steamid
    if has_attacker:
        if kill.attacker_blind:
            parts.append(Part("icon", "blind", label=_MARK_TEXT["blind"]))
        parts.append(Part("name", clean_name(attacker.name), attacker.side))
        if kill.assister is not None and kill.assister.steamid != attacker.steamid:
            parts.append(Part("plus", "+"))
            if kill.flash_assist:
                parts.append(Part("icon", "flashbang_assist", label=_MARK_TEXT["flashbang_assist"]))
            parts.append(Part("name", clean_name(kill.assister.name), kill.assister.side))
        if kill.attacker_airborne:
            parts.append(Part("icon", "airborne", label=_MARK_TEXT["airborne"]))
    weapon = weapons.normalize(kill.weapon)
    if not has_attacker and weapon in ("world", "worldspawn"):
        parts.append(Part("icon", "suicide", label=_MARK_TEXT["suicide"]))
    else:
        parts.append(Part("icon", weapon, label=kill.weapon_name or weapons.display_name(kill.weapon)))
    if has_attacker:
        if kill.noscope and weapons.weapon_class(kill.weapon) == "sniper":
            parts.append(Part("icon", "noscope", label=_MARK_TEXT["noscope"]))
        if kill.through_smoke:
            parts.append(Part("icon", "smoke", label=_MARK_TEXT["smoke"]))
        if kill.wallbang:
            parts.append(Part("icon", "wallbang", label=_MARK_TEXT["wallbang"]))
    if kill.headshot:
        parts.append(Part("icon", "headshot", label=_MARK_TEXT["headshot"]))
    parts.append(Part("name", clean_name(kill.victim.name), kill.victim.side))
    return parts


def row_emphasis(kill: KillEvent, pov_steamid: str | None) -> str | None:
    """How a row relates to the watched player: "death" (he died), "kill" (his kill or assist) or None."""
    if not pov_steamid:
        return None
    if kill.victim.steamid == pov_steamid:
        return "death"
    if (kill.attacker is not None and kill.attacker.steamid == pov_steamid) or \
            (kill.assister is not None and kill.assister.steamid == pov_steamid):
        return "kill"
    return None


@dataclass(frozen=True)
class RowState:
    """Where one row is in its life at a given tick (all 0..1)."""

    alpha: float
    slide: float  # 1 = fully off to the side it enters from
    height: float  # share of the row height it currently takes in the stack


def visible_kills(kills: list[KillEvent], tick: float, tickrate: float, *, max_rows: int = 5,
                  hold_s: float = 6.0) -> list[tuple[KillEvent, RowState]]:
    """The rows on screen at ``tick``, oldest first, with their animation state."""
    out: list[tuple[KillEvent, RowState]] = []
    if tickrate <= 0 or max_rows <= 0:
        return out
    recent = [k for k in kills if k.tick <= tick and (tick - k.tick) / tickrate < hold_s]
    recent.sort(key=lambda k: k.tick)
    for k in recent[-max_rows:]:
        age = max(0.0, (tick - k.tick) / tickrate)
        enter = min(1.0, age / ENTER_S)
        enter = 1 - (1 - enter) ** 3
        leave = min(1.0, max(0.0, (age - (hold_s - EXIT_S)) / EXIT_S))
        out.append((k, RowState(alpha=enter * (1 - leave), slide=1 - enter,
                                height=min(min(1.0, age / (ENTER_S * 0.6)), 1 - leave * leave))))
    return out


# --------------------------------------------------------------------------------------------- drawing
_ROW_CACHE: dict[tuple, tuple[skia.Image, float, float]] = {}
_icons_checked = (-1e9, False)


def _icons_on() -> bool:
    """``icons.available()`` looks at the disk: ask at most every few seconds, not for every row of every frame."""
    global _icons_checked
    now = time.monotonic()
    if now - _icons_checked[0] > 5.0:
        _icons_checked = (now, icons.available())
    return _icons_checked[1]


def _row_image(kill: KillEvent, emphasis: str | None, brand: Brand, scale: float,
               max_name: float) -> tuple[skia.Image, float, float]:
    """A finished row as an image (cached: a row never changes once it exists). Returns (image, w, h)."""
    c = brand.colors
    key = (kill.tick, kill.victim.steamid, kill.attacker.steamid if kill.attacker else None, kill.weapon, emphasis,
           round(scale, 3), round(max_name, 1), c.background, c.text, c.ct, c.t, c.negative, c.muted,
           brand.fonts.display, _icons_on())
    hit = _ROW_CACHE.get(key)
    if hit is not None:
        return hit
    ty = shared_type(brand.fonts.display, brand.fonts.body)
    s = scale
    h = ROW_H * s
    pad, gap = 10 * s, 7 * s
    name_size, label_size = 17.5 * s, 13.5 * s
    side = {"CT": c.ct, "T": c.t}
    text_col = argb(c.text)
    items: list[tuple[str, object, float, int]] = []  # (kind, payload, width, colour)
    for p in row_parts(kill):
        if p.kind == "name":
            text = ty.fit(p.value, "strong", name_size, max_name)
            items.append(("text", (text, "strong", name_size), ty.measure(text, "strong", name_size),
                          argb(side.get(p.side or "", c.muted))))
        elif p.kind == "plus":
            items.append(("text", ("+", "strong", name_size), ty.measure("+", "strong", name_size), text_col))
        else:
            mark = p.value in icons.MARKS or p.value == "flashbang_assist"
            img = icons.icon(p.value, max(2, int(round((17 if mark else 15.5) * s))), text_col)
            if img is not None:
                items.append(("image", img, float(img.width()), text_col))
            elif p.label:
                items.append(("text", (p.label, "name", label_size), ty.measure(p.label, "name", label_size),
                              argb(c.text, 0.82)))
    w = 2 * pad + sum(i[2] for i in items) + gap * max(0, len(items) - 1)
    m = 2.0  # margin so the outline is not clipped
    surface = skia.Surface(int(w + 2 * m + 1), int(h + 2 * m + 1))
    cv = surface.getCanvas()
    cv.clear(skia.ColorTRANSPARENT)
    rect = skia.Rect.MakeXYWH(m, m, w, h)
    radius = 3 * s
    paint = skia.Paint(AntiAlias=True)
    paint.setColor(argb(blend(c.background, c.negative, 0.42), 0.86) if emphasis == "death" else argb(c.background, 0.8))
    cv.drawRoundRect(rect, radius, radius, paint)
    if emphasis:
        stroke = skia.Paint(AntiAlias=True, Style=skia.Paint.kStroke_Style, StrokeWidth=max(1.0, 1.7 * s),
                            Color=argb(c.negative))
        inset = stroke.getStrokeWidth() / 2
        cv.drawRoundRect(skia.Rect.MakeXYWH(m + inset, m + inset, w - 2 * inset, h - 2 * inset), radius, radius, stroke)
    x = m + pad
    for kind, payload, iw, color in items:
        if kind == "image":
            img: skia.Image = payload  # type: ignore[assignment]
            cv.drawImage(img, x, m + (h - img.height()) / 2, skia.SamplingOptions(skia.FilterMode.kLinear))
        else:
            text, face, size = payload  # type: ignore[misc]
            ty.draw(cv, text, x, m + h / 2 + size * 0.345, face, size, color)
        x += iw + gap
    result = (surface.makeImageSnapshot(), w, h)
    if len(_ROW_CACHE) > 256:
        _ROW_CACHE.clear()
    _ROW_CACHE[key] = result
    return result


def draw_killfeed(canvas: skia.Canvas, kills: list[KillEvent], tick: float, tickrate: float, *,
                  brand: Brand, scale: float, x: float, y: float, align: str = "right",
                  pov_steamid: str | None = None, max_rows: int = 5, hold_s: float = 6.0,
                  max_name_width: float = 170.0, opacity: float = 1.0) -> float:
    """Draw the rows for kills that happened up to `tick` (most recent last / at the bottom), each visible for
    `hold_s` seconds of game time with a short enter and exit animation. (x, y) is the top corner on the `align`
    side. `scale` = 1.0 at 720p height. Returns the height drawn.

    ``max_name_width`` (in 720p pixels, scaled) is where a long name gets its ellipsis; ``opacity`` fades the
    whole feed."""
    rows = visible_kills(kills, tick, tickrate, max_rows=max_rows, hold_s=hold_s)
    if not rows or opacity <= 0:
        return 0.0
    s = scale
    gap = ROW_GAP * s
    paint = skia.Paint(AntiAlias=True)
    sampling = skia.SamplingOptions(skia.FilterMode.kLinear)
    cy = y
    for kill, st in rows:
        image, w, h = _row_image(kill, row_emphasis(kill, pov_steamid), brand, s, max_name_width * s)
        if st.alpha > 0.004:
            off = st.slide * 26 * s
            left = x - w + off if align == "right" else x - off
            paint.setAlphaf(min(1.0, st.alpha * opacity))
            canvas.drawImage(image, left - 2.0, cy - 2.0, sampling, paint)
        cy += (h + gap) * st.height
    return max(0.0, cy - y - gap)
