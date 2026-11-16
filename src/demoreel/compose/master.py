"""Capture masters: raw first-person footage kept so that edits do not need the game again.

Recording with CS2 is the expensive step (a game launch, a GPU, minutes). Everything after it — trimming,
speed changes, captions, HUD, bumpers, a whole new theme — is cheap. So each capture is stored as a
*master* together with the tick ranges and points of view it covers, recorded with **handles** (a little
extra footage before and after each segment). A later render reuses any master that covers what it needs:
tightening a clip, starting it a second earlier, adding slow motion or re-branding never relaunches the game.

Layout: ``<data_dir>/masters/<match_id>/<master_id>.mp4`` + ``.json``.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from ..config import get_settings
from ..render.base import Clip, ClipSegment
from ..render.ingame import xray
from ..render.ingame.actions import FIRST_RECORD_TICK
from .overlay import Span

HANDLE_BEFORE_S = 1.5
HANDLE_AFTER_S = 1.0
MAX_CAPTURE_FPS = 240
FIRST_TICK = FIRST_RECORD_TICK  # earlier than this the game cannot be aimed at a player before recording starts


def recordable(clips: list[Clip], total_ticks: int) -> list[Clip]:
    """The same clips with every segment clamped to what the game can actually record (``FIRST_TICK`` .. end - 8).
    Rendering uses these, so a play from the first seconds of a demo is cut to the footage that can exist."""
    out = []
    for c in clips:
        # Everything else about the clip is kept: a segment's role (play / replay) and the clip's action ticks
        # are what composition edits by.
        segs = [replace(s, start_tick=max(FIRST_TICK, s.start_tick), end_tick=min(s.end_tick, total_ticks - 8))
                for s in c.segments]
        segs = [s for s in segs if s.end_tick - s.start_tick > 8]
        if segs:
            out.append(replace(c, segments=segs))
    return out


@dataclass
class Covered:
    """One stretch of ticks the master shows from one point of view."""

    start_tick: int
    end_tick: int
    pov: str | None


@dataclass
class Master:
    id: str
    match_id: str
    width: int
    height: int
    fps: int
    spans: list[dict] = field(default_factory=list)  # [{start_tick, end_tick, frames}] in video order
    covered: list[Covered] = field(default_factory=list)
    has_audio: bool = True
    options: dict = field(default_factory=dict)  # recording options that change the picture (HUD, x-ray…)
    xray: str = "off"  # "auto": outlines around kills through smoke / walls; "off": none (also: older captures)

    @property
    def path(self) -> Path:
        return masters_dir(self.match_id) / f"{self.id}.mp4"

    def covers(self, segments: list[ClipSegment]) -> bool:
        return all(any(c.start_tick <= s.start_tick and s.end_tick <= c.end_tick and c.pov == s.pov_steamid
                       for c in self.covered) for s in segments)

    def as_spans(self) -> list[Span]:
        """The footage as composition reads it: where each take starts in the video, its ticks and whose view
        it is. Takes are recorded in the order of ``covered``; when a take is missing (the recorder skipped
        one) the point of view is taken from the only covered stretch with the same ticks, or left unknown."""
        in_order = len(self.spans) == len(self.covered) and all(
            c.start_tick <= sp["start_tick"] and sp["end_tick"] <= c.end_tick
            for sp, c in zip(self.spans, self.covered, strict=True))
        out, frames = [], 0
        for i, sp in enumerate(self.spans):
            if "pov" in sp:
                pov = sp["pov"]
            elif in_order:
                pov = self.covered[i].pov
            else:
                same = {c.pov for c in self.covered
                        if c.start_tick <= sp["start_tick"] and sp["end_tick"] <= c.end_tick}
                pov = same.pop() if len(same) == 1 else None
            out.append(Span(t0=frames / self.fps, start_tick=sp["start_tick"], end_tick=sp["end_tick"], pov=pov))
            frames += int(sp["frames"])
        return out


def masters_dir(match_id: str) -> Path:
    d = get_settings().data_dir / "masters" / match_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def capture_fps_for(segments: list[ClipSegment], out_fps: int) -> int:
    """Frame rate to record at so the slowest segment still gets ``out_fps`` real frames per second."""
    slowest = min((float(s.speed or 1.0) for s in segments), default=1.0)
    if slowest >= 0.999:
        return out_fps
    return min(MAX_CAPTURE_FPS, int(round(out_fps / max(slowest, 0.05))))


SESSION_START_S = 25.0  # measured: launch + demo load before the first recorded frame (warm)
SESSION_RATE = 0.75  # recording seconds per extra second of footage-equivalent (HLAE at 60 vs 30 fps: +33 %)


def capture_groups(clips: list[Clip], out_fps: int, base_fps: int) -> dict[int, list[Clip]]:
    """Clips grouped by the frame rate they have to be recorded at, lowest first.

    Recording time grows with the number of frames, so a play in slow motion must not raise the frame rate of
    every other play recorded in the same batch: it gets a session of its own at its own rate."""
    groups: dict[int, list[Clip]] = {}
    for clip in clips:
        groups.setdefault(max(int(base_fps), capture_fps_for(clip.segments, out_fps)), []).append(clip)
    groups = dict(sorted(groups.items()))
    # ... unless the slower group is small: another game session costs ~25 s of start-up, more than recording a
    # few plays with extra frames. A group joins the next faster one when its extra recording time is under that.
    rates = list(groups)
    for low, high in zip(rates, rates[1:], strict=False):
        seconds = sum((s.end_tick - s.start_tick) / 64.0 for c in groups[low] for s in c.segments)
        extra = seconds * (high / low - 1.0) * SESSION_RATE  # recording runs close to real time per output frame
        if extra < SESSION_START_S:
            groups[high] = groups.pop(low) + groups[high]
    return dict(sorted(groups.items()))


def picture_options(extra: dict) -> dict:
    """The recording options that end up in the pixels; two captures are interchangeable only if these match."""
    out = {k: extra[k] for k in sorted(extra) if k.startswith("ingame_") and k in {
        "ingame_hud", "ingame_assists", "ingame_true_view", "ingame_commands"}}
    if xray.mode(extra) == "on":  # "auto" and "off" are told apart per play, see find_master
        out["ingame_xray"] = True
    return out


def list_masters(match_id: str) -> list[Master]:
    out = []
    for f in masters_dir(match_id).glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            data["covered"] = [Covered(**c) for c in data.get("covered", [])]
            m = Master(**data)
        except (ValueError, TypeError, OSError):
            continue
        if m.path.is_file() and m.path.stat().st_size > 0:
            out.append(m)
    return out


def find_master(match_id: str, segments: list[ClipSegment], *, width: int, height: int, min_fps: int,
                extra: dict, allow_lower_fps: bool = False, kills: list | None = None,
                tickrate: float = 64) -> Master | None:
    """A stored capture that can serve these segments.

    ``width``/``height`` are the size footage should have been *recorded* at. A capture qualifies when it is at
    least that large and at least ``min_fps``; among those the smallest is used (cheapest to decode). With
    ``allow_lower_fps`` a smaller or slower capture is accepted too (softer picture, repeated frames in slow
    motion) — used when the game is not available to record a better one.

    ``kills`` (the match's kill log) decides the x-ray question: footage recorded without outlines serves a
    play in "auto" mode unless that play has a kill through smoke or a wall, and the other way round."""
    want = picture_options(extra)
    fits = [m for m in list_masters(match_id) if m.options == want and m.covers(segments)]
    mode = xray.mode(extra)
    if mode != "on" and xray.needed(kills, segments, tickrate):
        fits = [m for m in fits if m.xray == mode]
    good = sorted((m for m in fits if m.fps >= min_fps and m.width >= width and m.height >= height),
                  key=lambda m: (m.width * m.height, m.fps))
    if good:
        return good[0]
    if allow_lower_fps and fits:
        return max(fits, key=lambda m: (m.width * m.height, m.fps))
    return None


def capture_clips(clips: list[Clip], rounds: list, tickrate: float, total_ticks: int) -> tuple[list[Clip], list[Covered]]:
    """The clips to actually record: each segment widened by the handles (never across a round boundary, so the
    footage stays within one continuous piece of play), overlapping pieces of the same POV merged."""
    before, after = int(HANDLE_BEFORE_S * tickrate), int(HANDLE_AFTER_S * tickrate)
    wide: list[Covered] = []
    for clip in clips:
        for s in clip.segments:
            rnd = next((r for r in rounds if r.start_tick <= s.start_tick <= max(r.official_end_tick, r.end_tick)), None)
            lo = rnd.start_tick if rnd else 0
            hi = max(rnd.official_end_tick, rnd.end_tick) if rnd else total_ticks
            # The recorder cannot start before tick 96 nor run into the last ticks of the demo.
            wide.append(Covered(max(lo, s.start_tick - before, FIRST_TICK),
                                min(hi, total_ticks - 8, s.end_tick + after), s.pov_steamid))
    # Merge what overlaps *for the same point of view*. Two players' plays may overlap in time (both got
    # kills in the same fight): those stay separate takes, each recorded in full.
    wide = [c for c in wide if c.end_tick - c.start_tick > 8]
    wide.sort(key=lambda c: (c.pov or "", c.start_tick, c.end_tick))
    merged: list[Covered] = []
    for c in wide:
        last = merged[-1] if merged else None
        if last is not None and last.pov == c.pov and c.start_tick <= last.end_tick:
            last.end_tick = max(last.end_tick, c.end_tick)
        else:
            merged.append(Covered(c.start_tick, c.end_tick, c.pov))
    merged.sort(key=lambda c: (c.start_tick, c.end_tick))
    # One clip per take, so the recorder never joins two of them into a single sequence.
    record = [Clip(segments=[ClipSegment(c.start_tick, c.end_tick, c.pov, 1.0, None)]) for c in merged]
    return record, merged


def save_master(match_id: str, video: Path, *, width: int, height: int, fps: int, spans: list[dict],
                covered: list[Covered], extra: dict, has_audio: bool = True) -> Master:
    m = Master(id=f"ms_{secrets.token_hex(6)}", match_id=match_id, width=width, height=height, fps=fps,
               spans=spans, covered=covered, has_audio=has_audio, options=picture_options(extra),
               xray=xray.mode(extra))
    video.replace(m.path)
    m.path.with_suffix(".json").write_text(json.dumps(asdict(m), indent=1), encoding="utf-8")
    return m


def delete_masters(match_id: str) -> None:
    import shutil

    shutil.rmtree(get_settings().data_dir / "masters" / match_id, ignore_errors=True)
