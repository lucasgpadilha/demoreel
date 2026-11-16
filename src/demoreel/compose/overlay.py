"""Retime captured footage and draw the HUD over it.

Input: a *master* — the raw first-person capture, possibly at a higher frame rate than the output, plus
the list of tick spans it covers. Output: the body video at the delivery frame rate, where each highlight
segment plays at its own speed (real slow motion when the master has the frames for it), with the branded
HUD, captions and cut flashes, and the game audio re-timed to match.

This is also where the play is *edited*: quiet stretches between kills run fast (speed ramp), the picture
holds for a moment on the decisive kill (freeze-frame), vertical videos push in on every kill, a replay from
another point of view gets its own sparse HUD. All of it is decided by :func:`timeline`, which says which demo
tick every output frame shows; video, audio, HUD and the kill marks handed to callers are all read from it,
so they cannot disagree.

Because this stage only reads the master, changing a caption, a speed, the nameplate or the whole theme
never needs the game again.
"""

from __future__ import annotations

import bisect
import math
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import skia
from PIL import Image as PILImage

from ..analysis.chat import reactions
from ..analysis.schema import KillEvent, MatchAnalysis, RoundSummary
from ..brand import Brand
from ..render import ffmpeg
from ..render.base import Clip, ClipSegment, RenderCancelled, RenderError
from . import sfx
from .hud import ChatLine, ClipInfo, Hud, HudState

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CUT_FLASH_FRAMES = 5
SLOW_EASE_S = 0.22

# Speed ramp. Measured on twenty matches, half of the time between two kills of a play is the player
# repositioning with nothing fired. Those stretches are played fast instead of being cut out (a cut loses
# where he went). What is never touched: the lead-in to the first kill, the second before and the 0.6 s after
# every kill, a short window around every shot / hit / damage taken, everything after the last kill, and any
# pair of kills less than 3 s apart (that is one exchange).
RAMP_KILL_BEFORE_S = 1.0
RAMP_KILL_AFTER_S = 0.6
RAMP_ACTION_BEFORE_S = 0.4
RAMP_ACTION_AFTER_S = 0.6
RAMP_MIN_KILL_GAP_S = 3.0
RAMP_MIN_S = 1.5  # a quiet stretch shorter than this is not worth a change of speed
RAMP_SPEED = 3.0
RAMP_LONG_S = 4.0  # ... and from this long on it runs at RAMP_LONG_SPEED
RAMP_LONG_SPEED = 4.0
RAMP_EASE_S = 0.2  # the speed glides up and down over this long instead of stepping

# Freeze-frame on the decisive kill (the last one of the clip): the picture holds, punches in a little, and
# the play's label lands. The game sound makes room for an impact.
FREEZE_S = 0.35
SEEK_AHEAD_S = 6.0  # further ahead than this in the master, a new decoder is quicker than skipping frames
FREEZE_DELAY_S = 0.05  # the frame held is this long after the kill tick: the hit is on screen by then
FREEZE_ZOOM = 0.04

# Vertical videos push in on every kill: the window is narrow and the enemy is small in it.
PUNCH_IN = 1.12
PUNCH_LEAD_S = 0.15
PUNCH_HOLD_S = 0.12
PUNCH_TAIL_S = 0.5

# Chat reactions enter during the post-roll of the play.
CHAT_S = 2.2
CHAT_MIN_S = 0.9  # not worth showing for less than this
CHAT_LINES = 3


@dataclass
class Span:
    """A continuous stretch of the master: ``frames`` frames starting at ``t0`` seconds, covering these ticks.

    ``pov`` is the player the stretch was recorded from, when known. A master often holds the same ticks twice
    (the play, and the victim's view of it for the replay): the point of view is what tells them apart."""

    t0: float
    start_tick: int
    end_tick: int
    pov: str | None = None


@dataclass
class _Piece:
    """A stretch of a shot with one kind of timing. ``play`` runs at the shot's own speed, ``fast`` covers its
    ticks in fewer frames (speed ramp), ``freeze`` holds one tick."""

    kind: str  # play | fast | freeze
    tick0: float
    tick1: float
    frames: int


@dataclass
class _Shot:
    clip: int
    start_tick: int
    end_tick: int
    speed: float
    caption: str | None
    src_t0: float  # seconds into the master
    out_frames: int
    jump: bool  # True when the picture jumps in time relative to the previous shot
    role: str = "play"  # play | replay
    pieces: list[_Piece] = field(default_factory=list)
    ticks: np.ndarray = field(default_factory=lambda: np.zeros(0))  # the demo tick shown by each output frame


_LABELS = {
    "pt": {"clutch": "Clutch 1v{n}", "ace": "ACE", "kills": "{n} kills", "round": "Round {n}", "hs": "{n} HS",
           "defuse": "Defuse ninja", "bomb": "Bomba", "defused": "Defusada", "versus": "1v{n}",
           "match_point": "Match point", "overtime": "Prorrogação", "pistol": "Pistol", "replay": "REPLAY",
           "view": "Visão de {name}"},
    "en": {"clutch": "1v{n} clutch", "ace": "ACE", "kills": "{n} kills", "round": "Round {n}", "hs": "{n} HS",
           "defuse": "Ninja defuse", "bomb": "Bomb", "defused": "Defused", "versus": "1v{n}",
           "match_point": "Match point", "overtime": "Overtime", "pistol": "Pistol round", "replay": "REPLAY",
           "view": "{name}'s view"},
}


def _words(locale: str) -> dict[str, str]:
    return _LABELS["pt" if (locale or "").lower().startswith("pt") else "en"]


def _is_play(segment) -> bool:
    return getattr(segment, "role", "play") != "replay"


# ------------------------------------------------------------------------------------------- what a clip is

