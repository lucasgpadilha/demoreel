"""The HUD of the 2D replay: the game's spectator furniture, drawn with the brand's slabs and type.

Scorebug (teams, score, clock or bomb timer, players alive), one roster per team (health, name, weapon,
bomb / kit), and the pieces shared with the first-person HUD in ``compose/hud.py``: the nameplate that
introduces the player, the kill counter, the caption, the letterbox of slow motion, the corner mark.
"""

from __future__ import annotations

from dataclasses import astuple, dataclass, replace

import skia

from ..killfeed import draw_killfeed
from .canvas import (
    SLANT,
    argb,
    blend,
    clamp,
    clean_name,
    ease_in,
    ease_out,
    ease_out_back,
    slab,
    slab_path,
    span,
)
from .layout import ROW_H, ROW_PITCH, Layout
from .theme import Theme, hand_icon


@dataclass
class BugState:
    """What the scorebug shows on one frame."""

    team_left: str
    team_right: str
    side_left: str  # "CT" | "T"
    side_right: str
    score_left: int
    score_right: int
    alive_left: int
    alive_right: int
    total_left: int
    total_right: int
    clock: str  # "1:23", or the seconds left on the bomb
    bomb: bool = False  # the clock is the bomb's
    bomb_safe: bool = False  # ... and it was defused
    fuse: float = 0.0  # share of the bomb timer left
    defuse: float = -1.0  # share of the defuse done, < 0 = nobody is defusing
    tab: str = ""  # "Round 7" or how the round ended
    tab_side: str | None = None  # set when the round is over: the winner's side
    since_end: float | None = None  # seconds since the round ended
    winner_left: bool = False
    speed: float = 1.0


@dataclass(frozen=True)
class RosterRow:
    name: str
    alive: bool
    hp: int
    weapon: str  # what he holds (demo name)
    has_c4: bool
    has_kit: bool
    is_pov: bool
    side: str | None


@dataclass
class PlateInfo:
    """Texts of the nameplate and the kill counter (see ``compose.overlay.clip_info``)."""

    player: str
    label: str
    detail: str
    kills_total: int


