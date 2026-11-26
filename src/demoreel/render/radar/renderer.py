"""2D tactical replay renderer.

The map fills the frame and a follow camera composes the action on it; the HUD around it is the game's
spectator furniture in the brand's identity (scorebug, rosters, the game's kill feed). The watched player's
route and kills stay drawn on the map, so a clip ends on a diagram of the play. Frames are drawn with skia
and piped straight into ffmpeg: no game, GPU or display is needed.

The layout is designed per aspect ratio (16:9, 9:16, 1:1) from ``options.width`` / ``options.height``; see
``layout.py``. The design rationale lives in ``docs/design/replay-2d-plan.md``.

Renderer options (``RenderOptions.extra``):
    camera: "follow" (default: frame the POV player and who he is fighting) | "full" (whole map)
    zoom:   how far the follow camera may zoom in, relative to the whole map (default: by distance,
            about 2100 world units across, so a small map and a large one feel the same)
    brand:  slug of the brand kit to theme with (default: the instance's active kit)
"""

from __future__ import annotations

import bisect
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import skia

from ...analysis.schema import KillEvent, MatchAnalysis, RoundSummary
from ...brand import brand_for
from .. import ffmpeg, maps
from ..base import (
    Availability,
    Clip,
    ClipSegment,
    RenderCancelled,
    RenderError,
    RenderJob,
    RenderResult,
    is_camera_view,
    player_view,
)
from . import camera as cam
from .canvas import argb, clamp, ease_out
from .data import Events, SegmentData, Track, load_events, load_segment
from .hud import BugState, Hud, PlateInfo, RosterRow
from .layout import Rect, choose_layout
from .theme import Theme, map_title
from .timeline import first_frame_at, frame_ticks
from .world import BOMB_SECONDS, ROUTE_STEP, Mark, Scene, Trace, World, blindness, run_starts

ROUND_SECONDS = 115
CARD_SECONDS = 1.5
INTRO_SECONDS = 1.15  # the push-in from the whole map at the start of a clip
OUTRO_SECONDS = 1.5  # the pull-back to the whole drawing at its end
VIEW_UNITS = 2100.0  # world units the follow camera shows across the shorter side when nothing else matters
LOOK_AHEAD = 230.0  # world units of extra room in the direction the player looks
CUT_FRAMES = 5
FEED_HOLD_S = 6.0


@dataclass
class _SegmentPlan:
    seg: ClipSegment
    shown: np.ndarray  # tick drawn on the map, per frame
    linear: np.ndarray  # tick of the HUD clock, per frame
    offset: int  # clip frame of this segment's first frame


@dataclass
class _ClipPlan:
    clip: Clip
    plate: PlateInfo
    pov: str | None
    round_text: str
    segments: list[_SegmentPlan]
    kill_ticks: list[int]
    kill_frames: list[int] = field(default_factory=list)  # clip frames of the watched player's kills
    frames: int = 0
    trace: Trace = field(default_factory=Trace)