def _play_of(clip: Clip, analysis: MatchAnalysis) -> tuple[RoundSummary | None, str | None, list[KillEvent]]:
    """(round, player, his kills shown by the clip). A replay segment shows a kill again from the victim's
    eyes: it neither adds kills nor decides whose play this is."""
    play = [s for s in clip.segments if _is_play(s)] or list(clip.segments)
    pov = next((s.pov_steamid for s in play if s.pov_steamid), None)
    first = play[0].start_tick
    rnd = next((r for r in analysis.rounds if r.start_tick <= first <= max(r.official_end_tick, r.end_tick)), None)
    kills: list[KillEvent] = []
    if rnd is not None and pov:
        kills = [k for k in rnd.kills
                 if k.attacker and k.attacker.steamid == pov and not k.is_teamkill and not k.is_suicide
                 and any(s.start_tick <= k.tick <= s.end_tick for s in play)]
    return rnd, pov, sorted(kills, key=lambda k: k.tick)


def play_kills(clip: Clip, analysis: MatchAnalysis) -> list[int]:
    """Ticks of the kills the clip's player gets on screen, in order."""
    return [k.tick for k in _play_of(clip, analysis)[2]]


def clip_info(clip: Clip, analysis: MatchAnalysis, locale: str = "en", *, chat: bool = True) -> ClipInfo:
    """Derive the HUD texts of a highlight from the match analysis. ``chat=False`` leaves the chat's reactions
    to the play out."""
    words = _words(locale)
    rnd, pov, kill_events = _play_of(clip, analysis)
    play = [s for s in clip.segments if _is_play(s)] or list(clip.segments)
    names = {p.steamid: p.name for p in analysis.players}
    kills = [k.tick for k in kill_events]
    hs = sum(int(k.headshot) for k in kill_events)
    weapons_used = list(dict.fromkeys(k.weapon_name for k in kill_events))
    label = ""
    if rnd is not None and pov:
        clutch = next((c for c in rnd.clutches if c.player.steamid == pov and c.won), None)
        if len(kills) >= 5:
            label = words["ace"]
        elif clutch is not None and clutch.versus >= 1:
            label = words["clutch"].format(n=clutch.versus)
        elif len(kills) >= 2:
            label = words["kills"].format(n=len(kills))
    detail_bits = [" + ".join(weapons_used[:2])] if weapons_used else []
    if hs >= 2:
        detail_bits.append(words["hs"].format(n=hs))
    info = ClipInfo(player=names.get(pov, clip.title or "") if pov else (clip.title or ""), label=label,
                    detail="  ".join(detail_bits), round_text=words["round"].format(n=rnd.number) if rnd else "",
                    kill_ticks=kills, replay_text=words["replay"])
    if rnd is not None:
        # the score while this round was being played (the previous round may not be in the demo)
        score = {"team_a": rnd.score_team_a - (rnd.winner_team == "team_a"),
                 "team_b": rnd.score_team_b - (rnd.winner_team == "team_b")}
        teams = {analysis.team_a.key: analysis.team_a.name, analysis.team_b.key: analysis.team_b.name}
        # the POV player's team goes on the left
        side = next((s for s, ids in (("ct", rnd.ct_players), ("t", rnd.t_players)) if pov in ids), "ct")
        mine = rnd.ct_team if side == "ct" else rnd.t_team
        other = rnd.t_team if side == "ct" else rnd.ct_team
        info.team_left, info.team_right = teams.get(mine, ""), teams.get(other, "")
        info.score_left, info.score_right = str(score[mine]), str(score[other])
        if rnd.plant_tick is not None:
            info.plant_tick, info.defuse_tick = rnd.plant_tick, rnd.defuse_tick
            info.bomb_until_tick = rnd.defuse_tick or rnd.explode_tick or rnd.end_tick
            info.bomb_text, info.defused_text = words["bomb"], words["defused"]
        # What is at stake, for the nameplate: alone against n (won or not: that was the situation), else what
        # the round meant for the match. The same thresholds as the "stakes" of the highlight score.
        end = max(s.end_tick for s in play)
        alone = next((c for c in rnd.clutches if pov and c.player.steamid == pov and c.versus >= 1
                      and c.start_tick <= end), None)
        stake = ""
        if alone is not None:
            stake = words["versus"].format(n=alone.versus)
        elif rnd.is_overtime:
            stake = words["overtime"]
        elif max(score.values()) >= 12:
            stake = words["match_point"]
        elif rnd.is_pistol:
            stake = words["pistol"]
        if stake:
            info.context = f"{stake} · {info.score_left} : {info.score_right}"
    if chat and kills:
        # Worth showing: a line that reads as a reaction (crying, swearing, laughing, praise), or anything at
        # all from the player or from somebody he killed. Small talk by bystanders is not about the play.
        said = [m for m in analysis.chat if str(m.get("text") or "").strip()
                and (m.get("kind") not in ("talk", "gg") or m.get("relation") in ("victim", "killer"))]
        info.chat = [ChatLine(name=str(m.get("name") or "?"), text=str(m.get("text") or ""), kind=str(m.get("kind") or "talk"))
                     for m in reactions(said, kills, CHAT_LINES)]
    return info


# ------------------------------------------------------------------------------------------- time

