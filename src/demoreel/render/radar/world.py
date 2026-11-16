"""Draws the map layer of a frame: the re-toned radar, utility, the bomb, shots, the trace and the players.

Everything here is positioned in *radar pixels* (the 1024x1024 space of the radar image) and put on screen
through a view ``(cx, cy, k)`` supplied by the camera: radar point (cx, cy) lands on the centre of the
layout's focus rectangle, one radar pixel is ``k`` screen pixels.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import skia

from ...analysis.schema import RoundSummary
from .. import maps
from ..base import ClipSegment
from . import art
from .canvas import argb, blend, clamp, clean_name, ease_out, ease_out_back
from .data import Area, Events, Flight, SegmentData, Track
from .layout import Layout, Rect
from .theme import Theme

ROUTE_STEP = 8  # ticks between two points of the route
PLANT_SECONDS = 3.2
BOMB_SECONDS = 40.0
_FLIGHT_ICON = {"smoke": "smokegrenade", "flash": "flashbang", "he": "hegrenade", "molotov": "molotov",
                "decoy": "decoy"}


@dataclass
class Mark:
    """A death pinned on the map."""

    tick: int
    x: float  # where the victim fell (radar px)
    y: float
    lower: bool
    side: str | None  # the victim's side
    ax: float | None  # where the killer stood
    ay: float | None
    attacker_side: str | None
    pov: bool  # a kill of the watched player: it gets a number and its line stays
    number: int
    frame: int  # clip frame on which it happens (the output clock drives its animation)


@dataclass
class Trace:
    """The drawing a clip leaves on the map: the watched player's route and every death, across segments."""

    routes: list[tuple[skia.Path, bool]] = field(default_factory=list)  # (polyline in radar px, lower level)
    marks: list[Mark] = field(default_factory=list)
    lo: np.ndarray = field(default_factory=lambda: np.array([np.inf, np.inf]))
    hi: np.ndarray = field(default_factory=lambda: np.array([-np.inf, -np.inf]))

    def grow(self, x: float, y: float) -> None:
        self.lo = np.minimum(self.lo, (x, y))
        self.hi = np.maximum(self.hi, (x, y))

    @property
    def empty(self) -> bool:
        return not np.isfinite(self.lo).all()


@dataclass
class Scene:
    """One segment, prepared: demo data plus everything derived from it once."""

    seg: ClipSegment
    data: SegmentData
    rnd: RoundSummary | None
    info: maps.MapInfo
    tickrate: float
    trace: Trace
    pov: Track | None
    flights: list[Flight]
    flash_left: dict[str, np.ndarray]  # pid -> seconds of blindness left, per tick
    flash_total: dict[str, np.ndarray]
    busy_since: dict[str, np.ndarray]  # pid -> tick index where the current defuse started (-1 = not defusing)
    route_xy: np.ndarray  # (m, 2) radar px, NaN where the watched player is dead or absent
    route_lower: np.ndarray
    route_done: int = 0  # how many route points are already in trace.routes
    route_open: bool = False

    def sample(self, t: Track, tick: float) -> tuple[float, float, float, float, int] | None:
        """Interpolated (x, y, z, yaw, index) of a player at a fractional tick, or None if absent."""
        d = self.data
        f = tick - d.start
        i0 = max(0, min(int(math.floor(f)), d.n - 1))
        if not t.present[i0]:
            return None
        i1 = min(i0 + 1, d.n - 1)
        a = f - i0 if t.present[i1] and t.alive[i0] and t.alive[i1] else 0.0
        dyaw = ((float(t.yaw[i1]) - float(t.yaw[i0]) + 180) % 360) - 180
        return (float(t.x[i0]) + (float(t.x[i1]) - float(t.x[i0])) * a,
                float(t.y[i0]) + (float(t.y[i1]) - float(t.y[i0])) * a,
                float(t.z[i0]), float(t.yaw[i0]) + dyaw * a, i0)


