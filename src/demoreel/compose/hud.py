"""The branded HUD drawn over first-person footage.

The game's own HUD is hidden while recording (only the kill feed stays), so everything the viewer needs is
drawn here in the client's identity: a corner mark with round and score, a nameplate that introduces the
player and what is at stake, a kill counter that fills as the play unfolds and announces the play on its
decisive kill, the chat's reactions at the end, captions, a "replay" badge and letterbox bars for slow motion.

All elements are sized relative to the frame height, so 720p, 1080p and vertical formats share the code.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import skia

from ..brand import Brand
from ..render.killfeed import clean_name, shared_type
from .bumper import _clamp, _col, _span, ease_in, ease_out, ease_out_back, typeface
from .logo import load_logo

SLANT = 0.36  # the same lean as the bars in the bumpers
NAMEPLATE_S = 0.42 + 1.5 + 0.28  # slide in, hold, slide out: gone before the first kill (~2.5 s in)
LABEL_HOLD_S = 0.75  # the play's label stays this long after its kill (plus the freeze-frame), then fades
LABEL_FADE_S = 0.35
CHAT_STEP_S = 0.38  # chat lines enter one by one, this far apart
_CHAT_COLOURS = {"whine": "negative", "toxic": "negative", "laugh": "accent", "praise": "positive"}


@dataclass
class ChatLine:
    """One line of the match chat shown as a reaction to the play."""

    name: str
    text: str
    kind: str = "talk"  # whine | toxic | laugh | praise | gg | talk (see analysis/chat.py): picks the colour


@dataclass
class ClipInfo:
    """What the HUD needs to know about one highlight."""

    player: str
    label: str  # "Clutch 1v3", "ACE", "3 kills"…: announced on the decisive kill
    detail: str  # weapon and headshots: second line of the nameplate when nothing is at stake
    round_text: str  # "Round 7"
    context: str = ""  # what is at stake, shown instead of label + detail: "1v3 · 4 : 3", "Match point · 12 : 11"
    score_left: str = ""
    score_right: str = ""
    team_left: str = ""
    team_right: str = ""
    kill_ticks: list[int] = field(default_factory=list)
    # Bomb: a countdown from the plant until it is defused / explodes / the round ends, then "defused".
    plant_tick: int | None = None
    bomb_until_tick: int | None = None
    defuse_tick: int | None = None
    bomb_text: str = "Bomb"
    defused_text: str = "Defused"
    bomb_seconds: float = 40.0
    chat: list[ChatLine] = field(default_factory=list)  # reactions shown at the end of the play
    replay_text: str = "REPLAY"


@dataclass
class HudState:
    """Per-frame inputs. Times are in *output* seconds (after speed changes)."""

    clip: ClipInfo
    t_clip: float  # seconds since this clip (or, in a replay, the replay) started on screen
    t_left: float  # seconds until the play ends (a replay that follows it is not counted; in a replay: its own end)
    kills_done: int
    t_since_kill: float | None  # since the most recent kill
    caption: str | None = None
    slow: float = 0.0  # 0..1 how "in slow motion" the picture is (drives the letterbox)
    cut_flash: float = 0.0  # 0..1 white flash on a time jump
    tick: int | None = None  # demo tick shown by this frame
    tickrate: float = 64.0
    replay: bool = False  # the moment again from another point of view: only the badge, the caption, the bars
    freeze_s: float = 0.0  # how long the picture is held on the decisive kill (the label stays that much longer)
    punch: float = 0.0  # 0..1 light flash when the freeze-frame lands
    t_chat: float | None = None  # seconds since the chat reactions started to enter; None = not yet / never


class Hud:
    def __init__(self, brand: Brand, width: int, height: int, scale: float | None = None, show_mark: bool = True):
        self.brand, self.w, self.h = brand, width, height
        self.s = scale if scale is not None else height / 720
        self.show_mark = show_mark  # vertical layouts carry the logo in their header instead
        self.c = brand.colors
        self.mark: skia.Image | None = None
        self.accent = self.c.accent
        if brand.logo_path is not None:
            try:
                lg = load_logo(brand.logo_path, brand.logo_background)
                x0, y0, x1, y1 = lg.bbox
                self.mark = skia.Image.fromarray(np.ascontiguousarray(lg.rgba[y0:y1, x0:x1]),
                                                 colorType=skia.kRGBA_8888_ColorType,
                                                 alphaType=skia.kUnpremul_AlphaType).withDefaultMipmaps()
                if lg.accent_color:
                    self.accent = "#{:02x}{:02x}{:02x}".format(*lg.accent_color)
            except (OSError, ValueError):
                self.mark = None
        s = self.s
        self.f_name = self._font("BarlowCondensed-ExtraBold.ttf", 46 * s)
        self.f_label = self._font("BarlowCondensed-Bold.ttf", 25 * s)
        self.f_small = self._font("BarlowCondensed-SemiBold.ttf", 21 * s)
        self.f_score = self._font("BarlowCondensed-ExtraBold.ttf", 27 * s)
        self.f_caption = self._font("Barlow-SemiBold.ttf", 25 * s)
        self.f_big = self._font("BarlowCondensed-ExtraBold.ttf", 66 * s)
        self.p = skia.Paint(AntiAlias=True)
        self.type = shared_type(brand.fonts.display, brand.fonts.body)  # glyph fallback: names, and what players type
        self._split: dict[tuple[str, int], tuple[tuple[tuple[str, skia.Font, float], ...], float]] = {}
        self._fits: dict[tuple[str, int, float], str] = {}

    @staticmethod
    def _font(name: str, size: float) -> skia.Font:
        f = skia.Font(typeface(name), size)
        f.setSubpixel(True)
        f.setEdging(skia.Font.Edging.kAntiAlias)
        return f

    # ------------------------------------------------------------------ primitives
    def _slab(self, cv: skia.Canvas, x: float, y: float, w: float, h: float, color: int) -> None:
        """Parallelogram leaning like the logo's own strokes."""
        k = h * SLANT
        path = skia.Path()
        path.moveTo(x + k, y)
        path.lineTo(x + w + k, y)
        path.lineTo(x + w, y + h)
        path.lineTo(x, y + h)
        path.close()
        self.p.setColor(color)
        cv.drawPath(path, self.p)

    def _runs(self, text: str, font: skia.Font) -> tuple[tuple[tuple[str, skia.Font, float], ...], float]:
        """``text`` in ``font`` as (string, font, width) runs and their total width. Names of players and teams
        are full of symbols the brand's font does not have ("⑯ ✓ ☆ nick"): those go to a system font instead
        of coming out as empty boxes. A text the font covers is one run in it, exactly as before."""
        key = (text, id(font))  # the fonts live as long as the HUD does
        hit = self._split.get(key)
        if hit is None:
            if len(self._split) > 2000:
                self._split.clear()
            hit = self._split[key] = self.type.split(text, font)
        return hit

    def _width(self, text: str, font: skia.Font) -> float:
        """The width `_text` draws ``text`` with."""
        return self._runs(text, font)[1]

    def _text(self, cv: skia.Canvas, text: str, x: float, y: float, font: skia.Font, color: int, *,
              align: str = "left", shadow: bool = True) -> float:
        runs, w = self._runs(text, font)
        if align == "center":
            x -= w / 2
        elif align == "right":
            x -= w
        if shadow:
            self.p.setColor(skia.ColorSetARGB(int(150 * (skia.ColorGetA(color) / 255)), 0, 0, 0))
            sx = x
            for t, f, rw in runs:
                cv.drawString(t, sx + 1.5 * self.s, y + 2 * self.s, f, self.p)
                sx += rw
        self.p.setColor(color)
        for t, f, rw in runs:
            cv.drawString(t, x, y, f, self.p)
            x += rw
        return w

    def _fit(self, text: str, font: skia.Font, max_width: float) -> str:
        """``text`` shortened with an ellipsis until `_text` draws it within ``max_width``."""
        key = (text, id(font), round(max_width, 1))
        hit = self._fits.get(key)
        if hit is None:
            hit = text
            if self._width(hit, font) > max_width:
                while len(hit) > 1 and self.type.split(hit + "…", font)[1] > max_width:
                    hit = hit[:-1]
                hit = hit.rstrip() + "…"
            if len(self._fits) > 2000:
                self._fits.clear()
            self._fits[key] = hit
        return hit

    # ------------------------------------------------------------------ elements
    def _corner(self, cv: skia.Canvas, st: HudState) -> None:
        s, c = self.s, st.clip
        fade = _clamp(min(st.t_clip / (0.2 if st.replay else 0.35), st.t_left / 0.3))
        if fade <= 0:
            return
        x, y, mh = 30 * s, 26 * s, 46 * s
        if st.replay:  # a replay is letterboxed: the corner sits just under the bar, not across its edge
            y = max(y, self._bar(st) + 10 * s)
        if self.mark is not None and self.brand.watermark and self.show_mark:
            mw = mh * self.mark.width() / self.mark.height()
            cv.drawImageRect(self.mark, skia.Rect.MakeXYWH(x, y, mw, mh),
                             skia.SamplingOptions(skia.FilterMode.kLinear, skia.MipmapMode.kLinear),
                             skia.Paint(AntiAlias=True, Alphaf=0.92 * fade))
            x += mw + 16 * s
        base = y + mh * 0.72
        if st.replay:
            # The same moment again: the score, the round and the bomb clock of the play would be noise (and the
            # clock would run backwards), so the corner only says what this is.
            bh = mh * 0.74
            bw = self._width(c.replay_text, self.f_label) + 30 * s
            self._slab(cv, x, y + mh * 0.13, bw, bh, _col(self.accent, 0.96 * fade))
            self._text(cv, c.replay_text, x + 15 * s + bh * SLANT * 0.5, y + mh * 0.13 + bh * 0.76, self.f_label,
                       _col(self.c.background, fade), shadow=False)
            return
        # scorebug: team – score : score – team, then the round
        if c.score_left != "" and c.score_right != "":
            if c.team_left:
                x += self._text(cv, self._fit(c.team_left, self.f_small, 150 * s), x, base, self.f_small,
                                _col(self.c.text, 0.9 * fade)) + 10 * s
            x += self._text(cv, c.score_left, x, base, self.f_score, _col(self.c.text, fade)) + 6 * s
            x += self._text(cv, ":", x, base - 2 * s, self.f_score, _col(self.c.muted, fade)) + 6 * s
            x += self._text(cv, c.score_right, x, base, self.f_score, _col(self.c.text, fade)) + 10 * s
            if c.team_right:
                x += self._text(cv, self._fit(c.team_right, self.f_small, 150 * s), x, base, self.f_small,
                                _col(self.c.text, 0.9 * fade)) + 16 * s
        if c.round_text:
            rw = self._width(c.round_text, self.f_small) + 22 * s
            self._slab(cv, x, y + mh * 0.24, rw, mh * 0.62, _col(self.accent, 0.95 * fade))
            self._text(cv, c.round_text, x + 11 * s + mh * 0.62 * SLANT * 0.5, base - 1 * s, self.f_small,
                       _col(self.c.background, fade), shadow=False)
            x += rw + 12 * s
        # Bomb clock: the game's own HUD is hidden, so without it nobody knows why a clutch is in a hurry.
        text, colour = None, self.c.negative
        if st.tick is not None and c.plant_tick is not None and st.tick >= c.plant_tick:
            if c.defuse_tick is not None and c.defuse_tick <= st.tick <= c.defuse_tick + 2.5 * st.tickrate:
                text, colour = c.defused_text, self.c.positive
            elif c.bomb_until_tick is None or st.tick < c.bomb_until_tick:
                left = max(0.0, c.bomb_seconds - (st.tick - c.plant_tick) / st.tickrate)
                text = f"{c.bomb_text} 0:{int(left + 0.999):02d}"
        if text:
            bw = self._width(text, self.f_small) + 22 * s
            self._slab(cv, x, y + mh * 0.24, bw, mh * 0.62, _col(colour, 0.95 * fade))
            self._text(cv, text, x + 11 * s + mh * 0.62 * SLANT * 0.5, base - 1 * s, self.f_small,
                       _col("#ffffff", fade), shadow=False)

    def _nameplate(self, cv: skia.Canvas, st: HudState) -> None:
        """Lower third that introduces the player, then gets out of the way."""
        s, c = self.s, st.clip
        slide = self._nameplate_slide(st)
        if slide <= 0:
            return
        x0, y0 = 34 * s, self.h - 176 * s
        name, label, detail, width = self._nameplate_box(c)
        label_w = self._width(label, self.f_label) + 26 * s if label else 0
        dx = -(width + 80 * s) * (1 - slide)
        cv.save()
        cv.clipRect(skia.Rect.MakeXYWH(0, y0 - 16 * s, self.w * 0.7, 140 * s))
        cv.translate(dx, 0)
        self._slab(cv, x0 - 60 * s, y0, width + 60 * s, 64 * s, _col(self.c.background, 0.86))
        self._slab(cv, x0 + width - 4 * s, y0, 8 * s, 64 * s, _col(self.accent))
        self._text(cv, name, x0 + 26 * s, y0 + 48 * s, self.f_name, _col(self.c.text), shadow=False)
        y1 = y0 + 70 * s
        x = x0 + 4 * s
        if label:
            self._slab(cv, x, y1, label_w, 34 * s, _col(self.accent))
            self._text(cv, label, x + 13 * s + 34 * s * SLANT * 0.5, y1 + 25.5 * s, self.f_label,
                       _col(self.c.background), shadow=False)
            x += label_w + 18 * s
        if detail:
            self._text(cv, detail, x, y1 + 25 * s, self.f_small, _col(self.c.text, 0.92))
        cv.restore()

    @staticmethod
    def _nameplate_slide(st: HudState) -> float:
        show, hold, hide = 0.42, 1.5, 0.28
        if st.replay or st.t_clip > NAMEPLATE_S:
            return 0.0
        return ease_out(_span(st.t_clip, 0.10, show), 3.2) * (1 - ease_in(_span(st.t_clip, show + hold, hide), 2))

    def _nameplate_box(self, c: ClipInfo) -> tuple[str, str, str, float]:
        """(name, text in the accent slab, text after it, width). With something at stake the second line says
        that - "1v3" in the slab, the score after it - and the play's own label waits for the decisive kill."""
        s = self.s
        name = self._fit(c.player, self.f_name, self.w * 0.42)
        label, _, detail = c.context.partition(" · ") if c.context else (c.label, "", c.detail)
        label_w = self._width(label, self.f_label) + 26 * s if label else 0
        detail_w = self._width(detail, self.f_small) if detail else 0
        return name, label, detail, max(self._width(name, self.f_name) + 44 * s, label_w + detail_w + 44 * s)

    def _kill_counter(self, cv: skia.Canvas, st: HudState) -> None:
        total = len(st.clip.kill_ticks)
        if total < 2:
            return
        s = self.s
        fade = _clamp(min((st.t_clip - 0.4) / 0.4, st.t_left / 0.3))
        if fade <= 0:
            return
        pw, ph, gap = 34 * s, 13 * s, 9 * s
        row = total * pw + (total - 1) * gap
        x0, y = self.w / 2 - row / 2, self.h - 62 * s
        for i in range(total):
            x = x0 + i * (pw + gap)
            done = i < st.kills_done
            fresh = done and i == st.kills_done - 1 and st.t_since_kill is not None and st.t_since_kill < 0.5
            if fresh:
                k = st.t_since_kill / 0.5
                grow = 1 + 0.9 * (1 - ease_out_back(_clamp(k * 1.6), 2.2))
                cv.save()
                cv.translate(x + pw / 2, y + ph / 2)
                cv.scale(grow, grow)
                cv.translate(-(x + pw / 2), -(y + ph / 2))
                self._slab(cv, x, y, pw, ph, _col("#ffffff" if k < 0.25 else self.accent, fade))
                cv.restore()
            else:
                self._slab(cv, x, y, pw, ph, _col(self.accent, fade) if done else _col(self.c.text, 0.28 * fade))
        # the number punches in above the row on each kill; the last one is announced by `_label`
        last = st.kills_done >= total and bool(st.clip.label)
        if st.t_since_kill is not None and st.t_since_kill < 1.1 and st.kills_done > 0 and not last:
            k = st.t_since_kill
            self._punch_text(cv, f"{st.kills_done}/{total}", k, _clamp(k / 0.08) * (1 - _span(k, 0.75, 0.35)) * fade,
                             self.c.text)

    def _punch_text(self, cv: skia.Canvas, text: str, k: float, alpha: float, colour: str) -> None:
        """Big text in the lane above the kill counter, landing with a short overshoot ``k`` seconds ago."""
        sc = 1 + 0.5 * (1 - ease_out(_clamp(k / 0.22), 3))
        cv.save()
        cv.translate(self.w / 2, self.h - 84 * self.s)
        cv.scale(sc, sc)
        self._text(cv, text, 0, 0, self.f_big, _col(colour, alpha), align="center")
        cv.restore()

    def _label(self, cv: skia.Canvas, st: HudState) -> None:
        """What the play was - "ACE", "Clutch 1v3", "3 kills" - landing on its decisive kill (with the
        freeze-frame) and staying through it."""
        c = st.clip
        if not c.label or not c.kill_ticks or st.kills_done < len(c.kill_ticks) or st.t_since_kill is None:
            return
        k, hold = st.t_since_kill, st.freeze_s + LABEL_HOLD_S
        if k >= hold + LABEL_FADE_S:
            return
        alpha = _clamp(k / 0.08) * (1 - _span(k, hold, LABEL_FADE_S)) * _clamp(st.t_left / 0.3)
        self._punch_text(cv, c.label, k, alpha, self.accent)

    def _caption_rect(self, st: HudState) -> tuple[str, float, float, float]:
        """(text, left edge, baseline, width of the slab). The lane above the kill counter belongs to the big
        number and the play's label, so in a clip that has those the caption sits one lane higher."""
        s = self.s
        text = self._fit(st.caption or "", self.f_caption, self.w * 0.7)
        tw = self._width(text, self.f_caption)
        busy = not st.replay and (len(st.clip.kill_ticks) >= 2 or bool(st.clip.label))
        return text, self.w / 2 - tw / 2 - 26 * s, self.h - (176 if busy else 128) * s, tw + 44 * s

    def _caption(self, cv: skia.Canvas, st: HudState) -> None:
        if not st.caption:
            return
        s = self.s
        text, left, y, width = self._caption_rect(st)
        alpha = 1.0
        slide = self._nameplate_slide(st)
        if slide > 0:  # a caption that would sit on the nameplate waits for it to leave
            plate = 34 * s + self._nameplate_box(st.clip)[3] + 64 * s * SLANT + 12 * s
            if left < plate:
                alpha = 1 - slide
        if alpha <= 0:
            return
        self._slab(cv, left, y - 31 * s, width, 44 * s, _col(self.c.background, 0.82 * alpha))
        self._text(cv, text, self.w / 2, y, self.f_caption, _col(self.c.text, alpha), align="center", shadow=False)

    def _chat(self, cv: skia.Canvas, st: HudState) -> None:
        """What the lobby typed about the play: one slab per line, stacked like a chat (newest at the bottom, the
        older ones pushed up), on the left - clear of the crosshair and of the kill counter in every shape."""
        lines = st.clip.chat
        if not lines or st.t_chat is None or st.t_chat <= 0:
            return
        s = self.s
        fade = _clamp(st.t_left / 0.3) * (1 - self._nameplate_slide(st))
        if fade <= 0:
            return
        row_h, gap, size = 38 * s, 6 * s, 21 * s
        max_w = min(self.w * 0.58, 470 * s)
        x0 = 30 * s
        y = self.h - (224 if st.caption else 150) * s  # bottom of the stack: above the label lane (and a caption)
        for i in range(len(lines) - 1, -1, -1):
            line = lines[i]
            enter = ease_out(_clamp((st.t_chat - i * CHAT_STEP_S) / 0.24), 3)
            if enter <= 0:
                continue
            y -= (row_h + gap) * enter
            colour = getattr(self.c, _CHAT_COLOURS.get(line.kind, "muted"))
            name = self.type.fit(clean_name(line.name), "strong", size, max_w * 0.42) + ":"
            nw = self.type.measure(name, "strong", size)
            text = self.type.fit(" ".join(line.text.split()), "body", size * 0.94, max_w - nw - 48 * s)
            width = nw + self.type.measure(text, "body", size * 0.94) + 46 * s
            alpha = enter * fade
            dx = -(1 - enter) * 36 * s
            self._slab(cv, x0 + dx, y, width, row_h, _col(self.c.background, 0.84 * alpha))
            self._slab(cv, x0 + dx, y, 6 * s, row_h, _col(colour, alpha))
            tx = x0 + dx + 20 * s + row_h * SLANT * 0.5
            self.type.draw(cv, name, tx, y + row_h * 0.70, "strong", size,
                           _col(self.c.text if colour == self.c.muted else colour, alpha))
            self.type.draw(cv, text, tx + nw + 9 * s, y + row_h * 0.70, "body", size * 0.94, _col(self.c.text, alpha))

    def _bar(self, st: HudState) -> float:
        """Height of each letterbox bar."""
        return self.h * 0.06 * ease_out(st.slow, 2) if st.slow > 0 else 0.0

    def draw(self, cv: skia.Canvas, st: HudState) -> None:
        if st.slow > 0:  # letterbox closes in for slow motion
            bar = self._bar(st)
            self.p.setColor(skia.ColorBLACK)
            cv.drawRect(skia.Rect.MakeXYWH(0, 0, self.w, bar), self.p)
            cv.drawRect(skia.Rect.MakeXYWH(0, self.h - bar, self.w, bar), self.p)
        self._corner(cv, st)
        if not st.replay:  # a replay shows the badge (in the corner), its caption and the bars: nothing else
            self._nameplate(cv, st)
            self._kill_counter(cv, st)
            self._label(cv, st)
            self._chat(cv, st)
        self._caption(cv, st)
        flash = max(st.cut_flash, 0.2 * st.punch)
        if flash > 0:
            self.p.setColor(skia.ColorSetARGB(int(235 * flash), 255, 246, 232))
            cv.drawRect(skia.Rect.MakeWH(self.w, self.h), self.p)