def quiet_stretches(start_tick: int, end_tick: int, kill_ticks: list[int], action_ticks: list[int],
                    tickrate: float) -> list[tuple[int, int, float]]:
    """The stretches of a segment that can run fast, as ``(from_tick, to_tick, speed)``.

    Only between two kills of the segment at least 3 s apart, outside 0.6 s after a kill, 1 s before the next
    and 0.4 s before / 0.6 s after anything the player did (``action_ticks``: his shots, his hits, damage he
    took). A stretch has to be 1.5 s long to count; from 4 s on it runs at 4x instead of 3x. Without
    ``action_ticks`` nothing is known about the stretch, so nothing is sped up."""
    kills = sorted(k for k in kill_ticks if start_tick <= k <= end_tick)
    if len(kills) < 2 or not action_ticks:
        return []
    busy = sorted((a - RAMP_ACTION_BEFORE_S * tickrate, a + RAMP_ACTION_AFTER_S * tickrate) for a in action_ticks)
    out: list[tuple[int, int, float]] = []

    def add(a: float, b: float) -> None:
        a, b = math.ceil(a), math.floor(b)
        seconds = (b - a) / tickrate
        if seconds >= RAMP_MIN_S:
            out.append((a, b, RAMP_LONG_SPEED if seconds >= RAMP_LONG_S else RAMP_SPEED))

    for k0, k1 in zip(kills, kills[1:], strict=False):
        if (k1 - k0) / tickrate < RAMP_MIN_KILL_GAP_S:
            continue
        cur, hi = k0 + RAMP_KILL_AFTER_S * tickrate, k1 - RAMP_KILL_BEFORE_S * tickrate
        for a, b in busy:
            if b <= cur:
                continue
            if a >= hi:
                break
            add(cur, a)
            cur = max(cur, b)
        add(cur, hi)
    return out


def _pieces(start: int, end: int, speed: float, tickrate: float, fps: int, fast: list[tuple[int, int, float]],
            hold: float | None) -> list[_Piece]:
    """Cut a shot into pieces at whole output frames, so each piece's sound can be laid under exactly its own
    frames. ``fast``: quiet stretches; ``hold``: the tick to freeze on."""
    step = speed * tickrate / fps  # ticks per output frame at the shot's own speed
    events: list[tuple[float, float, float]] = [(float(a), float(b), p) for a, b, p in fast]
    if hold is not None:
        events.append((float(hold), float(hold), 0.0))
    pieces: list[_Piece] = []
    cur = float(start)

    def play_to(tick: float) -> None:
        nonlocal cur
        n = int(round((tick - cur) / step))
        if n > 0:
            pieces.append(_Piece("play", cur, cur + n * step, n))
            cur += n * step

    for a, b, p in sorted(events):
        play_to(a)
        if p <= 0:
            pieces.append(_Piece("freeze", cur, cur, max(2, int(round(FREEZE_S * fps)))))
        elif b - cur > 2 * step:
            # Speed rises from 1x to a plateau and falls back (a trapezoid over the output time T):
            # T + (plateau - 1) * (T - ease) = source seconds  =>  T for the wanted plateau.
            source = (b - cur) / tickrate
            frames = max(2, int(round((source + (p - 1) * RAMP_EASE_S) / p * fps)))
            pieces.append(_Piece("fast", cur, b, frames))
            cur = b
    n = int(round((end - cur) / step))
    if n > 0 or not pieces:
        pieces.append(_Piece("play", cur, float(end), max(1, n)))
    return pieces


def _piece_ticks(p: _Piece, speed: float, tickrate: float, fps: int) -> np.ndarray:
    if p.kind == "freeze":
        return np.full(p.frames, p.tick0)
    t = np.arange(p.frames, dtype=np.float64) / fps
    if p.kind != "fast":
        return p.tick0 + t * (speed * tickrate)
    total = p.frames / fps
    ease = min(RAMP_EASE_S, total / 2)
    plateau = 1 + ((p.tick1 - p.tick0) / tickrate - total) / (total - ease)
    up = np.clip(t, 0, ease)
    down = np.clip(t - (total - ease), 0, ease)
    glide = up * up / (2 * ease) + np.clip(t - ease, 0, total - 2 * ease) + down - down * down / (2 * ease)
    return p.tick0 + tickrate * (t + (plateau - 1) * glide)


def _span_for(spans: list[Span], seg: ClipSegment) -> Span | None:
    """The stretch of footage a segment is cut from: one that holds its first tick, recorded from its point of
    view. The same ticks are often in the master more than once (the killer's take and the victim's, two
    players in the same fight), so the ticks alone would pick whichever take happens to start last.
    Footage that does not say whose view it is (``pov`` None) is matched by ticks only."""
    holding = [sp for sp in spans if sp.start_tick <= seg.start_tick <= sp.end_tick]
    same_view = [sp for sp in holding if sp.pov is not None and sp.pov == seg.pov_steamid]
    # Among equals, the one that shows most of the segment; then the latest start (the shortest decode).
    return max(same_view or holding, key=lambda sp: (min(sp.end_tick, seg.end_tick), sp.start_tick), default=None)