class Hud:
    def __init__(self, theme: Theme, layout: Layout):
        self.th, self.L, self.s = theme, layout, layout.s
        self.p = skia.Paint(AntiAlias=True)
        self._rows: dict[tuple, skia.Image] = {}
        self._bugs: dict[tuple, skia.Image] = {}

    # ------------------------------------------------------------------ small things
    def _text(self, cv: skia.Canvas, text: str, x: float, y: float, face: str, size: float, color: int, *,
              align: str = "left", max_width: float | None = None, shadow: float = 0.0) -> float:
        return self.th.ty.draw(cv, text, x, y, face, size * self.s, color, align=align, max_width=max_width,
                               shadow=shadow)

    def letterbox(self, cv: skia.Canvas, slow: float) -> None:
        if slow <= 0:
            return
        bar = self.L.letterbox * ease_out(clamp(slow), 2)
        self.p.setColor(skia.ColorBLACK)
        cv.drawRect(skia.Rect.MakeXYWH(0, 0, self.L.w, bar), self.p)
        cv.drawRect(skia.Rect.MakeXYWH(0, self.L.h - bar, self.L.w, bar), self.p)

    def logo(self, cv: skia.Canvas, alpha: float = 1.0) -> None:
        th, L = self.th, self.L
        if not th.brand.watermark or alpha <= 0:
            return
        if th.mark is not None:
            mw = L.logo_h * th.mark.width() / th.mark.height()
            cv.drawImageRect(th.mark, skia.Rect.MakeXYWH(L.logo_x, L.logo_y, mw, L.logo_h), th.linear,
                             skia.Paint(AntiAlias=True, Alphaf=0.92 * alpha))
        else:
            self._text(cv, th.brand.name, L.logo_x, L.logo_y + L.logo_h * 0.7, "strong", 21,
                       argb(th.c.text, 0.85 * alpha), max_width=170 * self.s, shadow=0.7)

    # ------------------------------------------------------------------ scorebug
    def scorebug(self, cv: skia.Canvas, st: BugState) -> None:
        """Everything but the two progress bars changes at most once a second: it is drawn once per state
        and kept as an image; the bars (bomb fuse, defuse) are drawn live on top."""
        s, y = self.s, self.L.scorebug_y
        if st.since_end is not None and st.since_end < 0.6:  # the winner's score is pulsing: draw it live
            self._draw_bug(cv, st, y)
        else:
            still = replace(st, fuse=0.0, defuse=-1.0, since_end=None)
            key = astuple(still)
            image = self._bugs.get(key)
            if image is None:
                surface = skia.Surface(int(self.L.w), int(90 * s))
                off = surface.getCanvas()
                off.clear(skia.ColorTRANSPARENT)
                self._draw_bug(off, still, 4 * s)
                image = surface.makeImageSnapshot()
                if len(self._bugs) > 200:
                    self._bugs.clear()
                self._bugs[key] = image
            cv.drawImage(image, 0, y - 4 * s)
        if st.bomb:
            th, c = self.th, self.th.c
            H, clock_w = 40 * s, 100 * s
            lean = H * SLANT
            x_clock = self.L.scorebug_cx - clock_w / 2 - lean / 2
            cv.save()
            cv.clipPath(slab_path(x_clock, y, clock_w, H), True)
            self.p.setColor(argb(c.positive if st.bomb_safe else c.negative))
            cv.drawRect(skia.Rect.MakeXYWH(x_clock, y + H - 3.5 * s, (clock_w + lean) * clamp(st.fuse), 3.5 * s),
                        self.p)
            if st.defuse >= 0:
                self.p.setColor(argb(th.side_bright["CT"]))
                cv.drawRect(skia.Rect.MakeXYWH(x_clock, y, (clock_w + lean) * clamp(st.defuse), 3.5 * s), self.p)
            cv.restore()

    def _draw_bug(self, cv: skia.Canvas, st: BugState, y: float) -> None:
        th, s, c, ty = self.th, self.s, self.th.c, self.th.ty
        H = 40 * s
        gap = 3 * s
        clock_w, score_w = 100 * s, 48 * s
        name_size = 21 * s
        max_name = self.L.team_name_w * s
        left = ty.fit(clean_name(st.team_left, ""), "name", name_size, max_name)
        right = ty.fit(clean_name(st.team_right, ""), "name", name_size, max_name)
        lw = ty.measure(left, "name", name_size) + 30 * s if left else 0.0
        rw = ty.measure(right, "name", name_size) + 30 * s if right else 0.0
        lean = H * SLANT
        x_clock = self.L.scorebug_cx - clock_w / 2 - lean / 2
        base = y + H / 2
        dark = argb(c.background, 0.86)

        def cell_text(text: str, x: float, w: float, face: str, size: float, color: int) -> None:
            ty.draw(cv, text, x + w / 2 + lean / 2, base + size * s * 0.35, face, size * s, color, align="center")

        # clock
        slab(cv, self.p, x_clock, y, clock_w, H, dark)
        if st.bomb:
            tint = c.positive if st.bomb_safe else c.negative
            icon = th.icon("c4", 19 * s, tint)
            tw = ty.measure(st.clock, "number", 30 * s)
            iw = (icon.width() + 7 * s) if icon is not None else 0.0
            x0 = x_clock + lean / 2 + (clock_w - tw - iw) / 2
            if icon is not None:
                cv.drawImage(icon, x0, base - icon.height() / 2, th.linear)
            ty.draw(cv, st.clock, x0 + iw, base + 30 * s * 0.35, "number", 30 * s, argb(tint))
        else:
            cell_text(st.clock, x_clock, clock_w, "number", 30, argb(c.text))
        # scores: a slab in the colour of the side, the number cut out dark
        for is_left, score, side in ((True, st.score_left, st.side_left), (False, st.score_right, st.side_right)):
            x = x_clock - gap - score_w if is_left else x_clock + clock_w + gap
            pulse = 0.0
            if st.since_end is not None and st.winner_left == is_left:
                pulse = 1 - clamp(st.since_end / 0.6)
            color = blend(th.side_color(side), "#ffffff", 0.75 * pulse)
            if pulse > 0:
                grow = 1 + 0.22 * pulse
                cv.save()
                cv.translate(x + score_w / 2 + lean / 2, base)
                cv.scale(grow, grow)
                cv.translate(-(x + score_w / 2 + lean / 2), -base)
            slab(cv, self.p, x, y, score_w, H, argb(color))
            cell_text(str(score), x, score_w, "number", 31, argb(c.background))
            if pulse > 0:
                cv.restore()
        # team names and who is alive
        for is_left, text, w, side, alive, total in ((True, left, lw, st.side_left, st.alive_left, st.total_left),
                                                     (False, right, rw, st.side_right, st.alive_right,
                                                      st.total_right)):
            if w > 0:
                x = x_clock - 2 * gap - score_w - w if is_left else x_clock + clock_w + 2 * gap + score_w
                slab(cv, self.p, x, y, w, H, dark)
                cell_text(text, x, w, "name", 21, argb(c.text))
            pw, ph, pg = 13 * s, 5 * s, 4 * s
            py = y + H + 5 * s
            for n in range(max(0, total)):  # one pip per player, lit while he lives, counted from the clock out
                px = x_clock - gap - pw - n * (pw + pg) if is_left else x_clock + clock_w + gap + n * (pw + pg)
                slab(cv, self.p, px - ph * SLANT, py, pw, ph, argb(th.side_color(side), 1.0 if n < alive else 0.26))
        # tab under the clock: the round, then how it ended
        if st.tab:
            size = 15.5 * s
            tw = ty.measure(st.tab, "strong", size)
            th_h = 21 * s
            w = tw + 22 * s
            x = self.L.scorebug_cx - w / 2 - th_h * SLANT / 2 - lean / 2
            ty_top = y + H + (15 if (st.total_left or st.total_right) else 4) * s
            x -= (ty_top - y - H) * SLANT  # keep hanging on the clock's lean
            color = th.side_color(st.tab_side) if st.tab_side else th.accent
            slab(cv, self.p, x, ty_top, w, th_h, argb(color, 0.96))
            ty.draw(cv, st.tab, x + w / 2 + th_h * SLANT / 2, ty_top + th_h / 2 + size * 0.35, "strong", size,
                    argb(c.background), align="center")
            if abs(st.speed - 1.0) > 0.01:  # slow motion says so, next to the round
                label = f"{st.speed:g}x"
                sw = ty.measure(label, "strong", size) + 18 * s
                sx = x + w + 5 * s
                slab(cv, self.p, sx, ty_top, sw, th_h, argb(c.background, 0.88))
                ty.draw(cv, label, sx + sw / 2 + th_h * SLANT / 2, ty_top + th_h / 2 + size * 0.35, "strong", size,
                        argb(th.accent), align="center")

    # ------------------------------------------------------------------ rosters
    def _roster_row(self, row: RosterRow, mirrored: bool) -> skia.Image:
        key = (row, mirrored)
        hit = self._rows.get(key)
        if hit is not None:
            return hit
        th, s, c, ty = self.th, self.s, self.th.c, self.th.ty
        w, h = self.L.roster_w, ROW_H * s
        lean = h * SLANT
        surface = skia.Surface(int(w + lean + 2), int(h + 1))
        cv = surface.getCanvas()
        cv.clear(skia.ColorTRANSPARENT)
        p = skia.Paint(AntiAlias=True)
        side_col = th.side_color(row.side)
        bg = blend(c.background, c.surface_raised, 0.9) if row.is_pov else c.background
        # body: flush with the edge of the frame on the outside, cut on the brand's lean on the inside
        body = slab_path(0, 0, w + (lean if mirrored else 0), h, left=mirrored, right=not mirrored)
        p.setColor(argb(bg, (0.9 if row.is_pov else 0.8) if row.alive else 0.5))
        cv.drawPath(body, p)
        cv.save()
        cv.clipPath(body, True)
        full = w + lean
        if row.alive:  # health along the bottom edge
            bar_h = 3.5 * s
            p.setColor(argb(side_col, 0.24))
            cv.drawRect(skia.Rect.MakeXYWH(0, h - bar_h, full, bar_h), p)
            p.setColor(argb(c.negative if row.hp <= 30 else th.side_bright.get(row.side or "", side_col)))
            share = full * clamp(row.hp / 100)
            cv.drawRect(skia.Rect.MakeXYWH(full - share if mirrored else 0, h - bar_h, share, bar_h), p)
        edge_w = 4 * s
        p.setColor(argb(c.text if row.is_pov else side_col, 1.0 if row.alive else 0.35))
        cv.drawRect(skia.Rect.MakeXYWH(full - edge_w if mirrored else 0, 0, edge_w, h), p)
        cv.restore()
        base = h / 2 + 6.2 * s - (1.5 * s if row.alive else 0)
        outer = full - 12 * s if mirrored else 12 * s  # x of the outer text edge
        sign = -1 if mirrored else 1
        hp_w = 30 * s
        name_x = outer + sign * (hp_w + 9 * s)
        inner = (lean + 10 * s) if mirrored else (w - 10 * s)  # x of the inner edge for icons
        room = abs(inner - name_x)
        if row.alive:
            ty.draw(cv, str(row.hp), outer + sign * hp_w, base + 0.6 * s, "number", 19.5 * s,
                    argb(c.negative if row.hp <= 30 else c.text), align="left" if mirrored else "right")
            ix = inner
            holding = hand_icon(row.weapon)
            icon = th.icon(holding, 14.5 * s, blend(c.text, c.muted, 0.25)) if holding else None
            if icon is not None and icon.width() > 74 * s:  # long guns: cap the width, keep the proportions
                icon = th.icon(holding, 14.5 * s * 74 * s / icon.width(), blend(c.text, c.muted, 0.25))
            if icon is not None:
                cv.drawImage(icon, ix if mirrored else ix - icon.width(), (h - 3 * s - icon.height()) / 2, th.linear)
                ix += sign * -1 * (icon.width() + 7 * s)
            elif row.weapon:
                tw = ty.draw(cv, row.weapon, ix, base - 1 * s, "name", 12.5 * s, argb(c.muted),
                             align="left" if mirrored else "right", max_width=62 * s)
                ix += sign * -1 * (tw + 7 * s)
            badge = "c4" if row.has_c4 else "defuser" if row.has_kit else None
            if badge:
                tint = th.side_bright.get("T" if row.has_c4 else "CT", c.text)
                bi = th.icon(badge, 13.5 * s, tint)
                if bi is not None:
                    cv.drawImage(bi, ix if mirrored else ix - bi.width(), (h - 3 * s - bi.height()) / 2, th.linear)
                    ix += sign * -1 * (bi.width() + 6 * s)
            room = abs(ix - name_x) - 4 * s
        ty.draw(cv, clean_name(row.name), name_x, base, "name", 18.5 * s,
                argb(c.text if row.alive else c.muted, 1.0 if row.alive else 0.7),
                align="left" if not mirrored else "right", max_width=max(30 * s, room))
        image = surface.makeImageSnapshot()
        if len(self._rows) > 600:
            self._rows.clear()
        self._rows[key] = image
        return image

    def rosters(self, cv: skia.Canvas, left: list[RosterRow], right: list[RosterRow], alpha: float = 1.0) -> None:
        s, L = self.s, self.L
        self.p.setAlphaf(alpha)
        lean = ROW_H * s * SLANT
        for rows, mirrored, x in ((left, False, L.roster_left_x), (right, True, L.roster_right_x - lean)):
            rows = rows[:7]
            y = L.roster_top(max(5, len(rows)) if L.roster_bottom is None else len(rows))
            for row in rows:
                cv.drawImage(self._roster_row(row, mirrored), x, y, self.th.linear, self.p)
                y += ROW_PITCH * s
        self.p.setAlphaf(1.0)

    # ------------------------------------------------------------------ kill feed
    def killfeed(self, cv: skia.Canvas, kills: list, tick: float, tickrate: float, pov: str | None) -> None:
        L = self.L
        draw_killfeed(cv, kills, tick, tickrate, brand=self.th.brand, scale=self.s, x=L.feed_x, y=L.feed_y,
                      align="right", pov_steamid=pov, max_rows=L.feed_rows, max_name_width=L.feed_name_w)

    # ------------------------------------------------------------------ shared with the first-person HUD
    def nameplate(self, cv: skia.Canvas, info: PlateInfo, t_clip: float) -> None:
        """Lower third that introduces the player, then gets out of the way."""
        show, hold, hide = 0.42, 3.0, 0.32
        if t_clip > show + hold + hide or not info.player:
            return
        slide = ease_out(span(t_clip, 0.10, show), 3.2) * (1 - ease_in(span(t_clip, show + hold, hide), 2))
        if slide <= 0:
            return
        th, s, c, ty, L = self.th, self.s, self.th.c, self.th.ty, self.L
        name_size, label_size, detail_size = 42 * s, 23 * s, 20 * s
        slab_h, label_h = 58 * s, 31 * s
        x0 = L.plate_x
        y0 = L.plate_bottom - slab_h - 6 * s - label_h
        name = ty.fit(clean_name(info.player), "number", name_size, min(L.w * 0.62, 470 * s))
        nw = ty.measure(name, "number", name_size)
        label_w = ty.measure(info.label, "strong", label_size) + 26 * s if info.label else 0.0
        detail = ty.fit(info.detail, "name", detail_size, max(60 * s, L.w * 0.7 - label_w)) if info.detail else ""
        detail_w = ty.measure(detail, "name", detail_size) if detail else 0.0
        width = max(nw + 44 * s, label_w + detail_w + 44 * s)
        cv.save()
        cv.clipRect(skia.Rect.MakeXYWH(0, y0 - 16 * s, x0 + width + 90 * s, slab_h + label_h + 40 * s))
        cv.translate(-(x0 + width + 80 * s) * (1 - slide), 0)
        slab(cv, self.p, x0 - 60 * s, y0, width + 60 * s, slab_h, argb(c.background, 0.88))
        slab(cv, self.p, x0 + width - 4 * s, y0, 8 * s, slab_h, argb(th.accent))
        ty.draw(cv, name, x0 + 24 * s, y0 + slab_h / 2 + name_size * 0.35, "number", name_size, argb(c.text))
        y1 = y0 + slab_h + 6 * s
        x = x0 + 4 * s
        if info.label:
            slab(cv, self.p, x, y1, label_w, label_h, argb(th.accent))
            ty.draw(cv, info.label, x + 13 * s + label_h * SLANT * 0.5, y1 + label_h / 2 + label_size * 0.35, "strong",
                    label_size, argb(c.background))
            x += label_w + 16 * s
        if detail:
            ty.draw(cv, detail, x, y1 + label_h / 2 + detail_size * 0.35, "name", detail_size, argb(c.text, 0.94),
                    shadow=0.8)
        cv.restore()

    def kill_counter(self, cv: skia.Canvas, info: PlateInfo, done: int, t_since_kill: float | None, t_clip: float,
                     t_left: float) -> None:
        total = info.kills_total
        if total < 2:
            return
        th, s, c, L = self.th, self.s, self.th.c, self.L
        fade = clamp((t_clip - L.counter_delay) / 0.4)  # it stays to the last frame: the clip closes on the result
        if fade <= 0:
            return
        pw, ph, gap = 34 * s, 13 * s, 9 * s
        row = total * pw + (total - 1) * gap
        x0, y = L.counter_cx - row / 2 - ph * SLANT / 2, L.counter_y
        for i in range(total):
            x = x0 + i * (pw + gap)
            is_done = i < done
            fresh = is_done and i == done - 1 and t_since_kill is not None and t_since_kill < 0.5
            if fresh:
                k = t_since_kill / 0.5
                grow = 1 + 0.9 * (1 - ease_out_back(clamp(k * 1.6), 2.2))
                cv.save()
                cv.translate(x + pw / 2, y + ph / 2)
                cv.scale(grow, grow)
                cv.translate(-(x + pw / 2), -(y + ph / 2))
                slab(cv, self.p, x, y, pw, ph, argb("#ffffff" if k < 0.25 else th.accent, fade))
                cv.restore()
            else:
                if not is_done:
                    slab(cv, self.p, x, y, pw, ph, argb(c.background, 0.55 * fade))
                slab(cv, self.p, x, y, pw, ph, argb(th.accent, fade) if is_done else argb(c.text, 0.34 * fade))
        # the number punches in above the row on each kill; the last one is the label of the play
        if t_since_kill is not None and done > 0:
            final = done >= total
            k = t_since_kill
            lasts = 0.75 if not final else max(0.75, t_left - 0.1 + k)  # the final label stays to the end
            if k < lasts + 0.35:
                a = clamp(k / 0.08) * (1 - span(k, lasts, 0.35))
                sc = 1 + 0.5 * (1 - ease_out(clamp(k / 0.22), 3))
                text = info.label if final and info.label else f"{done}/{total}"
                cv.save()
                cv.translate(L.counter_cx, y - 20 * s)
                cv.scale(sc, sc)
                th.ty.draw(cv, text, 0, 0, "number", 58 * s, argb(th.accent if final else c.text, a * fade),
                           align="center", shadow=0.75)
                cv.restore()

    def caption(self, cv: skia.Canvas, caption: str | None, alpha: float = 1.0) -> None:
        if not caption or alpha <= 0:
            return
        s, c, ty, L = self.s, self.th.c, self.th.ty, self.L
        size = 24 * s
        text = ty.fit(clean_name(caption, ""), "body", size, L.w * 0.84 - 60 * s)
        if not text:
            return
        tw = ty.measure(text, "body", size)
        h = 44 * s
        y = L.caption_y
        slab(cv, self.p, L.w / 2 - tw / 2 - 26 * s - h * SLANT / 2, y - 31 * s, tw + 52 * s, h,
             argb(c.background, 0.86 * alpha))
        ty.draw(cv, text, L.w / 2, y, "body", size, argb(c.text, alpha), align="center")

    # ------------------------------------------------------------------ title card of a reel
    def card(self, cv: skia.Canvas, t: float, duration: float, info: PlateInfo, line: str, index: int,
             total: int) -> None:
        """Announces the next play over the dimmed map: which one, what it is, who."""
        th, s, c, ty, L = self.th, self.s, self.th.c, self.th.ty, self.L
        tall = L.kind == "tall"
        x0 = (40 if tall else 96) * s
        width = L.w - x0 - 40 * s
        name_size = (74 if tall else 92) * s
        name = ty.fit(clean_name(info.player), "number", name_size, width)
        label_size, label_h = 40 * s, 54 * s
        y = L.h * 0.5 - (name_size * 0.5 + label_h * 0.5 + 12 * s)
        wipe = ease_out(span(t, 0.04, 0.32), 3.4)
        # which play of the reel this is
        pw, ph, gap = 30 * s, 11 * s, 8 * s
        for i in range(total):
            slab(cv, self.p, x0 + i * (pw + gap), y - 34 * s, pw, ph,
                 argb(th.accent) if i == index else argb(c.text, 0.3 if i > index else 0.62))
        if info.label:
            lw = ty.measure(info.label, "number", label_size) + 36 * s
            cv.save()
            cv.clipRect(skia.Rect.MakeXYWH(0, y, x0 + (lw + label_h) * wipe, label_h))
            slab(cv, self.p, x0, y, lw, label_h, argb(th.accent))
            ty.draw(cv, info.label, x0 + 18 * s + label_h * SLANT * 0.5, y + label_h / 2 + label_size * 0.35, "number",
                    label_size, argb(c.background))
            cv.restore()
            y += label_h + 8 * s
        rise = (1 - ease_out(span(t, 0.10, 0.36), 3)) * 26 * s
        a = span(t, 0.10, 0.22)
        ty.draw(cv, name, x0, y + name_size * 0.8 + rise, "number", name_size, argb(c.text, a), shadow=0.6)
        y += name_size * 0.98
        if line:
            ty.draw(cv, line, x0 + 2 * s, y + 32 * s, "name", 28 * s, argb(c.text, 0.82 * span(t, 0.22, 0.25)),
                    max_width=width, shadow=0.6)