class _Session:
    """One render job: owns the skia surface, the ffmpeg pipe and the sequencing."""

    def __init__(self, job: RenderJob):
        t0 = time.perf_counter()
        self.job = job
        o = job.options
        self.w, self.h, self.fps = max(2, o.width - o.width % 2), max(2, o.height - o.height % 2), max(1, int(o.fps))
        self.analysis: MatchAnalysis = job.analysis
        self.tickrate = float(self.analysis.tickrate or 64)
        self.camera_mode = str(o.extra.get("camera", "follow"))
        self.zoom: float | None = None  # None = by distance: about VIEW_UNITS of the map across the frame
        try:
            if o.extra.get("zoom") is not None:
                self.zoom = clamp(float(o.extra["zoom"]), 1.0, 8.0)
        except (TypeError, ValueError):
            self.zoom = None
        slug = o.extra.get("brand")
        self.brand = brand_for(slug if isinstance(slug, str) else None)
        self.layout = choose_layout(self.w, self.h)
        self.theme = Theme(self.brand, self.w, self.h)
        self.surface = skia.Surface(self.w, self.h)
        self.canvas = self.surface.getCanvas()
        self.pixmap = skia.Pixmap()
        self.direct = self.surface.peekPixels(self.pixmap)
        self.color_type = self.surface.imageInfo().colorType()
        self.pix_fmt = "bgra" if self.color_type == skia.kBGRA_8888_ColorType else "rgba"
        self.map = maps.map_info(self.analysis.map)
        self.plans = [self._plan_clip(c) for c in job.clips]
        windows = [(sp.seg.start_tick, sp.seg.end_tick) for cp in self.plans for sp in cp.segments]
        self.events: Events = load_events(job.demo_path, windows)
        self.world = World(self.theme, self.layout, self.events, self.analysis.map, self.tickrate)
        self.hud = Hud(self.theme, self.layout)
        self.order = {p.steamid: i for i, p in enumerate(self.analysis.players)}
        self.team_names = {self.analysis.team_a.key: self.analysis.team_a.name,
                           self.analysis.team_b.key: self.analysis.team_b.name}
        self.all_kills: list[KillEvent] = sorted((k for r in self.analysis.rounds for k in r.kills),
                                                 key=lambda k: k.tick)
        self.kill_ticks = [k.tick for k in self.all_kills]
        self.writer = None
        self.frames_written = 0
        self.frames_total = 1
        self.thumb_done = False
        self.paint = skia.Paint(AntiAlias=True)
        self.spent = {"load": time.perf_counter() - t0, "draw": 0.0, "encode": 0.0}

    # ------------------------------------------------------------------ planning (no demo access)
    def _plan_clip(self, clip: Clip) -> _ClipPlan:
        from ...compose.overlay import clip_info  # the same words as the first-person HUD

        info = clip_info(clip, self.analysis, self.brand.locale)
        # The clip's player: a fixed camera (``cam:…``) is a point of view, but nobody's play.
        pov = next((player_view(s.pov_steamid) for s in clip.segments if player_view(s.pov_steamid)), None)
        segments: list[_SegmentPlan] = []
        offset = 0
        kill_frames: list[int] = []
        for seg in clip.segments:
            end = max(seg.end_tick, seg.start_tick + 1)
            mine = ([k for k in info.kill_ticks if seg.start_tick < k <= end]
                    if player_view(seg.pov_steamid) == pov else [])
            shown, linear = frame_ticks(seg.start_tick, end, seg.speed, self.tickrate, self.fps, mine)
            kill_frames.extend(offset + first_frame_at(shown, k) for k in mine)
            segments.append(_SegmentPlan(seg, shown, linear, offset))
            offset += len(shown)
        plate = PlateInfo(player=info.player or (clip.title or ""), label=info.label, detail=info.detail,
                          kills_total=len(kill_frames))
        return _ClipPlan(clip, plate, pov, info.round_text, segments, sorted(info.kill_ticks), sorted(kill_frames),
                         offset)

    def round_at(self, tick: int) -> RoundSummary | None:
        return next((r for r in self.analysis.rounds if r.start_tick <= tick <= max(r.official_end_tick, r.end_tick)),
                    None)

    # ------------------------------------------------------------------ plumbing
    def emit(self, repeat: int = 1) -> None:
        if self.writer is None:
            self.writer = ffmpeg.open_rgb_writer(self.job.out_path, self.w, self.h, self.fps, self.job.options.crf,
                                                 self.pix_fmt)
        t0 = time.perf_counter()
        buf = memoryview(self.pixmap).cast("B") if self.direct else self.surface.toarray(colorType=self.color_type).tobytes()
        for _ in range(repeat):
            try:
                self.writer.stdin.write(buf)
            except (BrokenPipeError, OSError):
                ffmpeg.close_writer(self.writer)
                raise
        self.frames_written += repeat
        self.spent["encode"] += time.perf_counter() - t0  # time blocked on the pipe = ffmpeg not keeping up
        if self.frames_written % 30 < repeat:
            self.job.progress(min(0.99, self.frames_written / self.frames_total), "Rendering frames")
            if self.job.cancelled():
                raise RenderCancelled("Render canceled.")

    def save_thumbnail(self) -> None:
        if self.thumb_done or self.job.thumbnail_path is None:
            return
        self.thumb_done = True
        self.job.thumbnail_path.parent.mkdir(parents=True, exist_ok=True)
        self.surface.makeImageSnapshot().save(str(self.job.thumbnail_path), skia.kJPEG, 90)

    def wash(self, hex_color: str, alpha: float) -> None:
        if alpha > 0.003:
            self.paint.setColor(argb(hex_color, alpha))
            self.canvas.drawRect(skia.Rect.MakeWH(self.w, self.h), self.paint)

    # ------------------------------------------------------------------ title card
    def card(self, cp: _ClipPlan, index: int, total: int) -> None:
        n = max(1, int(CARD_SECONDS * self.fps))
        cv, L = self.canvas, self.layout
        image = self.world.radar_image(False)[1]
        line = ", ".join(x for x in (cp.round_text, map_title(self.analysis.map)) if x)
        settled = int(0.62 * self.fps)
        for i in range(min(n, settled + 1)):
            t = i / self.fps
            cv.clear(argb(self.brand.colors.background))
            size = max(L.w, L.h) * (1.16 + 0.03 * ease_out(clamp(t / 0.6)))  # the map, large and dim, drifting
            self.paint.setAlphaf(0.34)
            cv.drawImageRect(image, skia.Rect.MakeXYWH(L.w * 0.62 - size / 2, L.h / 2 - size / 2, size, size),
                             self.theme.linear, self.paint)
            self.paint.setAlphaf(1.0)
            cv.drawImage(self.theme.vignette, 0, 0)
            self.hud.card(cv, t, CARD_SECONDS, cp.plate, line, index, total)
            self.hud.logo(cv)
            if i < CUT_FRAMES:
                self.wash(self.brand.colors.background, 1 - (i + 1) / (CUT_FRAMES + 1))
            self.emit(n - settled if i == settled else 1)

    # ------------------------------------------------------------------ one segment
    def _radar(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        info = self.map
        return np.stack([(x - info.pos_x) / info.scale, (info.pos_y - y) / info.scale], axis=-1).astype(np.float64)

    def _scene(self, cp: _ClipPlan, sp: _SegmentPlan, data: SegmentData) -> Scene:
        seg, tr = sp.seg, self.tickrate
        who = player_view(seg.pov_steamid)
        pov = data.tracks.get(who) if who else None
        flash_left, flash_total, busy = {}, {}, {}
        for pid, t in data.tracks.items():
            if (t.flash > 0.05).any():
                flash_left[pid], flash_total[pid] = blindness(t.flash, tr)
            if t.defusing.any():
                busy[pid] = run_starts(t.defusing & t.alive)
        if pov is not None:
            pts = np.arange(0, data.n, ROUTE_STEP)
            route = self._radar(pov.x[pts], pov.y[pts])
            route[~(pov.present[pts] & pov.alive[pts])] = np.nan
            route_lower = np.array([self.map.is_lower(float(z)) for z in pov.z[pts]], dtype=bool)
        else:
            route, route_lower = np.zeros((0, 2)), np.zeros(0, dtype=bool)
        flights = [f for f in self.events.flights if f.end >= seg.start_tick - 10 and f.start <= seg.end_tick]
        sc = Scene(seg=seg, data=data, rnd=self.round_at(seg.start_tick), info=self.map, tickrate=tr, trace=cp.trace,
                   pov=pov, flights=flights, flash_left=flash_left, flash_total=flash_total, busy_since=busy,
                   route_xy=route, route_lower=route_lower)
        # deaths inside this segment become marks of the clip's trace
        numbers = {t: i + 1 for i, t in enumerate(cp.kill_ticks)}
        lo, hi = bisect.bisect_right(self.kill_ticks, seg.start_tick), bisect.bisect_right(self.kill_ticks, seg.end_tick)
        for k in self.all_kills[lo:hi]:
            vt = data.tracks.get(k.victim.steamid)
            i = max(0, min(k.tick - data.start - 1, data.n - 1))
            if vt is None or not vt.present[i]:
                continue
            vx, vy = self.map.to_radar(float(vt.x[i]), float(vt.y[i]))
            ax = ay = None
            at = data.tracks.get(k.attacker.steamid) if k.attacker is not None and not k.is_suicide else None
            if at is not None and at.present[i]:
                ax, ay = self.map.to_radar(float(at.x[i]), float(at.y[i]))
            mine = who is not None and k.attacker is not None and k.attacker.steamid == who and k.tick in numbers
            cp.trace.marks.append(Mark(
                tick=k.tick, x=vx, y=vy, lower=self.map.is_lower(float(vt.z[i])), side=k.victim.side, ax=ax, ay=ay,
                attacker_side=k.attacker.side if k.attacker else None, pov=mine,
                number=numbers.get(k.tick, 0) if mine else 0, frame=sp.offset + first_frame_at(sp.shown, k.tick)))
            if mine:
                cp.trace.grow(vx, vy)
                if ax is not None:
                    cp.trace.grow(ax, ay)
        return sc

    def _last_alive(self, t: Track, idx: np.ndarray) -> np.ndarray:
        """Radar positions of a player per frame, frozen where he died; NaN before he is known."""
        ok = t.present & t.alive
        last = np.where(ok, np.arange(len(ok)), -1)
        np.maximum.accumulate(last, out=last)
        at = last[idx]
        xy = self._radar(t.x[np.maximum(at, 0)], t.y[np.maximum(at, 0)])
        xy[at < 0] = np.nan
        return xy

    def _camera(self, cp: _ClipPlan, sp: _SegmentPlan, sc: Scene, first: bool,
                last: bool) -> tuple[np.ndarray, np.ndarray, Rect]:
        """(views, lower_mix, focus): one view and floor mix per frame, and the rectangle the views refer to."""
        L, data, tr, fps = self.layout, sc.data, self.tickrate, self.fps
        n = len(sp.shown)
        idx = np.clip(np.floor(sp.shown - data.start).astype(np.int64), 0, data.n - 1)
        whole = np.tile(cam.full_view(L.focus_full.w, L.focus_full.h), (n, 1))
        full = cam.full_view(L.focus.w, L.focus.h)
        pov = sc.pov
        if pov is None:
            return whole, np.zeros(n), L.focus_full
        # which floor: the watched player's, without flicker on stairs
        last_ok = np.where(pov.present & pov.alive, np.arange(data.n), -1)
        np.maximum.accumulate(last_ok, out=last_ok)
        z = pov.z[np.maximum(last_ok[idx], 0)]
        lower = cam.majority(np.array([self.map.is_lower(float(v)) for v in z], dtype=bool), int(0.3 * fps))
        lower_mix = np.clip(cam.smooth(lower.astype(np.float64), 0.09 * fps), 0, 1)
        if self.camera_mode == "full":
            return whole, lower_mix, L.focus_full
        # who is part of the action, and when (the demo is known in full: the camera can anticipate)
        windows: dict[str, list[tuple[float, float]]] = {}
        lo, hi = bisect.bisect_left(self.kill_ticks, data.start - 3 * tr), bisect.bisect_right(self.kill_ticks, data.end + 3 * tr)
        for k in self.all_kills[lo:hi]:
            if k.attacker is None:
                continue
            if k.attacker.steamid == pov.pid and k.victim.steamid != pov.pid:
                windows.setdefault(k.victim.steamid, []).append((k.tick - 2.6 * tr, k.tick + 0.45 * tr))
            elif k.victim.steamid == pov.pid:
                windows.setdefault(k.attacker.steamid, []).append((k.tick - 2.6 * tr, k.tick + 0.2 * tr))
        for hu in self.events.hurts_between(data.start - 2 * tr, data.end + 2 * tr):
            other = hu.victim if hu.attacker == pov.pid else hu.attacker if hu.victim == pov.pid else None
            if other and other != pov.pid:
                windows.setdefault(other, []).append((hu.tick - 1.6 * tr, hu.tick + 1.0 * tr))
        others = []
        for pid, spans in windows.items():
            t = data.tracks.get(pid)
            if t is None:
                continue
            active = np.zeros(n, dtype=bool)
            for a, b in spans:
                active |= (sp.shown >= a) & (sp.shown <= b)
            if active.any():
                xy = self._last_alive(t, idx)
                xy[~active] = np.nan
                others.append(xy)
        pov_xy = self._last_alive(pov, idx)
        yaw = np.radians(pov.yaw[np.maximum(last_ok[idx], 0)].astype(np.float64))
        ahead = 2 * LOOK_AHEAD / self.map.scale  # a point ahead of him: the frame leaves room where he looks
        others.append(pov_xy + np.stack([np.cos(yaw), -np.sin(yaw)], axis=1) * ahead)
        margin = 380 / self.map.scale
        k_fit = cam.fit_scale(L.focus.w, L.focus.h)
        k_max = k_fit * self.zoom if self.zoom else min(L.focus.w, L.focus.h) / (VIEW_UNITS / self.map.scale)
        views = cam.follow_path(pov_xy, others, L.focus.w, L.focus.h, k_max=max(k_fit, k_max), margin=margin, fps=fps)
        if first:  # open on the whole map, push in to the player
            m = min(n, int(INTRO_SECONDS * fps))
            for i in range(m):
                views[i] = cam.blend_views(full, views[i], cam.ease_in_out(0.1 + 0.9 * i / max(1, m)))
        if last and n > 4:  # close on the whole drawing
            trace = cp.trace
            lo_xy, hi_xy = trace.lo.copy(), trace.hi.copy()
            if len(sc.route_xy) and not np.isnan(sc.route_xy[:, 0]).all():
                lo_xy = np.fmin(lo_xy, np.nanmin(sc.route_xy, axis=0))
                hi_xy = np.fmax(hi_xy, np.nanmax(sc.route_xy, axis=0))
            if np.isfinite(lo_xy).all() and np.isfinite(hi_xy).all():
                m = min(n, int(OUTRO_SECONDS * fps))
                wide = cam.frame_box(lo_xy, hi_xy, L.focus.w, L.focus.h, margin * 0.8, k_fit, float(views[-1, 2]))
                for i in range(n - m, n):
                    e = cam.ease_in_out((i - (n - m)) / max(1.0, m * 0.8))
                    views[i] = cam.blend_views(views[i], wide, e)
        # a kill of his punches the zoom for an instant
        for kf in cp.kill_frames:
            f0 = kf - sp.offset
            if 0 <= f0 < n:
                reach = np.arange(f0, min(n, f0 + int(0.7 * fps)))
                views[reach, 2] *= 1 + 0.06 * np.exp(-(reach - f0) / fps / 0.13)
        return views, lower_mix, L.focus

    def _rows(self, tracks: list[Track], i: int, pov: str | None) -> list[RosterRow]:
        rows = []
        for t in tracks:
            alive = bool(t.present[i] and t.alive[i])
            side = t.side(i) or t.main_side()
            rows.append(RosterRow(name=t.name, alive=alive, hp=max(0, min(100, int(t.health[i]))) if alive else 0,
                                  weapon=t.weapon[i] if alive else "", has_c4=bool(alive and t.has_c4[i]),
                                  has_kit=bool(alive and t.defuser[i] and side == "CT"), is_pov=t.pid == pov, side=side))
        return rows

    def _bug(self, sc: Scene, tick: float, left_side: str, left: list[RosterRow], right: list[RosterRow],
             speed: float) -> BugState:
        rnd, tr = sc.rnd, self.tickrate
        right_side = "T" if left_side == "CT" else "CT"
        st = BugState(team_left="", team_right="", side_left=left_side, side_right=right_side, score_left=0,
                      score_right=0, alive_left=sum(r.alive for r in left), alive_right=sum(r.alive for r in right),
                      total_left=len(left), total_right=len(right), clock="", speed=speed)
        if rnd is None:
            return st
        over = tick > rnd.end_tick
        score = {"team_a": rnd.score_team_a, "team_b": rnd.score_team_b}
        if not over:  # the score while this round is being played (the previous round may not be in the demo)
            score[rnd.winner_team] -= 1
        key_left = rnd.ct_team if left_side == "CT" else rnd.t_team
        key_right = rnd.t_team if left_side == "CT" else rnd.ct_team
        st.team_left, st.team_right = self.team_names.get(key_left, ""), self.team_names.get(key_right, "")
        st.score_left, st.score_right = max(0, score.get(key_left, 0)), max(0, score.get(key_right, 0))
        now = min(tick, float(rnd.end_tick))
        if rnd.plant_tick is not None and now >= rnd.plant_tick:
            left_s = max(0.0, BOMB_SECONDS - (now - rnd.plant_tick) / tr)
            st.bomb, st.clock, st.fuse = True, str(int(math.ceil(left_s))), left_s / BOMB_SECONDS
            i = sc.data.index(tick)
            for t in sc.data.tracks.values():
                since = sc.busy_since.get(t.pid)
                if since is not None and since[i] >= 0 and not over:
                    need = 5.0 if t.defuser[i] else 10.0
                    started = sc.data.start + int(since[i])
                    if since[i] == 0 and rnd.defuse_tick is not None and rnd.defuser is not None \
                            and rnd.defuser.steamid == t.pid:
                        started = rnd.defuse_tick - int(need * tr)
                    st.defuse = clamp((tick - started) / (need * tr))
            if over and rnd.defuse_tick is not None:
                st.defuse, st.bomb_safe = 1.0, True
        else:
            ref = rnd.freeze_end_tick
            left_s = (ref - now) / tr if now < ref else max(0.0, ROUND_SECONDS - (now - ref) / tr)
            st.clock = f"{int(left_s) // 60}:{int(left_s) % 60:02d}"
        words = self.theme.words
        if over:
            st.tab = words.get(rnd.reason) or words["time" if "time" in rnd.reason else "won"]
            st.tab_side = rnd.winner_side
            st.since_end = (tick - rnd.end_tick) / tr
            st.winner_left = rnd.winner_side == left_side
        else:
            st.tab = words["round"].format(n=rnd.number)
        return st

    def render_segment(self, cp: _ClipPlan, j: int, thumb_frame: int | None, after_card: bool) -> None:
        sp = cp.segments[j]
        seg, tr, fps = sp.seg, self.tickrate, self.fps
        t0 = time.perf_counter()
        data = load_segment(self.job.demo_path, seg.start_tick, max(seg.end_tick, seg.start_tick + 1))
        if self.map is None:
            self.map = _autofit(self.analysis.map, data)
        sc = self._scene(cp, sp, data)
        first, last = j == 0, j == len(cp.segments) - 1
        views, lower_mix, focus = self._camera(cp, sp, sc, first, last)
        self.spent["load"] += time.perf_counter() - t0
        t_draw = time.perf_counter() - self.spent["encode"]
        n = len(sp.shown)
        pov = player_view(seg.pov_steamid)
        rnd = sc.rnd
        # the watched player's team is the left one (as in the first-person HUD); CT without a POV
        left_side = "CT"
        if pov and rnd is not None and pov in rnd.t_players:
            left_side = "T"
        elif pov and sc.pov is not None and (rnd is None or pov not in rnd.ct_players):
            left_side = sc.pov.main_side() or "CT"
        ordered = sorted(data.tracks.values(), key=lambda t: (self.order.get(t.pid, 999), t.name.lower()))
        left_tracks = [t for t in ordered if t.main_side() == left_side]
        right_tracks = [t for t in ordered if t.main_side() not in (left_side, None)]
        lo = bisect.bisect_left(self.kill_ticks, seg.start_tick - (FEED_HOLD_S + 1) * tr)
        feed = self.all_kills[lo:bisect.bisect_right(self.kill_ticks, seg.end_tick)]
        slow_seg = seg.speed < 0.999
        cv = self.canvas
        for i in range(n):
            tick, lin = float(sp.shown[i]), float(sp.linear[i])
            clip_frame = sp.offset + i
            t_seg, t_seg_left = i / fps, (n - 1 - i) / fps
            slow = clamp(min(t_seg / 0.22, t_seg_left / 0.22)) if slow_seg else 0.0
            self.world.draw(cv, sc, tick, views[i], focus, float(lower_mix[i]), clip_frame, fps, slow)
            self.hud.letterbox(cv, slow)
            it = data.index(lin)
            left, right = self._rows(left_tracks, it, pov), self._rows(right_tracks, it, pov)
            self.hud.rosters(cv, left, right)
            self.hud.scorebug(cv, self._bug(sc, lin, left_side, left, right, seg.speed))
            self.hud.killfeed(cv, feed, lin, tr, pov)
            self.hud.logo(cv)
            t_clip, t_left = clip_frame / fps, (cp.frames - 1 - clip_frame) / fps
            done = bisect.bisect_right(cp.kill_frames, clip_frame)
            since = (clip_frame - cp.kill_frames[done - 1]) / fps if done else None
            self.hud.kill_counter(cv, cp.plate, done, since, t_clip, t_left)
            self.hud.caption(cv, seg.caption, clamp(min(t_seg / 0.15, t_seg_left / 0.15 + 0.2)))
            self.hud.nameplate(cv, cp.plate, t_clip)
            if i < CUT_FRAMES:
                if first and not after_card:  # the clip opens out of the dark
                    self.wash(self.brand.colors.background, 1 - (i + 1) / (CUT_FRAMES + 1))
                else:  # a jump in time: the same flash as the first-person HUD
                    self.wash("#fff6e8", 0.9 * (1 - i / CUT_FRAMES) ** 1.6)
            if thumb_frame is not None and clip_frame >= thumb_frame:
                self.save_thumbnail()
            self.emit()
        self.spent["draw"] += time.perf_counter() - self.spent["encode"] - t_draw

    # ------------------------------------------------------------------ the job
    def run(self) -> RenderResult:
        plans = self.plans
        show_cards = self.job.options.title_cards and len(plans) > 1
        total = sum(cp.frames for cp in plans)
        if show_cards:
            total += len(plans) * max(1, int(CARD_SECONDS * self.fps))
        self.frames_total = max(1, total)
        try:
            for n, cp in enumerate(plans):
                if show_cards:
                    self.card(cp, n, len(plans))
                thumb = None
                if n == 0:  # the poster is a kill: the last one of the first clip, a beat after the impact
                    thumb = (min(cp.frames - 1, cp.kill_frames[-1] + int(0.24 * self.fps)) if cp.kill_frames
                             else int(cp.frames * 0.55))
                for j in range(len(cp.segments)):
                    self.render_segment(cp, j, thumb, show_cards)
            if not self.thumb_done:
                self.save_thumbnail()
        except BaseException:
            if self.writer is not None:
                try:
                    self.writer.stdin.close()
                except OSError:
                    pass
                self.writer.kill()
                self.writer.wait()  # on Windows the output file stays locked until ffmpeg is really gone
            raise
        if self.writer is None:
            raise RenderError("Nothing to render: the clips have no segments.")
        ffmpeg.close_writer(self.writer, "radar video encode")
        details = {"frames": self.frames_written, "layout": self.layout.kind,
                   "seconds": {k: round(v, 2) for k, v in self.spent.items()}}
        notes = camera_notes(self.job.clips)
        if notes:
            details["warnings"] = notes
        return RenderResult(duration_s=self.frames_written / self.fps, width=self.w, height=self.h, fps=self.fps,
                            details=details)


def camera_notes(clips: list[Clip]) -> list[str]:
    """What a 2D replay shows differently from the request: segments set to a fixed camera, which only the game
    can film from. The replay shows the whole map there."""
    notes = []
    for c, clip in enumerate(clips):
        segs = [str(j) for j, s in enumerate(clip.segments) if is_camera_view(s.pov_steamid)]
        if not segs:
            continue
        where = f"Segment {segs[0]}" if len(segs) == 1 else f"Segments {', '.join(segs)}"
        if len(clips) > 1:
            where += f" of clip {c + 1}"
        notes.append(f"{where} {'is' if len(segs) == 1 else 'are'} watched from a fixed camera, which only the "
                     "in-game renderer can film: this 2D replay shows the whole map there. Render with "
                     "renderer='ingame' to see the camera's view.")
    return notes


def _autofit(name: str, data: SegmentData) -> maps.MapInfo:
    """Unknown map: derive a calibration that fits the positions we see."""
    xs = np.concatenate([t.x[t.present] for t in data.tracks.values()] or [np.array([0.0, 1.0])])
    ys = np.concatenate([t.y[t.present] for t in data.tracks.values()] or [np.array([0.0, 1.0])])
    if len(xs) == 0:
        xs = ys = np.array([0.0, 1.0])
    span = max(float(xs.max() - xs.min()), float(ys.max() - ys.min()), 1000.0) * 1.3
    cx, cy = float(xs.max() + xs.min()) / 2, float(ys.max() + ys.min()) / 2
    return maps.MapInfo(name, cx - span / 2, cy + span / 2, span / maps.RADAR_SIZE)


class RadarRenderer:
    name = "radar"
    description = ("2D tactical replay: the map full frame with a follow camera, spectator-style scorebug, rosters "
                   "and the game's kill feed, the player's route and kills drawn on the map. Runs anywhere: no "
                   "game, GPU or display needed. Layouts for 16:9, 9:16 and 1:1 are picked from width/height. "
                   "Options: extra.camera = follow|full, extra.zoom = 1.0-8.0, extra.brand = kit slug.")

    def availability(self) -> Availability:
        try:
            path = ffmpeg.ffmpeg_path()
        except Exception as exc:  # noqa: BLE001
            return Availability(False, str(exc))
        return Availability(True, "", {"ffmpeg": path})

    def render(self, job: RenderJob) -> RenderResult:
        if not Path(job.demo_path).exists():
            raise RenderError("The demo file of this match is no longer stored on the server.")
        return _Session(job).run()