def _plan(clips: list[Clip], spans: list[Span] | None, tickrate: float, fps: int, *,
          kills: list[list[int]] | None = None, ramp: bool = False, freeze: bool = False) -> list[_Shot]:
    """One shot per segment, with the tick every one of its output frames shows.

    ``spans`` is the footage (segments are cut to it; None = assume everything is covered). ``kills`` are each
    clip's kill ticks: they are what the speed ramp protects and where the freeze-frame goes."""
    shots: list[_Shot] = []
    prev_end: int | None = None
    for ci, clip in enumerate(clips):
        mine = sorted(kills[ci]) if kills else []
        decisive = mine[-1] if freeze and mine else None
        for seg in clip.segments:
            end, src_t0 = seg.end_tick, 0.0
            if spans is not None:
                span = _span_for(spans, seg)
                if span is None:
                    raise RenderError(f"The captured footage does not cover ticks {seg.start_tick}–{seg.end_tick}.")
                end = min(seg.end_tick, span.end_tick)
                src_t0 = span.t0 + (seg.start_tick - span.start_tick) / tickrate
            speed = max(0.05, float(seg.speed or 1.0))
            is_play = _is_play(seg)
            fast = (quiet_stretches(seg.start_tick, end, mine, clip.action_ticks, tickrate)
                    if ramp and is_play and abs(speed - 1.0) < 1e-3 else [])
            hold = None
            if is_play and decisive is not None and seg.start_tick <= decisive <= end:
                hold, decisive = min(decisive + FREEZE_DELAY_S * tickrate, float(end)), None
            pieces = _pieces(seg.start_tick, end, speed, tickrate, fps, fast, hold)
            shots.append(_Shot(clip=ci, start_tick=seg.start_tick, end_tick=end, speed=speed, caption=seg.caption,
                               src_t0=src_t0, out_frames=sum(p.frames for p in pieces),
                               jump=prev_end is not None and abs(seg.start_tick - prev_end) > 2,
                               role="play" if is_play else "replay", pieces=pieces,
                               ticks=np.concatenate([_piece_ticks(p, speed, tickrate, fps) for p in pieces])))
            prev_end = end
    return shots


@dataclass
class Timeline:
    """Which demo tick every frame of the composed body shows. Made by :func:`timeline`; it is what
    :func:`compose_body` renders, so times read from it are the times in the video."""

    fps: int
    tickrate: float
    shots: list[_Shot]
    kills: list[list[int]]  # per clip: the ticks of its player's kills, in order

    @property
    def frames(self) -> int:
        return sum(sh.out_frames for sh in self.shots)

    @property
    def duration(self) -> float:
        return self.frames / self.fps

    def time_of_tick(self, tick: float, clip: int | None = None) -> float | None:
        """Seconds into the body at which a demo tick is first shown by the play (by a replay, if only a replay
        shows it), or None. ``clip`` limits the search to one clip of a reel."""
        offsets = np.cumsum([0] + [sh.out_frames for sh in self.shots])
        for want_play in (True, False):
            for sh, offset in zip(self.shots, offsets, strict=False):
                if (sh.role == "play") != want_play or (clip is not None and sh.clip != clip) \
                        or not sh.start_tick <= tick <= sh.end_tick:
                    continue
                i = int(np.searchsorted(sh.ticks, tick, side="left"))  # the first frame at or past the tick
                if i == 0:
                    return float(offset) / self.fps
                before = float(sh.ticks[i - 1])
                after = float(sh.ticks[i]) if i < len(sh.ticks) else before + sh.speed * self.tickrate / self.fps
                part = (tick - before) / (after - before) if after > before else 1.0
                return (float(offset) + i - 1 + min(1.0, part)) / self.fps
        return None

    def marks(self) -> list[dict[str, float | int]]:
        """``[{"tick": 74393, "t": 3.2}, ...]``: when each kill of each clip happens in the body, in order."""
        out = []
        for ci, ticks in enumerate(self.kills):
            for k in ticks:
                t = self.time_of_tick(k, clip=ci)
                if t is not None:
                    out.append({"tick": int(k), "t": round(t, 3)})
        return sorted(out, key=lambda m: m["t"])

    def freeze_of(self, clip: int) -> tuple[int, int] | None:
        """(first frame, number of frames) of the clip's freeze-frame, in frames of the body."""
        offset = 0
        for sh in self.shots:
            at = offset
            for p in sh.pieces:
                if p.kind == "freeze" and sh.clip == clip:
                    return at, p.frames
                at += p.frames
            offset += sh.out_frames
        return None

    def ramped(self) -> float:
        """Seconds the speed ramp took out of the body (source time minus screen time of the fast pieces)."""
        return sum((p.tick1 - p.tick0) / self.tickrate - p.frames / self.fps
                   for sh in self.shots for p in sh.pieces if p.kind == "fast")


def timeline(clips: list[Clip], analysis: MatchAnalysis, *, fps: int = 30, spans: list[Span] | None = None,
             ramp: bool = True, freeze: bool = True) -> Timeline:
    """The timing of the body :func:`compose_body` makes from these clips with the same ``fps``, ``ramp`` and
    ``freeze`` (and ``spans``, when the footage may be shorter than a segment). Needs no footage."""
    tickrate = float(analysis.tickrate or 64)
    kills = [play_kills(c, analysis) for c in clips]
    return Timeline(fps, tickrate, _plan(clips, spans, tickrate, fps, kills=kills, ramp=ramp, freeze=freeze), kills)


def kill_marks(clips: list[Clip], analysis: MatchAnalysis, *, fps: int = 30, spans: list[Span] | None = None,
               ramp: bool = True, freeze: bool = True) -> list[dict[str, float | int]]:
    """``[{"tick": int, "t": float}]`` for the kills of these clips: ``t`` is seconds into the body (before the
    intro bumper: add the render's ``body_offset_s``). The same list :func:`compose_body` returns; this one
    needs no footage, so it can be computed for a video that already exists (pass ``ramp=False, freeze=False``
    for videos made before those existed)."""
    return timeline(clips, analysis, fps=fps, spans=spans, ramp=ramp, freeze=freeze).marks()


def time_of_tick(clips: list[Clip], tick: int, tickrate: float, *, analysis: MatchAnalysis | None = None,
                 fps: int = 30, spans: list[Span] | None = None, ramp: bool = True,
                 freeze: bool = True) -> float | None:
    """Seconds into the composed body at which a demo tick is shown, or None if no segment shows it.

    With ``analysis`` this is read from :func:`timeline` and matches a body composed with the same options.
    Without it every segment is assumed to play at its own constant speed (no ramp, no freeze-frame)."""
    if analysis is not None:
        return timeline(clips, analysis, fps=fps, spans=spans, ramp=ramp, freeze=freeze).time_of_tick(tick)
    for want_play in (True, False):
        t = 0.0
        for clip in clips:
            for s in clip.segments:
                speed = float(s.speed or 1.0)
                if _is_play(s) == want_play and s.start_tick <= tick <= s.end_tick:
                    return t + (tick - s.start_tick) / tickrate / speed
                t += (s.end_tick - s.start_tick) / tickrate / speed
    return None