def run_starts(active: np.ndarray) -> np.ndarray:
    """For a boolean array: per element, the index where its run of True began (-1 where False)."""
    n = len(active)
    idx = np.arange(n)
    starts = np.where(active & ~np.concatenate(([False], active[:-1])), idx, -1)
    np.maximum.accumulate(starts, out=starts)
    return np.where(active, starts, -1)


def blindness(flash: np.ndarray, tickrate: float) -> tuple[np.ndarray, np.ndarray]:
    """(seconds left, total seconds) of blindness per tick from the demo's ``flash_duration`` prop, which
    holds the full length of a blindness for as long as it lasts."""
    active = flash > 0.05
    change = np.concatenate(([True], np.abs(np.diff(flash)) > 1e-3))
    idx = np.arange(len(flash))
    starts = np.where(active & change, idx, -1)
    np.maximum.accumulate(starts, out=starts)
    left = np.where(active, flash - (idx - starts) / tickrate, 0.0)
    return np.clip(left, 0.0, None).astype(np.float32), np.where(active, flash, 0.0).astype(np.float32)


class World:
    def __init__(self, theme: Theme, layout: Layout, events: Events, map_name: str, tickrate: float):
        self.th, self.L, self.events, self.map_name, self.tickrate = theme, layout, events, map_name, tickrate
        self.s = layout.s
        self.fill = skia.Paint(AntiAlias=True)
        self.stroke = skia.Paint(AntiAlias=True, Style=skia.Paint.kStroke_Style, StrokeCap=skia.Paint.kRound_Cap,
                                 StrokeJoin=skia.Paint.kRound_Join)
        self.img = skia.Paint(AntiAlias=True)
        self.plus = skia.Paint(AntiAlias=True, BlendMode=skia.BlendMode.kPlus)
        self.images: dict[bool, tuple[skia.Image, ...]] = {}
        self._fires: dict[int, np.ndarray] = {}

    # ------------------------------------------------------------------ helpers
    def radar_image(self, lower: bool) -> tuple[skia.Image, ...]:
        """Ground texture of one floor (mip chain). Maps without a radar image get the bare plotting table."""
        if lower not in self.images:
            p = maps.radar_image(self.map_name, lower) or (maps.radar_image(self.map_name) if lower else None)
            self.images[lower] = art.map_texture(p, self.th.c)
        return self.images[lower]

    def _sprite(self, cv: skia.Canvas, image: skia.Image, x: float, y: float, radius: float, alpha: float,
                paint: skia.Paint | None = None, rotate: float = 0.0) -> None:
        if alpha <= 0.004 or radius <= 0.5:
            return
        p = paint or self.img
        p.setAlphaf(min(1.0, alpha))
        if rotate:
            cv.save()
            cv.translate(x, y)
            cv.rotate(rotate)
            cv.drawImageRect(image, skia.Rect.MakeLTRB(-radius, -radius, radius, radius), self.th.linear, p)
            cv.restore()
        else:
            cv.drawImageRect(image, skia.Rect.MakeLTRB(x - radius, y - radius, x + radius, y + radius),
                             self.th.linear, p)

    def _icon(self, cv: skia.Canvas, name: str, x: float, y: float, height: float, color: str,
              alpha: float = 1.0) -> bool:
        image = self.th.icon(name, height, color)
        if image is None:
            return False
        self.img.setAlphaf(min(1.0, alpha))
        cv.drawImage(image, x - image.width() / 2, y - image.height() / 2, self.th.linear, self.img)
        return True

    def _fire_blobs(self, a: Area) -> np.ndarray:
        key = a.start * 4099 + int(a.x)
        blobs = self._fires.get(key)
        if blobs is None:
            rng = np.random.default_rng(abs(key) % (2 ** 32))
            n = 11
            ang, dist = rng.uniform(0, math.tau, n), np.sqrt(rng.uniform(0.02, 1.0, n)) * 112
            blobs = np.stack([np.cos(ang) * dist, np.sin(ang) * dist, rng.uniform(34, 62, n),
                              rng.uniform(0.16, 0.42, n), rng.uniform(0, math.tau, n)], axis=1)
            self._fires[key] = blobs
        return blobs

    # ------------------------------------------------------------------ the frame
    def draw(self, cv: skia.Canvas, sc: Scene, tick: float, view: np.ndarray, focus: Rect, lower_mix: float,
             clip_frame: int, fps: int, slow: float = 0.0, map_alpha: float = 1.0) -> None:
        """``view`` puts radar point (cx, cy) on the centre of ``focus`` at ``k`` screen px per radar px."""
        th, L, s, c = self.th, self.L, self.s, self.th.c
        info = sc.info
        cx, cy, k = float(view[0]), float(view[1]), float(view[2])
        fx, fy = focus.cx, focus.cy
        upp = k / info.scale  # screen px per world unit
        lower = lower_mix >= 0.5

        def pos(x: float, y: float) -> tuple[float, float]:
            return fx + ((x - info.pos_x) / info.scale - cx) * k, fy + ((info.pos_y - y) / info.scale - cy) * k

        def rpos(rx: float, ry: float) -> tuple[float, float]:
            return fx + (rx - cx) * k, fy + (ry - cy) * k

        def level(z: float) -> float:
            return 1.0 if info.is_lower(z) == lower else 0.32

        # --- ground -----------------------------------------------------------------------------
        cv.clear(argb(c.background))
        cv.save()
        cv.translate(fx, fy)
        cv.scale(k, k)
        cv.translate(-cx, -cy)
        dst = skia.Rect.MakeWH(maps.RADAR_SIZE, maps.RADAR_SIZE)
        self.img.setAlphaf(map_alpha)
        cv.drawImageRect(art.pick_level(self.radar_image(lower), k), dst, th.linear, self.img)
        if 0.02 < lower_mix < 0.98:  # changing floors: dissolve between the two radar images
            self.img.setAlphaf(map_alpha * (1 - abs(lower_mix - 0.5) * 2) * 0.5)
            cv.drawImageRect(art.pick_level(self.radar_image(not lower), k), dst, th.linear, self.img)
        cv.restore()
        cv.drawImage(th.vignette, 0, 0, th.linear)

        tr = self.tickrate
        # --- fire ---------------------------------------------------------------------------------
        for a in self.events.fires:
            if not a.start <= tick <= a.end:
                continue
            x, y = pos(a.x, a.y)
            age = (tick - a.start) / tr
            fade = min(1.0, (a.end - tick) / (0.5 * tr)) * level(a.z)
            grow = ease_out(clamp(age / 0.9), 2.2)
            self.fill.setColor(argb("#200a04", 0.42 * fade))
            cv.drawCircle(x, y, 132 * upp * (0.4 + 0.6 * grow), self.fill)
            for bx, by, br, bf, ph in self._fire_blobs(a):
                flick = 0.78 + 0.3 * math.sin(tick * bf + ph) + 0.12 * math.sin(tick * bf * 2.7 + ph * 2)
                self._sprite(cv, th.flame, x + bx * grow * upp, y + by * grow * upp, br * upp * flick * 1.5,
                             0.62 * fade * (0.7 + 0.3 * flick), self.plus)
        # --- smoke --------------------------------------------------------------------------------
        for a in self.events.smokes:
            if not a.start <= tick <= a.end:
                continue
            x, y = pos(a.x, a.y)
            age, left = (tick - a.start) / tr, (a.end - tick) / tr
            bloom = ease_out(clamp(age / 0.5), 2.5)
            fade = clamp(left / 1.1) * level(a.z)
            radius = 172 * upp * (0.3 + 0.7 * bloom)
            # two layers drifting against each other keep the cloud alive (no rotation: that is the slow path)
            ph = tick * 0.016 + a.start
            drift = radius * 0.045
            self._sprite(cv, th.smoke[0], x + math.cos(ph) * drift, y + math.sin(ph) * drift, radius, 0.88 * fade)
            self._sprite(cv, th.smoke[1], x - math.cos(ph * 0.7) * drift, y - math.sin(ph * 0.7) * drift,
                         radius * (0.9 + 0.03 * math.sin(ph * 1.3)), 0.5 * fade)
            if age > 0.4:  # how much longer it stands
                ring = 154 * upp
                self.stroke.setStrokeWidth(max(1.0, 1.5 * s))
                self.stroke.setColor(argb(c.text, 0.13 * fade))
                cv.drawCircle(x, y, ring, self.stroke)
                self.stroke.setColor(argb(c.text, 0.62 * fade))
                share = clamp((a.end - tick) / max(1.0, a.end - a.start))
                cv.drawArc(skia.Rect.MakeLTRB(x - ring, y - ring, x + ring, y + ring), -90, -360 * share, False,
                           self.stroke)
        # --- bursts -------------------------------------------------------------------------------
        for a in self.events.hes:
            if a.start <= tick <= a.end:
                p = (tick - a.start) / max(1, a.end - a.start)
                x, y = pos(a.x, a.y)
                lv = level(a.z)
                self._sprite(cv, th.glow_warm, x, y, (70 + 230 * ease_out(p)) * upp, (1 - p) ** 1.6 * lv, self.plus)
                self.stroke.setStrokeWidth(max(1.0, 2.2 * s * (1 - p)))
                self.stroke.setColor(argb("#ffb070", (1 - p) * lv))
                cv.drawCircle(x, y, 200 * upp * ease_out(p), self.stroke)
        for a in self.events.flashes:
            if a.start <= tick <= a.end:
                p = (tick - a.start) / max(1, a.end - a.start)
                x, y = pos(a.x, a.y)
                self._sprite(cv, th.glow, x, y, (60 + 260 * ease_out(p)) * upp, (1 - p) ** 1.3 * level(a.z), self.plus)

        # --- slow motion: the map dims outside a pool of light around the action -------------------
        if slow > 0.01 and sc.pov is not None:
            smp = sc.sample(sc.pov, tick)
            px, py = pos(smp[0], smp[1]) if smp else (fx, fy)
            reach = float(math.hypot(L.w, L.h)) * 1.05  # the sprite always covers the frame
            self._sprite(cv, th.spotlight, px, py, reach, 0.8 * slow)

        # --- the trace: route of the watched player -------------------------------------------------
        self._advance_route(sc, tick)
        if sc.trace.routes:
            cv.save()
            cv.translate(fx, fy)
            cv.scale(k, k)
            cv.translate(-cx, -cy)
            self.stroke.setStrokeWidth(max(1.0, 1.7 * s) / k)
            for path, path_lower in sc.trace.routes:
                self.stroke.setColor(argb(c.text, 0.5 if path_lower == lower else 0.17))
                cv.drawPath(path, self.stroke)
            cv.restore()

        # --- bomb ---------------------------------------------------------------------------------
        self._draw_bomb(cv, sc, tick, pos, level, upp)

        # --- grenades in the air ------------------------------------------------------------------
        for fl in sc.flights:
            if not fl.start <= tick <= fl.end + 10:
                continue
            j = int(min(np.searchsorted(fl.ticks, tick), len(fl.ticks) - 1))
            lv = level(float(fl.z[j]))
            gone = clamp((tick - fl.end) / 10)
            j0 = max(0, j - 36)
            if j - j0 >= 2:
                path = skia.Path()
                step = 3
                x0, y0 = pos(float(fl.x[j0]), float(fl.y[j0]))
                path.moveTo(x0, y0)
                for q in range(j0 + step, j, step):
                    path.lineTo(*pos(float(fl.x[q]), float(fl.y[q])))
                path.lineTo(*pos(float(fl.x[j]), float(fl.y[j])))
                self.stroke.setStrokeWidth(max(1.0, 1.3 * s))
                self.stroke.setColor(argb(c.text, 0.42 * lv * (1 - gone)))
                cv.drawPath(path, self.stroke)
            hx, hy = pos(float(fl.x[j]), float(fl.y[j]))
            if not self._icon(cv, _FLIGHT_ICON[fl.kind], hx, hy, 13 * s, c.text, lv * (1 - gone)):
                self.fill.setColor(argb(c.text, lv * (1 - gone)))
                cv.drawCircle(hx, hy, 2.6 * s, self.fill)

        # --- shots --------------------------------------------------------------------------------
        life = 6.0
        shots = self.events.shots_between(tick - life, tick)
        if shots:
            hurts = self.events.hurts_between(tick - life - 3, tick + 3)
            for sh in shots:
                age = (tick - sh.tick) / life
                ang = math.radians(sh.yaw)
                dx, dy = math.cos(ang), math.sin(ang)
                reach = 640.0
                for hu in hurts:
                    if hu.attacker == sh.pid and abs(hu.tick - sh.tick) <= 2:
                        reach = max(40.0, math.hypot(hu.x - sh.x, hu.y - sh.y))
                        break
                lv = level(sh.z)
                x0, y0 = pos(sh.x + dx * 26, sh.y + dy * 26)
                x1, y1 = pos(sh.x + dx * reach, sh.y + dy * reach)
                mine = sc.pov is not None and sh.pid == sc.pov.pid
                self.stroke.setStrokeWidth(max(1.0, (1.5 if mine else 1.2) * s))
                self.stroke.setColor(argb(c.text, (0.85 if mine else 0.6) * (1 - age) * lv))
                cv.drawLine(x0 + (x1 - x0) * age * 0.5, y0 + (y1 - y0) * age * 0.5, x1, y1, self.stroke)
                if age < 0.4:
                    self._sprite(cv, th.glow_warm, x0, y0, 9 * s, (1 - age / 0.4) * lv, self.plus)

        # --- deaths: kill lines, crosses, numbers -------------------------------------------------
        for m in sc.trace.marks:
            if m.frame > clip_frame:
                continue
            t = (clip_frame - m.frame) / fps  # output seconds since it happened
            vx, vy = rpos(m.x, m.y)
            lv = 1.0 if m.lower == lower else 0.32
            if m.ax is not None and m.ay is not None and (m.pov or t < 0.55):
                ax, ay = rpos(m.ax, m.ay)
                if m.pov:
                    hot = 1 - clamp(t / 0.5)
                    self.stroke.setStrokeWidth(max(1.0, (1.5 + 2.2 * hot) * s))
                    self.stroke.setColor(argb(blend(th.accent, "#ffffff", 0.7 * hot), (0.5 + 0.5 * hot) * lv))
                else:
                    self.stroke.setStrokeWidth(max(1.0, 1.4 * s))
                    self.stroke.setColor(argb(th.side_color(m.attacker_side), 0.7 * (1 - t / 0.55) * lv))
                cv.drawLine(ax, ay, vx, vy, self.stroke)
            if t < 0.5:
                p = t / 0.5
                self.stroke.setStrokeWidth(max(1.0, 2.2 * s * (1 - p)))
                self.stroke.setColor(argb(c.text, (1 - p) * lv))
                cv.drawCircle(vx, vy, (7 + (38 if m.pov else 22) * ease_out(p)) * s, self.stroke)
                if m.pov and t < 0.2:
                    self._sprite(cv, th.glow, vx, vy, 30 * s, (1 - t / 0.2) * lv, self.plus)
            grow = 1 + 1.5 * (1 - ease_out_back(clamp(t / 0.24), 2.0)) if t < 0.24 else 1.0
            d = 5.6 * s * grow
            for width, color in ((4.6 * s, argb(c.background, 0.75 * lv)),
                                 (2.3 * s, argb(blend(th.side_color(m.side), c.text, 0.12), lv))):
                self.stroke.setStrokeWidth(width)
                self.stroke.setColor(color)
                cv.drawLine(vx - d, vy - d, vx + d, vy + d, self.stroke)
                cv.drawLine(vx - d, vy + d, vx + d, vy - d, self.stroke)
            if m.pov and m.number:
                nx, ny, nr = vx + 11 * s, vy - 11 * s, 7.8 * s * min(1.0, grow)
                self.fill.setColor(argb(c.background, 0.9 * lv))
                cv.drawCircle(nx, ny, nr + 1.6 * s, self.fill)
                self.fill.setColor(argb(th.accent, lv))
                cv.drawCircle(nx, ny, nr, self.fill)
                th.ty.draw(cv, str(m.number), nx, ny + 4.3 * s, "number", 12.5 * s, argb(c.background, lv),
                           align="center")

        # --- players ------------------------------------------------------------------------------
        self._draw_players(cv, sc, tick, pos, level, upp)

    # ------------------------------------------------------------------ parts
    def _advance_route(self, sc: Scene, tick: float) -> None:
        """Extend the route polyline up to ``tick`` (called with rising ticks)."""
        upto = min(len(sc.route_xy), int((tick - sc.data.start) // ROUTE_STEP) + 1)
        routes = sc.trace.routes
        while sc.route_done < upto:
            i = sc.route_done
            x, y = sc.route_xy[i]
            low = bool(sc.route_lower[i])
            if np.isnan(x):
                sc.route_open = False
            elif sc.route_open and routes and routes[-1][1] == low:
                routes[-1][0].lineTo(float(x), float(y))
                sc.trace.grow(float(x), float(y))
            else:
                path = skia.Path()
                if sc.route_open and routes:  # changed floor: keep the line continuous
                    px, py = sc.route_xy[i - 1]
                    path.moveTo(float(px), float(py))
                    path.lineTo(float(x), float(y))
                else:
                    path.moveTo(float(x), float(y))
                routes.append((path, low))
                sc.route_open = True
                sc.trace.grow(float(x), float(y))
            sc.route_done += 1

    def _draw_bomb(self, cv: skia.Canvas, sc: Scene, tick: float, pos, level, upp: float) -> None:
        th, s, c, rnd, tr = self.th, self.s, self.th.c, sc.rnd, self.tickrate
        if rnd is None:
            return
        planted = rnd.plant_tick is not None and tick >= rnd.plant_tick
        if not planted:
            ground = self.events.bomb_on_ground(tick, rnd.start_tick)
            carried = any(t.has_c4[sc.data.index(tick)] and t.alive[sc.data.index(tick)]
                          for t in sc.data.tracks.values())
            if ground is not None and not carried and tick <= rnd.end_tick:
                x, y = pos(ground[0], ground[1])
                lv = level(ground[2])
                self.fill.setColor(argb(c.background, 0.8 * lv))
                cv.drawCircle(x, y, 9 * s, self.fill)
                if not self._icon(cv, "c4", x, y, 12 * s, th.side_color("T"), lv):
                    self.fill.setColor(argb(th.side_color("T"), lv))
                    cv.drawRect(skia.Rect.MakeXYWH(x - 4 * s, y - 4 * s, 8 * s, 8 * s), self.fill)
            return
        where = self.events.bomb_pos.get(rnd.plant_tick)
        if where is None:
            return
        x, y = pos(where[0], where[1])
        lv = level(where[2])
        if rnd.explode_tick is not None and tick >= rnd.explode_tick:
            p = clamp((tick - rnd.explode_tick) / (1.1 * tr))
            self._sprite(cv, th.glow_warm, x, y, (120 + 900 * ease_out(p)) * upp, (1 - p) ** 1.5, self.plus)
            self.stroke.setStrokeWidth(max(1.0, 3 * s * (1 - p)))
            self.stroke.setColor(argb("#ffb070", 1 - p))
            cv.drawCircle(x, y, 760 * upp * ease_out(p), self.stroke)
            return
        defused = rnd.defuse_tick is not None and tick >= rnd.defuse_tick
        color = c.positive if defused else c.negative
        if not defused:
            e = (tick - rnd.plant_tick) / tr
            beat = (e + 0.0009 * e ** 3) % 1.0
            self.stroke.setStrokeWidth(max(1.0, 1.8 * s))
            self.stroke.setColor(argb(color, (1 - beat) * 0.9 * lv))
            cv.drawCircle(x, y, (9 + 20 * ease_out(beat)) * s, self.stroke)
        self.fill.setColor(argb(c.background, 0.86 * lv))
        cv.drawCircle(x, y, 10.5 * s, self.fill)
        self.stroke.setStrokeWidth(max(1.0, 1.5 * s))
        self.stroke.setColor(argb(color, lv))
        cv.drawCircle(x, y, 10.5 * s, self.stroke)
        if not self._icon(cv, "c4", x, y, 13 * s, color, lv):
            self.fill.setColor(argb(color, lv))
            cv.drawRect(skia.Rect.MakeXYWH(x - 4.5 * s, y - 4.5 * s, 9 * s, 9 * s), self.fill)

    def _draw_players(self, cv: skia.Canvas, sc: Scene, tick: float, pos, level, upp: float) -> None:
        th, s, c, tr = self.th, self.s, self.th.c, self.tickrate
        pov_id = sc.pov.pid if sc.pov is not None else None
        rnd = sc.rnd
        hurts = self.events.hurts_between(tick - 12, tick)
        labels: list[tuple[float, float, float, float]] = []
        drawn: list[tuple[Track, float, float, float, float, bool]] = []
        for t in sorted(sc.data.tracks.values(), key=lambda t: t.pid == pov_id):  # watched player on top
            smp = sc.sample(t, tick)
            if smp is None or not t.alive[smp[4]]:
                continue
            x, y, z, yaw, i = smp
            sx, sy = pos(x, y)
            if not (-60 * s < sx < self.L.w + 60 * s and -60 * s < sy < self.L.h + 60 * s):
                continue
            is_pov = t.pid == pov_id
            lv = level(z)
            side = t.side(i)
            color = th.side_color(side)
            r = (11.0 if is_pov else 9.5) * s
            ang = math.radians(yaw)
            dx, dy = math.cos(ang), -math.sin(ang)
            if lv < 1.0:  # on the other floor: a hollow ghost
                self.stroke.setStrokeWidth(max(1.0, 1.8 * s))
                self.stroke.setColor(argb(color, 0.5))
                cv.drawCircle(sx, sy, r * 0.8, self.stroke)
                continue
            if is_pov:  # what he is looking at
                reach = clamp(430 * upp, r * 4.5, r * 11)
                for share, alpha in ((1.0, 0.07), (0.72, 0.07), (0.46, 0.08)):  # flat steps instead of a gradient
                    cone = skia.Path()
                    cone.moveTo(sx, sy)
                    q = reach * share
                    cone.arcTo(skia.Rect.MakeLTRB(sx - q, sy - q, sx + q, sy + q), -yaw - 37, 74, False)
                    cone.close()
                    self.fill.setColor(argb(c.text, alpha))
                    cv.drawPath(cone, self.fill)
            flash = 0.0
            left = sc.flash_left.get(t.pid)
            if left is not None and left[i] > 0:
                flash = clamp(float(left[i]) / max(0.6, float(sc.flash_total[t.pid][i])) * 1.6)
                self._sprite(cv, th.glow, sx, sy, r * 2.7, 0.75 * flash, self.plus)
            # pointer
            nose = skia.Path()
            nose.moveTo(sx + dx * r * 1.95, sy + dy * r * 1.95)
            nose.lineTo(sx - dy * r * 0.78 + dx * r * 0.62, sy + dx * r * 0.78 + dy * r * 0.62)
            nose.lineTo(sx + dy * r * 0.78 + dx * r * 0.62, sy - dx * r * 0.78 + dy * r * 0.62)
            nose.close()
            self.fill.setColor(argb(c.background, 0.9))
            cv.drawCircle(sx, sy, r + 1.7 * s, self.fill)
            self.fill.setColor(argb(th.side_bright.get(side or "", color)))
            cv.drawPath(nose, self.fill)
            # health: the rim of the marker is the gauge
            hp = max(0, min(100, int(t.health[i])))
            self.fill.setColor(argb(blend(color, c.background, 0.72)))
            cv.drawCircle(sx, sy, r, self.fill)
            rim = 2.7 * s
            self.stroke.setStrokeWidth(rim)
            self.stroke.setStrokeCap(skia.Paint.kButt_Cap)
            self.stroke.setColor(argb(c.negative if hp <= 30 else th.side_bright.get(side or "", color)))
            rr = r - rim / 2
            if hp >= 100:
                cv.drawCircle(sx, sy, rr, self.stroke)
            else:
                cv.drawArc(skia.Rect.MakeLTRB(sx - rr, sy - rr, sx + rr, sy + rr), -90, 360 * hp / 100, False,
                           self.stroke)
            self.stroke.setStrokeCap(skia.Paint.kRound_Cap)
            self.fill.setColor(argb(color))
            cv.drawCircle(sx, sy, r - rim - 0.9 * s, self.fill)
            hit = 0.0
            for hu in hurts:
                if hu.victim == t.pid:
                    hit = max(hit, 1 - (tick - hu.tick) / 12)
            white = max(hit * 0.85, flash * 0.9)
            if white > 0.02:
                self.fill.setColor(argb("#ffffff", white))
                cv.drawCircle(sx, sy, r, self.fill)
            if is_pov:
                self.stroke.setStrokeWidth(max(1.0, 1.7 * s))
                self.stroke.setColor(argb(c.text, 0.95))
                cv.drawCircle(sx, sy, r + 3.4 * s, self.stroke)
            # defusing / planting: a progress ring in the side's colour
            busy = sc.busy_since.get(t.pid)
            progress = -1.0
            if busy is not None and busy[i] >= 0:
                need = 5.0 if t.defuser[i] else 10.0
                started = sc.data.start + int(busy[i])
                if busy[i] == 0 and rnd is not None and rnd.defuse_tick is not None and rnd.defuser is not None \
                        and rnd.defuser.steamid == t.pid and rnd.defuse_tick >= tick:
                    started = rnd.defuse_tick - int(need * tr)  # it began before this segment
                progress = clamp((tick - started) / (need * tr))
            elif rnd is not None and rnd.plant_tick is not None and rnd.planter is not None \
                    and rnd.planter.steamid == t.pid and 0 <= rnd.plant_tick - tick <= PLANT_SECONDS * tr:
                progress = 1 - (rnd.plant_tick - tick) / (PLANT_SECONDS * tr)
            if progress >= 0:
                pr = r + 7.5 * s
                self.stroke.setStrokeWidth(3 * s)
                self.stroke.setColor(argb(c.background, 0.7))
                cv.drawCircle(sx, sy, pr, self.stroke)
                self.stroke.setColor(argb(th.side_bright.get(side or "", color)))
                cv.drawArc(skia.Rect.MakeLTRB(sx - pr, sy - pr, sx + pr, sy + pr), -90, 360 * progress, False,
                           self.stroke)
            if t.has_c4[i]:
                bx, by = sx + r * 0.95, sy + r * 0.85
                self.fill.setColor(argb(c.background, 0.92))
                cv.drawCircle(bx, by, 6.6 * s, self.fill)
                if not self._icon(cv, "c4", bx, by, 9.5 * s, c.text):
                    self.fill.setColor(argb(c.negative))
                    cv.drawRect(skia.Rect.MakeXYWH(bx - 3 * s, by - 3 * s, 6 * s, 6 * s), self.fill)
            drawn.append((t, sx, sy, r, 1.0, is_pov))
        # names last, the watched player's first; a name that would sit on another one is left out
        for t, sx, sy, r, _lv, is_pov in reversed(drawn):
            size = (16.5 if is_pov else 13.5) * s
            face = "strong" if is_pov else "name"
            text = th.ty.fit(clean_name(t.name), face, size, (190 if is_pov else 132) * s)
            w = th.ty.measure(text, face, size)
            box = (sx - w / 2 - 2 * s, sy - r - 9 * s - size, sx + w / 2 + 2 * s, sy - r - 5 * s)
            if any(box[0] < o[2] and o[0] < box[2] and box[1] < o[3] and o[1] < box[3] for o in labels):
                continue
            labels.append(box)
            th.ty.draw(cv, text, sx, sy - r - 8 * s - (3 * s if is_pov else 0), face, size,
                       argb(c.text, 1.0 if is_pov else 0.9), align="center", shadow=0.85)