def _audio_filter(shots: list[_Shot], tickrate: float, has_audio: bool, fps: int = 30) -> tuple[str, str]:
    """Filter graph that cuts each piece of each shot out of the master's audio and stretches it to the piece's
    speed. Slow motion drops the pitch with the tempo (the familiar slow-mo sound); fast motion keeps the pitch.
    A freeze-frame is silence (an impact is mixed over it), with the sound around it faded so it does not click."""
    parts: list[str] = []
    for sh in shots:
        for j, p in enumerate(sh.pieces):
            i = len(parts)
            dur_src = (p.tick1 - p.tick0) / tickrate
            dur_out = p.frames / fps
            if not has_audio or p.kind == "freeze" or dur_src <= 0:
                parts.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{dur_out:.4f},asetpts=PTS-STARTPTS[a{i}]")
                continue
            t0 = sh.src_t0 + (p.tick0 - sh.start_tick) / tickrate
            chain = (f"[0:a]atrim=start={t0:.4f}:end={t0 + dur_src:.4f},asetpts=PTS-STARTPTS,"
                     f"aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo")
            speed = dur_src / dur_out if p.kind == "fast" else sh.speed
            if speed < 0.999:
                chain += f",asetrate={48000 * speed:.1f},aresample=48000"
            elif speed > 1.001:
                while speed > 2.0:
                    chain += ",atempo=2.0"
                    speed /= 2.0
                chain += f",atempo={speed:.4f}"
            chain += f",apad,atrim=0:{dur_out:.4f}"
            # joins inside a shot: a breath around the freeze, a few milliseconds where the tempo changes
            before = sh.pieces[j - 1].kind if j > 0 else None
            after = sh.pieces[j + 1].kind if j + 1 < len(sh.pieces) else None
            fade_in = 0.04 if before == "freeze" else 0.006 if before is not None else 0.0
            fade_out = 0.03 if after == "freeze" else 0.006 if after is not None else 0.0
            if fade_in and dur_out > 2 * fade_in:
                chain += f",afade=t=in:st=0:d={fade_in}"
            if fade_out and dur_out > 2 * fade_out:
                chain += f",afade=t=out:st={dur_out - fade_out:.4f}:d={fade_out}"
            parts.append(chain + f"[a{i}]")
    joined = "".join(f"[a{i}]" for i in range(len(parts)))
    return ";".join(parts) + f";{joined}concat=n={len(parts)}:v=0:a=1[game]", "game"


def _smooth(x: float) -> float:
    x = 0.0 if x < 0 else 1.0 if x > 1 else x
    return x * x * (3 - 2 * x)


def _punch_env(dt: float) -> float:
    """0..1 push-in around a kill, ``dt`` seconds after it (negative: before)."""
    if dt <= -PUNCH_LEAD_S or dt >= PUNCH_TAIL_S:
        return 0.0
    if dt < 0:
        return _smooth((dt + PUNCH_LEAD_S) / PUNCH_LEAD_S)
    return 1.0 - _smooth((dt - PUNCH_HOLD_S) / (PUNCH_TAIL_S - PUNCH_HOLD_S))


@dataclass
class Layout:
    """Where the game picture goes in the output frame, and how it is cut out of the master."""

    kind: str  # landscape | portrait | square
    window: tuple[int, int, int, int]  # x, y, w, h of the game picture in the output
    vf: str  # ffmpeg filter that turns a master frame into exactly the window
    hud_scale: float

    @staticmethod
    def choose(width: int, height: int) -> Layout:
        ratio = width / height
        if ratio < 0.8:
            # Phone: the game in a 4:5 window cut from the centre of the 16:9 master (the action is at the
            # crosshair), brand header above, call to action below — both mostly under the apps' own UI.
            win_h = min(height, int(round(width * 5 / 4)) // 2 * 2)
            top = int((height - win_h) * 0.44) // 2 * 2
            return Layout("portrait", (0, top, width, win_h), f"crop=ih*{width}/{win_h}:ih,scale={width}:{win_h}:flags=bicubic",
                          width / 720)
        if ratio < 1.2:
            return Layout("square", (0, 0, width, height), f"crop=ih*{width}/{height}:ih,scale={width}:{height}:flags=bicubic",
                          width / 900)
        return Layout("landscape", (0, 0, width, height), f"scale={width}:{height}:flags=bicubic", height / 720)


def _portrait_backdrop(brand: Brand, width: int, height: int, layout: Layout) -> skia.Image:
    """Static frame behind the game window of a vertical video: brand ground, slanted bars, wordmark, CTA."""
    from .bumper import _col, typeface
    from .hud import SLANT
    from .logo import load_logo

    s = layout.hud_scale
    _x, top, _w, win_h = layout.window
    surface = skia.Surface(width, height)
    cv = surface.getCanvas()
    cv.clear(_col(brand.colors.background))
    p = skia.Paint(AntiAlias=True)
    for i in range(6):  # the same lean as the bumpers, kept quiet
        x = (i * 0.19 + 0.05) * width * 1.5 - width * 0.3
        bw = (30 + 44 * ((i * 7) % 5)) * s
        p.setColor(_col(brand.colors.text if i % 3 else brand.colors.accent, 0.045 + 0.02 * (i % 3)))
        path = skia.Path()
        path.moveTo(x, height)
        path.lineTo(x + bw, height)
        path.lineTo(x + bw + height * SLANT, 0)
        path.lineTo(x + height * SLANT, 0)
        path.close()
        cv.drawPath(path, p)
    name_font = skia.Font(typeface("BarlowCondensed-ExtraBold.ttf"), 50 * s)
    name = brand.name.upper()
    mark = None
    if brand.logo_path is not None:
        try:
            lg = load_logo(brand.logo_path, brand.logo_background)
            x0, y0, x1, y1 = lg.bbox
            mark = skia.Image.fromarray(np.ascontiguousarray(lg.rgba[y0:y1, x0:x1]), colorType=skia.kRGBA_8888_ColorType,
                                        alphaType=skia.kUnpremul_AlphaType).withDefaultMipmaps()
        except (OSError, ValueError):
            mark = None
    mh = 62 * s
    mw = mh * mark.width() / mark.height() if mark is not None else 0
    tw = name_font.measureText(name)
    total = mw + (16 * s if mark is not None else 0) + tw
    x = (width - total) / 2
    cy = top - 52 * s  # just above the game window, clear of the very top of the screen
    if mark is not None:
        cv.drawImageRect(mark, skia.Rect.MakeXYWH(x, cy - mh / 2, mw, mh),
                         skia.SamplingOptions(skia.FilterMode.kLinear, skia.MipmapMode.kLinear), skia.Paint(AntiAlias=True))
        x += mw + 16 * s
    p.setColor(_col(brand.colors.text))
    cv.drawString(name, x, cy + 17 * s, name_font, p)
    # accent rules framing the window
    p.setColor(_col(brand.colors.accent))
    cv.drawRect(skia.Rect.MakeXYWH(0, top - 3 * s, width, 3 * s), p)
    cv.drawRect(skia.Rect.MakeXYWH(0, top + win_h, width, 3 * s), p)
    cta = brand.bumper.cta or (brand.url or "").removeprefix("https://").removeprefix("http://").rstrip("/")
    if cta:
        f = skia.Font(typeface("BarlowCondensed-Bold.ttf"), 44 * s)
        p.setColor(_col(brand.colors.text))
        cv.drawString(cta, (width - f.measureText(cta)) / 2, top + win_h + 74 * s, f, p)
    if brand.tagline:
        f = skia.Font(typeface("Barlow-Medium.ttf"), 23 * s)
        p.setColor(_col(brand.colors.muted))
        cv.drawString(brand.tagline, (width - f.measureText(brand.tagline)) / 2, top + win_h + 112 * s, f, p)
    return surface.makeImageSnapshot()


@dataclass
class Body:
    """What :func:`compose_body` made."""

    duration: float  # seconds
    marks: list[dict[str, float | int]]  # [{"tick": int, "t": float}]: every kill of every clip, seconds into the body
    timeline: Timeline


def _pushed_in(frame: bytes, width: int, height: int, zoom: float) -> bytes:
    """The frame enlarged ``zoom`` times around its centre - the crosshair - and cut back to its size.
    The footage has already been resampled once to fit the window, so this second pass uses a Lanczos filter:
    a bilinear one visibly softens the picture, and Skia's cubic resamplers take most of a second per frame
    on the CPU."""
    zw, zh = width / zoom, height / zoom
    box = ((width - zw) / 2, (height - zh) / 2, (width + zw) / 2, (height + zh) / 2)
    # four plain channels: their order does not matter to a resize, and the fourth is not an alpha to weigh by
    image = PILImage.frombuffer("RGBX", (width, height), frame, "raw", "RGBX", 0, 1)
    return image.resize((width, height), PILImage.Resampling.LANCZOS, box=box).tobytes()


def _replay_caption(caption: str | None, words: dict[str, str]) -> str | None:
    """The analysis captions a replay "Replay · <victim>"; next to the REPLAY badge only the name is news."""
    if caption and caption.startswith("Replay · "):
        return words["view"].format(name=caption[len("Replay · "):])
    return caption


def compose_body(master: Path, out: Path, *, spans: list[Span], clips: list[Clip], analysis: MatchAnalysis,
                 brand: Brand, src_fps: float, fps: int, width: int, height: int, crf: int = 20,
                 hud: bool = True, master_has_audio: bool = True, ramp: bool = True, freeze: bool = True,
                 punch_in: bool = True, chat: bool | None = None,
                 progress: Callable[[float, str], None] = lambda fraction, message: None,
                 cancelled: Callable[[], bool] = lambda: False) -> Body:
    """Produce the body video at ``fps`` in the requested shape.

    ``ramp``: play the quiet stretches between kills fast. ``freeze``: hold the picture on the decisive kill.
    ``punch_in``: push in on every kill (vertical videos only). ``chat``: show the chat's reactions at the end
    of each play (None = as the brand kit says). Returns the duration and when each kill happens in the body."""
    tickrate = float(analysis.tickrate or 64)
    width, height = width - width % 2, height - height % 2
    layout = Layout.choose(width, height)
    wx, wy, ww, wh = layout.window
    tl = timeline(clips, analysis, fps=fps, spans=spans, ramp=ramp, freeze=freeze)
    shots = tl.shots
    words = _words(brand.locale)
    infos = [clip_info(c, analysis, brand.locale, chat=brand.chat if chat is None else chat) for c in clips]
    total_frames = tl.frames
    offsets = [0]
    for sh in shots:
        offsets.append(offsets[-1] + sh.out_frames)
    # Per clip, in frames of the body: when each kill is on screen, and the freeze-frame.
    kill_frames = [[math.ceil(t * fps - 1e-6) for t in (tl.time_of_tick(k, clip=ci) for k in tl.kills[ci]) if t is not None]
                   for ci in range(len(clips))]
    freezes = [tl.freeze_of(ci) for ci in range(len(clips))]
    # The HUD counts time within the play (all "play" shots of a clip) or within a replay (a run of replay
    # shots), so the corner and the kill counter leave before a replay starts instead of after it.
    play_total = [sum(sh.out_frames for sh in shots if sh.clip == ci and sh.role == "play") for ci in range(len(clips))]
    run_before, run_total = [0] * len(shots), [0] * len(shots)
    seen = [0] * len(clips)
    si = 0
    while si < len(shots):
        sh = shots[si]
        if sh.role == "play":
            run_before[si], run_total[si] = seen[sh.clip], play_total[sh.clip]
            seen[sh.clip] += sh.out_frames
            si += 1
            continue
        end = si
        while end < len(shots) and shots[end].role != "play" and shots[end].clip == sh.clip:
            end += 1
        for k in range(si, end):
            run_before[k], run_total[k] = offsets[k] - offsets[si], offsets[end] - offsets[si]
        si = end
    # Chat reactions enter during the post-roll: after the decisive kill has had its moment.
    chat_from: list[int | None] = [None] * len(clips)
    for ci, info in enumerate(infos):
        if not info.chat or not kill_frames[ci]:
            continue
        last = next((run_before[k] + kill_frames[ci][-1] - offsets[k] for k, sh in enumerate(shots)
                     if sh.clip == ci and sh.role == "play" and offsets[k] <= kill_frames[ci][-1] < offsets[k + 1]), 0)
        start = max(play_total[ci] - round(CHAT_S * fps), last + (freezes[ci][1] if freezes[ci] else 0) + round(0.15 * fps))
        if play_total[ci] - start >= round(CHAT_MIN_S * fps):
            chat_from[ci] = start

    painter = Hud(brand, ww, wh, scale=layout.hud_scale, show_mark=layout.kind != "portrait") if hud else None
    backdrop = _portrait_backdrop(brand, width, height, layout) if layout.kind == "portrait" else None
    push = punch_in and layout.kind == "portrait"
    # Vertical and square cuts lose the game's own kill feed (top-right corner), so ours is drawn instead.
    feed = None
    round_kills: list[list] = [[] for _ in clips]
    povs: list[str | None] = [next((sg.pov_steamid for sg in c.segments if _is_play(sg) and sg.pov_steamid), None)
                              for c in clips]
    if hud and layout.kind != "landscape":
        try:
            from ..render.killfeed import draw_killfeed as feed
        except Exception:  # noqa: BLE001 - the feed is an enhancement; never fail a render over it
            feed = None
        for i, c in enumerate(clips):
            round_kills[i] = list(rnd.kills) if (rnd := _play_of(c, analysis)[0]) else []

    frame_bytes = ww * wh * 4

    def decode_from(frame: int) -> subprocess.Popen:
        return subprocess.Popen(
            [ffmpeg.ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-ss", f"{frame / src_fps:.6f}",
             "-i", str(master), "-an", "-vf", layout.vf, "-f", "rawvideo", "-pix_fmt", "bgra", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW)

    # Masters can hold a whole match: jump to the first frame this video needs instead of decoding from zero.
    lead = int(src_fps // 4)
    first_frame = max(0, int(shots[0].src_t0 * src_fps) - lead)
    decoder = decode_from(first_frame)
    video_tmp = out.with_suffix(".video.mp4")
    writer = ffmpeg.open_rgb_writer(video_tmp, width, height, fps, crf=crf, pix_fmt="bgra")
    surface = skia.Surface(width, height)
    cv = surface.getCanvas()
    plain = painter is None and backdrop is None  # nothing to draw: frames can be passed straight through
    # Finished frames are copied straight out of the surface into this buffer (the surface's own layout).
    frame_out = np.empty((height, width, 4), dtype=np.uint8)
    out_info = skia.ImageInfo.Make(width, height, skia.kBGRA_8888_ColorType, skia.kPremul_AlphaType)

    src_index = first_frame - 1
    current: bytes | None = None
    done = 0
    cut_times: list[float] = []
    try:
        for si, sh in enumerate(shots):
            ci = sh.clip
            replay = sh.role != "play"
            caption = _replay_caption(sh.caption, words) if replay else sh.caption
            fz = None if replay else freezes[ci]
            if sh.jump:
                cut_times.append(done / fps)
            for j in range(sh.out_frames):
                tick = float(sh.ticks[j])
                want = int(round((sh.src_t0 + (tick - sh.start_tick) / tickrate) * src_fps))
                # A decoder only reads forwards. A shot that sits earlier in the master than the picture on
                # screen (a replay of a kill from the middle of the play: footage is stored in tick order), or
                # far ahead of it, gets a decoder of its own instead of a frozen picture or seconds of skipping.
                if j == 0 and (want < src_index - 1 or want - src_index > SEEK_AHEAD_S * src_fps):
                    decoder.kill()
                    decoder.wait()
                    src_index = max(0, want - lead) - 1
                    decoder = decode_from(src_index + 1)
                while src_index < want:  # within a shot the master is read forwards; unused frames are skipped
                    buf = decoder.stdout.read(frame_bytes)
                    if len(buf) < frame_bytes:
                        break  # master shorter than expected: hold the last frame
                    current, src_index = buf, src_index + 1
                if current is None:
                    raise RenderError("The captured footage is empty.")
                if plain:
                    writer.stdin.write(current)
                else:
                    # How far the picture is pushed in: on every kill in vertical videos, a touch on the freeze.
                    zoom, punch = 1.0, 0.0
                    held = min(max(done - fz[0], 0), fz[1]) / fps if fz else 0.0
                    if push and not replay:
                        env = 0.0
                        for kf in kill_frames[ci]:
                            dt = (done - kf) / fps
                            env = max(env, _punch_env(max(0.0, dt - held) if dt > 0 else dt))
                        zoom += (PUNCH_IN - 1) * env
                    if fz and done >= fz[0]:
                        since, hold_s = (done - fz[0]) / fps, fz[1] / fps
                        zoom *= 1 + FREEZE_ZOOM * (_smooth(since / 0.1) if since < hold_s
                                                   else 1 - _smooth((since - hold_s) / 0.25))
                        punch = max(0.0, 1 - since / 0.18)
                    if backdrop is not None:
                        cv.drawImage(backdrop, 0, 0)
                    pixels = _pushed_in(current, ww, wh, zoom) if zoom > 1.0005 else current
                    cv.drawImage(skia.Image.frombytes(pixels, skia.ISize(ww, wh), skia.kBGRA_8888_ColorType,
                                                      skia.kOpaque_AlphaType), wx, wy)
                    if painter is not None:
                        frames = kill_frames[ci]
                        kills_done = bisect.bisect_right(frames, done)
                        since_kill = (done - frames[kills_done - 1]) / fps if kills_done else None
                        slow = 0.0
                        if sh.speed < 0.999:  # letterbox eases in/out, but stays closed across back-to-back slow shots
                            ease = SLOW_EASE_S * fps
                            slow_before = si > 0 and shots[si - 1].clip == ci and shots[si - 1].speed < 0.999
                            slow_after = si + 1 < len(shots) and shots[si + 1].clip == ci and shots[si + 1].speed < 0.999
                            slow = min(1.0 if slow_before else j / ease,
                                       1.0 if slow_after else (sh.out_frames - 1 - j) / ease, 1.0)
                        flash = max(0.0, 1 - j / CUT_FLASH_FRAMES) ** 2 if sh.jump else 0.0
                        at = run_before[si] + j  # frames into the play (or into the replay)
                        t_chat = None
                        if not replay and chat_from[ci] is not None and at >= chat_from[ci]:
                            t_chat = (at - chat_from[ci]) / fps
                        cv.save()
                        cv.translate(wx, wy)
                        cv.clipRect(skia.Rect.MakeWH(ww, wh))
                        painter.draw(cv, HudState(
                            clip=infos[ci], t_clip=at / fps, t_left=(run_total[si] - at) / fps,
                            kills_done=kills_done, t_since_kill=since_kill, caption=caption, slow=slow,
                            cut_flash=flash, tick=int(tick), tickrate=float(tickrate), replay=replay,
                            freeze_s=fz[1] / fps if fz else 0.0, punch=punch, t_chat=t_chat))
                        if feed is not None and not replay:  # a replay would list the same kill again
                            try:
                                feed(cv, round_kills[ci], tick, tickrate, brand=brand, scale=layout.hud_scale,
                                     x=ww - 22 * layout.hud_scale, y=22 * layout.hud_scale, align="right",
                                     pov_steamid=povs[ci], max_rows=4)
                            except Exception:  # noqa: BLE001
                                feed = None
                        cv.restore()
                    if surface.readPixels(out_info, frame_out, width * 4, 0, 0):
                        writer.stdin.write(frame_out)
                    else:  # pragma: no cover - a surface that cannot be read this way: the slow, certain path
                        writer.stdin.write(surface.toarray(colorType=skia.kBGRA_8888_ColorType).tobytes())
                done += 1
                if done % 45 == 0:
                    progress(done / total_frames, "Compositing")
                    if cancelled():
                        raise RenderCancelled("Render canceled.")
    except BaseException:
        writer.kill()
        writer.wait()
        decoder.kill()
        decoder.wait()
        video_tmp.unlink(missing_ok=True)
        raise
    decoder.kill()
    decoder.wait()
    ffmpeg.close_writer(writer, "composite encode")

    duration = total_frames / fps
    graph, label = _audio_filter(shots, tickrate, master_has_audio, fps)
    args = ["-i", str(video_tmp)] if not master_has_audio else ["-i", str(master), "-i", str(video_tmp)]
    vmap = "0:v" if not master_has_audio else "1:v"
    wav = out.with_suffix(".cuts.wav")
    hits = [f[0] / fps for f in freezes if f is not None]
    try:
        if cut_times or hits:
            mix = sfx.Mix(duration)
            for n, t in enumerate(cut_times):  # a short whoosh on every jump in time
                mix.add(sfx.whoosh(0.34, low=420, high=3600, pan_from=-0.4, pan_to=0.4, seed=90 + n), max(0.0, t - 0.12), 0.5)
            if hits:  # the freeze-frame lands on an impact: the game sound is silent while the picture holds
                hit = sfx.impact(weight=0.8, seed=31, metal=False)
                for t in hits:
                    mix.add(hit, t, 0.45)
            sfx.write_wav(wav, mix.buf[: int(duration * sfx.SR)])
            args += ["-i", str(wav)]
            idx = len(args) // 2 - 1
            graph += f";[{label}][{idx}:a]amix=inputs=2:normalize=0:duration=first[mixed]"
            label = "mixed"
        ffmpeg.run([*args, "-filter_complex", graph, "-map", vmap, "-map", f"[{label}]", "-c:v", "copy",
                    "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-t", f"{duration:.3f}", "-movflags", "+faststart",
                    str(out)], "composite audio")
    finally:
        video_tmp.unlink(missing_ok=True)
        wav.unlink(missing_ok=True)
    return Body(duration=duration, marks=tl.marks(), timeline=tl)


def retime_and_overlay(master: Path, out: Path, **options) -> float:
    """:func:`compose_body` for callers that only want the duration in seconds."""
    return compose_body(master, out, **options).duration
