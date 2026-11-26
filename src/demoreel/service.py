"""Business logic shared by the REST API, the MCP server and the CLI.

Everything here takes a SQLAlchemy session and returns plain pydantic models, so the three front-ends stay
thin and always behave the same way.
"""

from __future__ import annotations

import bz2
import gzip
import hashlib
import hmac
import logging
import shutil
import threading
import time
import zipfile
from collections.abc import Iterable
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import jobs
from .analysis.schema import HighlightSegment, MatchAnalysis
from .config import get_settings
from .db import Highlight, Job, Match, Render, new_id, now, session_scope
from .schemas import (
    BrandOut,
    EditRequest,
    FeedHighlight,
    FeedItem,
    FeedMatch,
    HighlightCreate,
    HighlightOut,
    HighlightPatch,
    HighlightPlayer,
    JobOut,
    MatchOut,
    RenderOptionsIn,
    RenderOut,
    TeamScore,
)

log = logging.getLogger("demoreel.service")

CS2_MAGIC = b"PBDEMS2\x00"
CSGO_MAGIC = b"HL2DEMO\x00"
MAX_SEGMENTS = 40
MAX_CLIP_SECONDS = 300.0


class ServiceError(Exception):
    status_code = 400

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class NotFound(ServiceError):
    status_code = 404


class Conflict(ServiceError):
    status_code = 409


# ---------------------------------------------------------------------------------------------- files

def sign_path(rel: str, exp: int) -> str:
    key = (get_settings().api_key or "").encode()
    return hmac.new(key, f"{rel}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]


def file_url(rel: str | None) -> str | None:
    """Public URL of a stored file. When an API key is configured the link is signed and expires in 24h,
    so it can be handed to a <video> tag or another tool without leaking the key."""
    if not rel:
        return None
    s = get_settings()
    url = f"{s.base_url}/files/{rel}"
    if s.api_key:
        exp = int(time.time()) + 24 * 3600
        url += f"?exp={exp}&sig={sign_path(rel, exp)}"
    return url


def abs_path(rel: str) -> Path:
    return get_settings().data_dir / rel


# ---------------------------------------------------------------------------------------------- ingest

def _open_maybe_compressed(src: Path):
    with src.open("rb") as f:
        head = f.read(8)
    if head.startswith(b"\x1f\x8b"):
        return gzip.open(src, "rb")
    if head.startswith(b"BZh"):
        return bz2.open(src, "rb")
    if head.startswith(b"\x28\xb5\x2f\xfd"):
        try:
            from compression import zstd  # Python 3.14+
        except ImportError as exc:
            raise ServiceError("Zstandard-compressed demos need Python 3.14+. Decompress the .zst first.") from exc
        return zstd.open(src, "rb")
    if head.startswith(b"PK\x03\x04"):
        zf = zipfile.ZipFile(src)
        names = [n for n in zf.namelist() if n.lower().endswith(".dem")]
        if not names:
            raise ServiceError("The zip archive contains no .dem file.")
        return zf.open(names[0])
    return src.open("rb")


def ingest_demo(s: Session, src: Path, filename: str, *, auto_render_top: int | None = None,
                renderer: str | None = None, keep_source: bool = True) -> tuple[Match, Job | None, bool]:
    """Register a demo file (optionally .gz/.bz2/.zst/.zip compressed) and queue its analysis.

    Returns ``(match, job, created)``. Uploading the same demo twice returns the existing match.
    """
    settings = get_settings()
    match_id = new_id("m")
    rel = f"demos/{match_id}.dem"
    dest = settings.path(*rel.split("/"))
    sha, size, first = hashlib.sha256(), 0, True
    try:
        with _open_maybe_compressed(src) as fin, dest.open("wb") as fout:
            while chunk := fin.read(4 * 1024 * 1024):
                if first:
                    first = False
                    if chunk.startswith(CSGO_MAGIC):
                        raise ServiceError("This is a CS:GO (Source 1) demo. Only CS2 demos are supported.")
                    if not chunk.startswith(CS2_MAGIC):
                        raise ServiceError("Not a CS2 demo file (missing PBDEMS2 header).")
                sha.update(chunk)
                size += len(chunk)
                if size > settings.max_upload_mb * 1024 * 1024:
                    raise ServiceError(f"Demo is larger than the {settings.max_upload_mb} MB limit.")
                fout.write(chunk)
        if size == 0:
            raise ServiceError("Empty file.")
    except (OSError, EOFError, zipfile.BadZipFile) as exc:
        dest.unlink(missing_ok=True)
        raise ServiceError(f"Could not read the demo file: {exc}") from exc
    except ServiceError:
        dest.unlink(missing_ok=True)
        raise
    finally:
        if not keep_source:
            src.unlink(missing_ok=True)

    digest = sha.hexdigest()
    existing = s.scalar(select(Match).where(Match.sha256 == digest))
    if existing is not None:
        dest.unlink(missing_ok=True)
        job = None
        if existing.status == "failed":  # give failed analyses another chance
            existing.status, existing.error = "queued", None
            job = jobs.enqueue(s, "analyze_match", {"match_id": existing.id}, match_id=existing.id, priority=50)
        return existing, job, False

    name = Path(filename).name
    for ext in (".gz", ".bz2", ".zst", ".zip"):
        name = name.removesuffix(ext)
    match = Match(id=match_id, sha256=digest, filename=name or "demo.dem", size_bytes=size, demo_path=rel,
                  options={"auto_render_top": auto_render_top, "renderer": renderer})
    s.add(match)
    s.flush()
    job = jobs.enqueue(s, "analyze_match", {"match_id": match.id}, match_id=match.id, priority=50)
    return match, job, True


# ---------------------------------------------------------------------------------------------- matches

@lru_cache(maxsize=16)
def _load_analysis_cached(path: str, mtime: float) -> MatchAnalysis:
    return MatchAnalysis.model_validate_json(Path(path).read_text(encoding="utf-8"))


def load_analysis(match: Match) -> MatchAnalysis:
    if match.status != "ready" or not match.analysis_path:
        raise Conflict(f"Match {match.id} is not analyzed yet (status: {match.status}"
                       + (f", error: {match.error}" if match.error else "") + ").")
    p = abs_path(match.analysis_path)
    return _load_analysis_cached(str(p), p.stat().st_mtime)


def get_match(s: Session, match_id: str) -> Match:
    m = s.get(Match, match_id)
    if m is None:
        raise NotFound(f"Match {match_id!r} not found.")
    return m


def match_out(s: Session, m: Match) -> MatchOut:
    base = f"{get_settings().base_url}/api/v1/matches/{m.id}"
    count = s.scalar(select(func.count()).select_from(Highlight).where(Highlight.match_id == m.id)) or 0
    return MatchOut(
        id=m.id, status=m.status, error=m.error, filename=m.filename, size_bytes=m.size_bytes, sha256=m.sha256,  # type: ignore[arg-type]
        map=m.map, server_name=m.server_name, team_a=TeamScore(name=m.team_a_name, score=m.team_a_score),
        team_b=TeamScore(name=m.team_b_name, score=m.team_b_score), rounds=m.rounds, duration_s=m.duration_s,
        tickrate=m.tickrate, total_ticks=m.total_ticks, highlight_count=count, created_at=m.created_at,
        analyzed_at=m.analyzed_at, played_at=m.played_at, source_id=m.source_id, remote_id=m.remote_id,
        page_url=m.page_url, brand=m.brand,
        links={"self": base, "analysis": f"{base}/analysis", "scoreboard": f"{base}/scoreboard",
               "highlights": f"{base}/highlights", "renders": f"{base}/renders"},
    )


def list_matches(s: Session, *, limit: int = 50, offset: int = 0, status: str | None = None,
                 map_name: str | None = None) -> list[Match]:
    # Newest *played* first; matches without a known date fall back to when they were uploaded.
    q = (select(Match).order_by(func.coalesce(Match.played_at, Match.created_at).desc())
         .limit(min(limit, 200)).offset(offset))
    if status:
        q = q.where(Match.status == status)
    if map_name:
        q = q.where(Match.map == map_name)
    return list(s.scalars(q).all())


def delete_match(s: Session, match_id: str) -> None:
    m = get_match(s, match_id)
    for r in s.scalars(select(Render).where(Render.match_id == m.id)).all():
        _delete_render_files(r)
        s.delete(r)
    for h in s.scalars(select(Highlight).where(Highlight.match_id == m.id)).all():
        s.delete(h)
    for j in s.scalars(select(Job).where(Job.match_id == m.id)).all():
        if j.status in {"queued", "running"}:
            jobs.request_cancel(s, j)
    abs_path(m.demo_path).unlink(missing_ok=True)
    shutil.rmtree(get_settings().data_dir / "matches" / m.id, ignore_errors=True)
    shutil.rmtree(get_settings().data_dir / "masters" / m.id, ignore_errors=True)
    s.delete(m)


# ---------------------------------------------------------------------------------------------- highlights

def _segment_seconds(segments: list[HighlightSegment], tickrate: int) -> float:
    return sum((x.end_tick - x.start_tick) / tickrate / x.speed for x in segments)


def validate_segments(match: Match, segments: list[HighlightSegment]) -> list[HighlightSegment]:
    if not segments:
        raise ServiceError("A highlight needs at least one segment.")
    if len(segments) > MAX_SEGMENTS:
        raise ServiceError(f"Too many segments (max {MAX_SEGMENTS}).")
    tickrate, total = match.tickrate or 64, match.total_ticks or 0
    known = {p.steamid for p in load_analysis(match).players}
    for i, seg in enumerate(segments):
        if seg.start_tick < 0 or seg.end_tick > total:
            raise ServiceError(f"Segment {i}: ticks must be within 0..{total} (got {seg.start_tick}..{seg.end_tick}).")
        if seg.end_tick - seg.start_tick < tickrate // 4:
            raise ServiceError(f"Segment {i}: must be at least 0.25s long (end_tick must be > start_tick).")
        if seg.camera is not None and seg.pov_steamid is not None:
            raise ServiceError(f"Segment {i}: give either pov_steamid (a player's eyes) or camera (a fixed "
                               "position), not both. The edit operations set_pov and set_camera replace one "
                               "with the other.")
        if seg.pov_steamid is not None and seg.pov_steamid not in known:
            raise ServiceError(f"Segment {i}: pov_steamid {seg.pov_steamid!r} is not a player of this match. "
                               f"Players: {sorted(known)}")
    if _segment_seconds(segments, tickrate) > MAX_CLIP_SECONDS:
        raise ServiceError(f"Highlight is longer than {MAX_CLIP_SECONDS:.0f}s; trim it or split it in two.")
    return segments


def get_highlight(s: Session, highlight_id: str) -> Highlight:
    h = s.get(Highlight, highlight_id)
    if h is None:
        raise NotFound(f"Highlight {highlight_id!r} not found.")
    return h


def _in_category(q, category: str | None):
    """``play`` (the default everywhere: fails never mix into a list of plays), ``fail`` or ``all`` / None."""
    if category in (None, "all"):
        return q
    if category == "fail":
        return q.where(Highlight.category == "fail")
    if category == "play":
        return q.where(Highlight.category.is_(None) | (Highlight.category != "fail"))
    raise ServiceError("category must be 'play', 'fail' or 'all'.")


def list_highlights(s: Session, match_id: str, *, player_steamid: str | None = None, kind: str | None = None,
                    min_score: float | None = None, limit: int = 100,
                    featured: bool | None = None, category: str | None = "play") -> list[Highlight]:
    get_match(s, match_id)
    q = select(Highlight).where(Highlight.match_id == match_id).order_by(Highlight.score.desc(), Highlight.created_at)
    q = _in_category(q, category)
    if featured is not None:
        q = q.where(Highlight.featured.is_(True)) if featured else q.where(Highlight.featured.isnot(True))
    if player_steamid:
        q = q.where(Highlight.player_steamid == player_steamid)
    if kind:
        q = q.where(Highlight.kind == kind)
    if min_score is not None:
        q = q.where(Highlight.score >= min_score)
    return list(s.scalars(q.limit(min(limit, 500))).all())


FORMAT_SIZES = {"9:16": (1080, 1920), "1:1": (1080, 1080)}


def output_size(opts: RenderOptionsIn) -> tuple[int, int]:
    """Pixel size of the video a render request asks for."""
    if opts.width and opts.height:
        return opts.width, opts.height
    settings = get_settings()
    if opts.format in FORMAT_SIZES:
        return FORMAT_SIZES[opts.format]
    return settings.render_width, settings.render_height


def format_of(width: int | None, height: int | None, options: dict | None = None) -> str:
    if width and height:
        ratio = width / height
        return "9:16" if ratio < 0.8 else "1:1" if ratio < 1.2 else "16:9"
    return str((options or {}).get("format") or "16:9")


def render_out(s: Session, r: Render) -> RenderOut:
    stale = False
    if r.highlight_versions:
        current = dict(s.execute(select(Highlight.id, Highlight.version).where(Highlight.id.in_(r.highlight_ids))).all())
        stale = any(current.get(hid) != ver for hid, ver in r.highlight_versions.items())
    return RenderOut(
        id=r.id, match_id=r.match_id, kind=r.kind, title=r.title, highlight_ids=list(r.highlight_ids or []),  # type: ignore[arg-type]
        renderer=r.renderer, options=r.options or {}, status=r.status, error=r.error, job_id=r.job_id,  # type: ignore[arg-type]
        video_url=file_url(r.video_path) if r.status == "ready" else None,
        thumbnail_url=file_url(r.thumbnail_path) if r.status == "ready" else None,
        duration_s=r.duration_s, size_bytes=r.size_bytes, width=r.width, height=r.height, fps=r.fps, stale=stale,
        format=format_of(r.width, r.height, r.options), body_offset_s=r.body_offset_s,
        kill_marks=r.kill_marks,
        created_at=r.created_at, finished_at=r.finished_at,
    )


def curation_out(h: Highlight) -> dict[str, Any]:
    return {"team": h.team, "tier": h.tier, "featured": bool(h.featured), "featured_rank": h.featured_rank,
            "breakdown": dict(h.breakdown or {}), "category": h.category or "play", "facts": dict(h.facts or {})}


def apply_candidate(h: Highlight, c, *, keep_cut: bool = False) -> None:
    """Copy what the scoring model says about a play onto its row. ``keep_cut`` leaves the segments, title and
    description alone (the play was edited by a person or an agent)."""
    h.kind, h.round, h.score, h.tags = c.kind, c.round, c.score, list(c.tags)
    h.player_steamid, h.player_name = c.player.steamid, c.player.name
    h.team, h.tier, h.breakdown = c.team, c.tier, dict(c.breakdown)
    h.category, h.action_ticks, h.facts = c.category, list(c.action_ticks), dict(c.facts)
    if h.featured_by != "manual":
        h.featured, h.featured_rank, h.featured_by = bool(c.featured), c.featured_rank, "auto"
    if not keep_cut:
        h.title, h.description = c.title, c.description
        segments = [x.model_dump() for x in c.segments]
        if h.segments and h.segments != segments:  # the model cuts this play differently now: videos are stale
            h.version = (h.version or 1) + 1
            h.updated_at = now()
        h.segments, h.kill_ticks = segments, list(c.kill_ticks)
        h.edited = False


HAND_MADE_KINDS = ("custom", "player_round")  # what create_highlight makes; every other kind comes from the analysis


def cut_is_edited(h: Highlight) -> bool:
    """True when a person or an agent changed where this play is cut (so the model must not)."""
    if h.origin != "auto" or h.edited:
        return True
    return h.edited is None and (h.version or 1) > 1  # rows from before the `edited` column


def rescore_match(s: Session, match_id: str) -> dict[str, Any]:
    """Run the current scoring model and curation over a match that is already analyzed, without reading the
    demo again. Highlights keep their ids (and so their videos); plays the model no longer keeps are removed
    unless they have videos or were edited; plays it newly finds are added."""
    from .analysis import highlights as model

    m = get_match(s, match_id)
    analysis = load_analysis(m)
    found = model.detect(analysis.rounds, analysis.tickrate, analysis.total_ticks, analysis.map)
    plays = list(found)
    found = plays + list(analysis.fails)  # fails need the demo to be found: the stored ones are kept as they are
    every = s.scalars(select(Highlight).where(Highlight.match_id == match_id)).all()
    rows = [h for h in every if h.origin == "auto"]
    # A play that a person or an agent edited is no longer "auto", but it is still that play. A clip made by
    # hand never was one, even when it covers the same kills: the model leaves it alone and keeps its own play.
    edited = [h for h in every if h.origin != "auto" and h.kind not in HAND_MADE_KINDS]

    def key(kind: str, rnd: int | None, steamid: str | None) -> tuple:
        return (rnd, steamid, kind if kind.startswith("fail_") else kind == "ninja_defuse")

    def edited_version(c) -> Highlight | None:
        kills = set(c.kill_ticks or [])
        for h in edited:
            if h.round == c.round and h.player_steamid == c.player.steamid \
                    and (h.category or "play") == c.category and (
                    kills & set(h.kill_ticks or []) or (not kills and h.kind == c.kind)):
                return h
        return None

    existing = {key(h.kind, h.round, h.player_steamid): h for h in rows}
    with_video = {hid for r in s.scalars(select(Render).where(Render.match_id == match_id)).all()
                  for hid in (r.highlight_ids or [])}
    updated = added = removed = 0
    seen: set[str] = set()
    for c in found:
        h = edited_version(c)
        if h is not None:
            twin = existing.get(key(c.kind, c.round, c.player.steamid))
            if twin is not None and (twin.version or 1) == 1:  # an untouched copy of the same play
                for r in s.scalars(select(Render).where(Render.match_id == match_id)).all():
                    if twin.id in (r.highlight_ids or []):
                        delete_render(s, r.id)
                s.delete(twin)
                seen.add(twin.id)
                removed += 1
        else:
            h = existing.get(key(c.kind, c.round, c.player.steamid))
        if h is None:
            h = Highlight(match_id=match_id, kind=c.kind, title=c.title, description=c.description, origin="auto")
            s.add(h)
            added += 1
        else:
            updated += 1
        apply_candidate(h, c, keep_cut=cut_is_edited(h))
        s.flush()
        seen.add(h.id)
    for h in rows:
        if h.id in seen:
            continue
        if h.id in with_video or (h.version or 1) > 1 or h.featured_by == "manual":
            h.tier, h.breakdown = "C", {}
            if h.featured_by != "manual":
                h.featured, h.featured_rank, h.featured_by = False, None, "auto"
        else:
            s.delete(h)
            removed += 1
    analysis.highlights = plays
    if m.analysis_path:
        abs_path(m.analysis_path).write_text(analysis.model_dump_json(), encoding="utf-8")
    return {"match_id": match_id, "highlights": len(plays), "fails": len(found) - len(plays),
            "featured": sum(1 for c in found if c.featured),
            "updated": updated, "added": added, "removed": removed}


def _chat_of(m: Match | None, h: Highlight) -> list[dict[str, Any]]:
    """Chat lines reacting to the kills of a play (cheap: the analysis is cached per match)."""
    if m is None or not h.kill_ticks or m.status != "ready":
        return []
    try:
        from .analysis import chat as chat_model

        return chat_model.reactions(load_analysis(m).chat, list(h.kill_ticks))
    except ServiceError:
        return []


def highlight_out(s: Session, h: Highlight, *, with_renders: bool = True) -> HighlightOut:
    m = s.get(Match, h.match_id)
    tickrate = (m.tickrate if m else None) or 64
    segments = [HighlightSegment.model_validate(x) for x in (h.segments or [])]
    renders: list[RenderOut] = []
    if with_renders:
        rows = s.scalars(select(Render).where(Render.match_id == h.match_id, Render.kind == "highlight")
                         .order_by(Render.created_at.desc())).all()
        renders = [render_out(s, r) for r in rows if h.id in (r.highlight_ids or [])]
    return HighlightOut(
        id=h.id, match_id=h.match_id, kind=h.kind, title=h.title, description=h.description or "", round=h.round,
        player=HighlightPlayer(steamid=h.player_steamid, name=h.player_name), score=h.score, tags=list(h.tags or []),
        segments=segments, kill_ticks=list(h.kill_ticks or []), chat=_chat_of(m, h), **curation_out(h),
        duration_s=round(_segment_seconds(segments, tickrate), 1), origin=h.origin, version=h.version,  # type: ignore[arg-type]
        renders=renders, created_at=h.created_at, updated_at=h.updated_at,
    )


def create_highlight(s: Session, match_id: str, body: HighlightCreate) -> Highlight:
    m = get_match(s, match_id)
    analysis = load_analysis(m)
    tickrate = analysis.tickrate
    names = {p.steamid: p.name for p in analysis.players}
    kind, rnd, player, kill_ticks = "custom", body.round, body.player_steamid or body.pov_steamid, []

    if body.segments:
        segments = list(body.segments)
    elif body.start_tick is not None and body.end_tick is not None:
        segments = [HighlightSegment(start_tick=body.start_tick, end_tick=body.end_tick, pov_steamid=body.pov_steamid)]
    elif body.round is not None and body.player_steamid:
        r = next((x for x in analysis.rounds if x.number == body.round), None)
        if r is None:
            raise ServiceError(f"Round {body.round} does not exist (match has {analysis.rounds_played} rounds).")
        kills = [k for k in r.kills if k.attacker and k.attacker.steamid == body.player_steamid
                 and not k.is_teamkill and not k.is_suicide]
        if not kills:
            raise ServiceError(f"Player {body.player_steamid} has no kills in round {body.round}. "
                               "Pass start_tick/end_tick instead to clip any moment.")
        from .analysis.highlights import HighlightConfig, _segments

        kill_ticks = [k.tick for k in kills]
        segments = _segments(kill_ticks, body.player_steamid, r, tickrate, HighlightConfig(), analysis.total_ticks,
                             onsets={k.tick: k.fight_start_tick for k in r.kills if k.fight_start_tick is not None})
        kind = "player_round"
    else:
        raise ServiceError("Provide `segments`, or `start_tick` + `end_tick`, or `round` + `player_steamid`.")

    validate_segments(m, segments)
    if rnd is None:
        rnd = next((x.number for x in analysis.rounds
                    if x.start_tick <= segments[0].start_tick <= x.official_end_tick), None)
    if player is None:
        player = segments[0].pov_steamid
    title = body.title or (f"{names.get(player, 'Custom')} — round {rnd}" if rnd else "Custom highlight")
    h = Highlight(match_id=m.id, kind=kind, title=title, description=body.description or "", round=rnd,
                  player_steamid=player, player_name=names.get(player) if player else None, score=0.0,
                  tags=list(body.tags), segments=[x.model_dump() for x in segments], kill_ticks=kill_ticks,
                  origin=body.origin, category="fail" if body.category == "fail" else None)
    s.add(h)
    s.flush()
    return h


def _touch(h: Highlight, segments: list[HighlightSegment] | None = None) -> None:
    if segments is not None:
        h.segments = [x.model_dump() for x in segments]
        h.edited = True
    h.version = (h.version or 1) + 1
    h.updated_at = now()


def patch_highlight(s: Session, highlight_id: str, body: HighlightPatch) -> Highlight:
    h = get_highlight(s, highlight_id)
    segments = None
    if body.segments is not None:
        segments = validate_segments(get_match(s, h.match_id), list(body.segments))
    if body.title is not None:
        h.title = body.title
    if body.description is not None:
        h.description = body.description
    if body.tags is not None:
        h.tags = list(body.tags)
    if body.category is not None and body.category != (h.category or "play"):
        if h.kind not in HAND_MADE_KINDS:
            raise ServiceError(f"Highlight {h.id} is a {h.kind!r} found by the analysis, which decides its category "
                               f"({h.category or 'play'}). Only hand-made clips can be moved: create one with "
                               "`segments` or a tick range and give it the category you want.")
        h.category = "fail" if body.category == "fail" else None  # where it is listed, not how it looks: no new version
        h.updated_at = now()
    if body.featured is not None:
        h.featured, h.featured_by = bool(body.featured), "manual"
        if not body.featured:
            h.featured_rank = None
    elif "featured" in body.model_fields_set:  # explicit null: back to the automatic curation
        h.featured_by = None
        s.flush()
        try:
            rescore_match(s, h.match_id)
        except ServiceError:
            h.featured, h.featured_rank = False, None
    if segments is not None or body.title is not None or body.description is not None or body.tags is not None:
        _touch(h, segments)
    return h


def edit_highlight(s: Session, highlight_id: str, req: EditRequest) -> Highlight:
    """Apply edit operations in order. Nothing is saved unless every operation and the final result are valid."""
    h = get_highlight(s, highlight_id)
    m = get_match(s, h.match_id)
    tickrate = m.tickrate or 64
    segs = [HighlightSegment.model_validate(x) for x in (h.segments or [])]
    title, description, tags = h.title, h.description, list(h.tags or [])

    def at(i: int) -> HighlightSegment:
        if not 0 <= i < len(segs):
            raise ServiceError(f"Segment index {i} out of range (highlight has {len(segs)} segment(s), 0-based).")
        return segs[i]

    for n, op in enumerate(req.ops):
        try:
            if op.op == "trim":
                seg = at(op.segment)
                seg.start_tick += int(round(op.start_delta_s * tickrate))
                seg.end_tick += int(round(op.end_delta_s * tickrate))
            elif op.op == "set_range":
                seg = at(op.segment)
                seg.start_tick, seg.end_tick = op.start_tick, op.end_tick
            elif op.op == "set_pov":  # a player, or the free camera: in both cases no fixed camera any more
                for seg in (segs if op.segment is None else [at(op.segment)]):
                    seg.pov_steamid, seg.camera = op.steamid, None
            elif op.op == "set_camera":  # a fixed spot instead of a player; null = back to the free camera
                for seg in (segs if op.segment is None else [at(op.segment)]):
                    seg.camera = op.camera.model_copy() if op.camera is not None else None
                    if op.camera is not None:
                        seg.pov_steamid = None
            elif op.op == "set_speed":
                for seg in (segs if op.segment is None else [at(op.segment)]):
                    seg.speed = op.speed
            elif op.op == "set_caption":
                at(op.segment).caption = op.caption
            elif op.op == "add_segment":
                new = HighlightSegment(start_tick=op.start_tick, end_tick=op.end_tick, pov_steamid=op.pov_steamid,
                                       camera=op.camera, speed=op.speed, caption=op.caption)
                segs.insert(len(segs) if op.index is None else max(0, min(op.index, len(segs))), new)
            elif op.op == "remove_segment":
                at(op.segment)
                segs.pop(op.segment)
            elif op.op == "move_segment":
                seg = at(op.segment)
                segs.pop(op.segment)
                segs.insert(max(0, min(op.to, len(segs))), seg)
            elif op.op == "split_segment":
                seg = at(op.segment)
                if not seg.start_tick < op.at_tick < seg.end_tick:
                    raise ServiceError(f"at_tick must be inside the segment ({seg.start_tick}..{seg.end_tick}).")
                tail = seg.model_copy(update={"start_tick": op.at_tick})
                seg.end_tick = op.at_tick
                segs.insert(op.segment + 1, tail)
            elif op.op == "set_meta":
                title = op.title if op.title is not None else title
                description = op.description if op.description is not None else description
                tags = list(op.tags) if op.tags is not None else tags
        except ServiceError as exc:
            raise ServiceError(f"Operation #{n} ({op.op}): {exc.message}") from exc

    validate_segments(m, segs)
    h.title, h.description, h.tags = title, description, tags
    if req.origin:
        h.origin = req.origin
    _touch(h, segs)
    return h


def delete_highlight(s: Session, highlight_id: str) -> None:
    s.delete(get_highlight(s, highlight_id))


# ---------------------------------------------------------------------------------------------- renders

def renderers_status() -> list[dict[str, Any]]:
    """Renderers as this *server* can use them: a renderer the machine could run but that is switched off in
    the configuration is reported as unavailable, with the setting to change as the reason."""
    from .render import describe_renderers

    out = describe_renderers()
    if not get_settings().ingame_enabled:
        from . import workers

        with session_scope() as s:
            remote = workers.recording_workers(s)
        for r in out:
            if r["name"] == "ingame":
                r["details"] = {**r.get("details", {}), "machine_ready": r["available"]}
                if remote:
                    # Nothing records here, but a remote worker with the game takes those jobs.
                    r["available"], r["reason"] = True, None
                    r["details"]["remote_workers"] = remote
                    continue
                r["available"] = False
                r["reason"] = ("Disabled on this server. Set DEMOREEL_INGAME_ENABLED=1 on a machine with CS2 "
                               "(see docs/ingame-recording.md), or register a remote worker that has the game "
                               "(see docs/workers.md)."
                               + ("" if r["details"]["machine_ready"] else f" Also: {r['reason']}"))
    return out


def records_here() -> bool:
    """True when this machine itself can record with the game."""
    if not get_settings().ingame_enabled:
        return False
    try:
        from .render import get_renderer

        return bool(get_renderer("ingame").availability().ok)
    except Exception:  # noqa: BLE001 - a broken optional renderer must not break render requests
        return False


def resolve_renderer(name: str | None) -> str:
    """Turn 'auto'/None into a concrete renderer and make sure this deployment can run it."""
    from . import workers
    from .render import CAPABILITY

    settings = get_settings()
    name = (name or settings.default_renderer or "auto").lower()
    if name == "auto":
        return "ingame" if records_here() else "radar"
    if name not in CAPABILITY:
        raise ServiceError(f"Unknown renderer {name!r}. Use one of: auto, {', '.join(CAPABILITY)}.")
    if name == "ingame" and not settings.ingame_enabled:
        with session_scope() as s:
            remote = workers.recording_workers(s)
        if not remote:
            raise Conflict("The in-game renderer is disabled on this server. Set DEMOREEL_INGAME_ENABLED=1 on a "
                           "machine with CS2 installed (see docs/ingame-recording.md), register a remote worker "
                           "that has the game (see docs/workers.md), or use renderer='radar'.")
    return name


def _plan_renderer(s: Session, m: Match, highlights: list[Highlight], renderer: str | None,
                   opts: RenderOptionsIn) -> tuple[str, str, str | None]:
    """Pick the renderer, the kind of worker that can run the job and - when only one remote worker can,
    because it keeps the footage - that worker's id.

    First-person footage that was already recorded is kept as a *master*; a render that such a master covers
    needs no game at all, so it runs on any worker — even when in-game recording is disabled here. Footage a
    remote worker keeps counts too: the job is then addressed to that worker."""
    from . import workers
    from .compose import master as masters
    from .render import CAPABILITY
    from .render.base import ClipSegment, segment_view

    settings = get_settings()
    want = (renderer or settings.default_renderer or "auto").lower()
    if want not in {"auto", *CAPABILITY}:
        raise ServiceError(f"Unknown renderer {want!r}. Use one of: auto, {', '.join(CAPABILITY)}.")
    if want == "radar":
        return "radar", CAPABILITY["radar"], None
    segments = [ClipSegment(x["start_tick"], x["end_tick"], segment_view(x), float(x.get("speed") or 1.0))
                for h in highlights for x in (h.segments or [])]
    need = masters.capture_fps_for(segments, opts.fps or settings.render_fps)
    # Footage is matched by the size it is *recorded* at, not by the shape of the video being made.
    try:
        kills = masters.xray.match_kills(load_analysis(m))
    except ServiceError:
        kills = None  # no analysis to consult: stored footage is matched without the x-ray question
    kw = {"width": settings.ingame_capture_width, "height": settings.ingame_capture_height, "extra": opts.extra,
          "kills": kills, "tickrate": float(m.tickrate or 64)}
    recapture = bool(opts.extra.get("ingame_recapture"))
    if not recapture and masters.find_master(m.id, segments, min_fps=need, **kw) is not None:
        return "ingame", "cpu", None  # footage on disk: no game needed
    if not recapture:
        holder = workers.footage_holder(s, m.id, segments, min_fps=need, **kw)
        if holder is not None:
            return "ingame", "cpu", holder  # footage on a remote worker: it edits, no game needed
    if records_here() or workers.recording_workers(s):
        return "ingame", CAPABILITY["ingame"], None
    if masters.find_master(m.id, segments, min_fps=need, allow_lower_fps=True, **kw) is not None:
        return "ingame", "cpu", None  # usable footage at a lower frame rate
    holder = workers.footage_holder(s, m.id, segments, min_fps=need, allow_lower_fps=True, **kw)
    if holder is not None:
        return "ingame", "cpu", holder
    if want == "auto":
        return "radar", CAPABILITY["radar"], None
    return resolve_renderer("ingame"), CAPABILITY["ingame"], None  # raises the "disabled on this server" conflict


def create_render(s: Session, match_id: str, highlight_ids: list[str], *, renderer: str | None = None,
                  options: RenderOptionsIn | None = None, kind: str = "highlight", title: str | None = None,
                  priority: int = 100) -> tuple[Render, Job]:

    m = get_match(s, match_id)
    load_analysis(m)  # raises Conflict if not ready
    if not highlight_ids:
        raise ServiceError("No highlights to render.")
    rows = {h.id: h for h in s.scalars(select(Highlight).where(Highlight.id.in_(highlight_ids))).all()}
    for hid in highlight_ids:
        if hid not in rows or rows[hid].match_id != m.id:
            raise NotFound(f"Highlight {hid!r} not found in match {m.id}.")
    opts_in = options or RenderOptionsIn()
    opts = opts_in.model_dump(exclude_none=True)
    name, capability, target = _plan_renderer(s, m, [rows[h] for h in highlight_ids], renderer, opts_in)
    render = Render(match_id=m.id, kind=kind, title=title or (rows[highlight_ids[0]].title if kind == "highlight" else None),
                    highlight_ids=list(highlight_ids), highlight_versions={hid: rows[hid].version for hid in highlight_ids},
                    renderer=name, options=opts)
    s.add(render)
    s.flush()
    job = jobs.enqueue(s, "render", {"render_id": render.id}, capability=capability, match_id=m.id,
                       render_id=render.id, priority=priority, target_worker=target)
    render.job_id = job.id
    return render, job


def get_render(s: Session, render_id: str) -> Render:
    r = s.get(Render, render_id)
    if r is None:
        raise NotFound(f"Render {render_id!r} not found.")
    return r


# ---------------------------------------------------------------- results of jobs, applied to the database
# A job has two halves: the heavy work (parse a demo, record, edit) and writing down what came out of it.
# The second half lives here so that it is the same whether the work ran on this machine or on a remote worker.

def store_analysis(s: Session, match_id: str, analysis: MatchAnalysis) -> dict[str, Any]:
    """Save a finished analysis: the JSON on disk, the match row, its plays, and the videos made automatically.
    Returns the job result. A re-analysis keeps the plays' ids (and so their videos)."""
    settings = get_settings()
    m = get_match(s, match_id)
    opts = dict(m.options or {})
    rel = f"matches/{match_id}/analysis.json"
    settings.path(*rel.split("/")).write_text(analysis.model_dump_json(), encoding="utf-8")
    m.analysis_path, m.status, m.analyzed_at, m.error = rel, "ready", now(), None
    m.map, m.server_name = analysis.map, analysis.server_name
    m.team_a_name, m.team_a_score = analysis.team_a.name, analysis.team_a.score
    m.team_b_name, m.team_b_score = analysis.team_b.name, analysis.team_b.score
    m.rounds, m.duration_s = analysis.rounds_played, analysis.duration_s
    m.tickrate, m.total_ticks = analysis.tickrate, analysis.total_ticks
    created: list[Highlight] = []
    if s.scalars(select(Highlight.id).where(Highlight.match_id == match_id)).first() is not None:
        # Re-analysis: the plays keep their ids (and so their videos); cuts and scores follow the new
        # analysis, except where a person or an agent edited them.
        s.flush()
        rescore_match(s, match_id)
    else:
        for c in [*analysis.highlights, *analysis.fails]:
            h = Highlight(match_id=match_id, kind=c.kind, title=c.title, description=c.description, origin="auto")
            apply_candidate(h, c)
            s.add(h)
            created.append(h)
        s.flush()
        created.sort(key=lambda h: (h.featured_rank or 10**6, -h.score))  # auto-render the featured plays first

    render_jobs: list[str] = []
    top = opts.get("auto_render_top")
    top = settings.auto_render_top if top is None else int(top)
    for h in created[: max(0, top)]:
        try:
            _, job = create_render(s, match_id, [h.id], renderer=opts.get("renderer"), priority=200)
            render_jobs.append(job.id)
        except ServiceError as exc:
            log.warning("auto-render skipped for %s: %s", h.id, exc.message)
    return {"match_id": match_id, "map": analysis.map, "rounds": analysis.rounds_played,
            "highlights": len(created), "render_job_ids": render_jobs, "warnings": analysis.warnings,
            "score": [analysis.team_a.score, analysis.team_b.score]}


def finish_render(s: Session, render_id: str, *, duration_s: float, width: int, height: int, fps: int,
                  body_offset_s: float | None = None, kill_marks: list[dict[str, Any]] | None = None) -> RenderOut | None:
    """Mark a video as ready. Its file (and poster) must already be at ``renders/<id>.mp4`` (``.jpg``).
    ``kill_marks`` are in video time. Returns ``None`` when the render was deleted while it was being made."""
    settings = get_settings()
    r = s.get(Render, render_id)
    if r is None:
        return None
    video_rel, thumb_rel = f"renders/{render_id}.mp4", f"renders/{render_id}.jpg"
    r.status, r.finished_at, r.error = "ready", now(), None
    r.video_path = video_rel
    r.thumbnail_path = thumb_rel if (settings.data_dir / thumb_rel).is_file() else None
    r.duration_s, r.width, r.height, r.fps = round(float(duration_s), 2), int(width), int(height), int(fps)
    r.size_bytes = (settings.data_dir / video_rel).stat().st_size
    r.body_offset_s = body_offset_s
    if kill_marks is not None:
        r.kill_marks = [{"tick": int(k["tick"]), "t": round(float(k["t"]), 3)} for k in kill_marks]
    return render_out(s, r)


def queue_after_capture(s: Session, match_id: str, highlight_ids: list[str], formats: list[str]) -> int:
    """Queue the first-person videos of plays that were just recorded. Returns how many were queued."""
    queued = 0
    for hid in highlight_ids:
        for fmt in formats:
            try:
                create_render(s, match_id, [hid], renderer="ingame", options=RenderOptionsIn(format=fmt),
                              priority=150)
                queued += 1
            except ServiceError as exc:
                log.warning("render not queued for %s (%s): %s", hid, fmt, exc.message)
    return queued


def list_renders(s: Session, match_id: str) -> list[Render]:
    get_match(s, match_id)
    return list(s.scalars(select(Render).where(Render.match_id == match_id).order_by(Render.created_at.desc())).all())


def _delete_render_files(r: Render) -> None:
    for rel in (r.video_path, r.thumbnail_path):
        if rel:
            abs_path(rel).unlink(missing_ok=True)


def delete_render(s: Session, render_id: str) -> None:
    r = get_render(s, render_id)
    if r.job_id:
        j = s.get(Job, r.job_id)
        if j and j.status in {"queued", "running"}:
            jobs.request_cancel(s, j)
    _delete_render_files(r)
    s.delete(r)


def pick_top_highlights(s: Session, match_id: str, top: int, player_steamid: str | None = None) -> list[str]:
    rows = list_highlights(s, match_id, player_steamid=player_steamid, limit=top)
    if not rows:
        raise ServiceError("This match has no highlights to pick from.")
    # A reel reads better in match order than in score order.
    rows.sort(key=lambda h: (h.segments[0]["start_tick"] if h.segments else 0))
    return [h.id for h in rows]


# ---------------------------------------------------------------------------------------------- brand & feed

def brand_out(brand=None) -> BrandOut:
    from .brand import active_brand

    b = brand or active_brand()
    logo = f"brands/{b.slug}/{b.logo}" if b.logo_path is not None else None
    return BrandOut(slug=b.slug, name=b.name, tagline=b.tagline, url=b.url, logo_url=file_url(logo),
                    colors=b.colors.model_dump(), fonts=b.fonts.model_dump(), locale=b.locale, ui=dict(b.ui))


def feed(s: Session, *, limit: int = 24, player_steamid: str | None = None, kind: str | None = None,
         featured: bool | None = None, since: datetime | None = None, until: datetime | None = None,
         offset: int = 0, total: list[int] | None = None, category: str | None = "play",
         exclude: Iterable[str] = ()) -> list[FeedItem]:
    """The best plays that already have a video, across every match. In-game footage wins over the 2D replay
    when a play has both; among equals the newest video is shown. ``exclude`` leaves out the plays of those
    matches (a site that hid a match keeps its clips out of the feed)."""
    def rank(r: Render) -> tuple[int, int]:  # POV before 2D, landscape before vertical (a feed poster is 16:9)
        return (0 if r.renderer == "ingame" else 1, 0 if format_of(r.width, r.height, r.options) == "16:9" else 1)

    best: dict[str, Render] = {}
    rows = s.scalars(select(Render).where(Render.status == "ready", Render.kind == "highlight")
                     .order_by(Render.created_at.desc())).all()
    for r in rows:
        for hid in r.highlight_ids or []:
            cur = best.get(hid)
            if cur is None or rank(r) < rank(cur):
                best[hid] = r
    if not best:
        if total is not None:
            total.append(0)
        return []
    q = select(Highlight).where(Highlight.id.in_(best)).order_by(Highlight.score.desc(), Highlight.created_at.desc())
    q = _in_category(q, category)
    if featured is not None:
        q = q.where(Highlight.featured.is_(True)) if featured else q.where(Highlight.featured.isnot(True))
    if player_steamid:
        q = q.where(Highlight.player_steamid == player_steamid)
    if kind:
        q = q.where(Highlight.kind == kind)
    left_out = sorted(set(exclude))
    if left_out:
        q = q.where(Highlight.match_id.notin_(left_out))
    if since is not None or until is not None:  # by when the match was played (or uploaded, if unknown)
        when = func.coalesce(Match.played_at, Match.created_at)
        q = q.join(Match, Match.id == Highlight.match_id)
        if since is not None:
            q = q.where(when >= since)
        if until is not None:
            q = q.where(when < until)
    if total is not None:  # how many plays match, before paging
        total.append(s.scalar(select(func.count()).select_from(q.order_by(None).subquery())) or 0)
    q = q.offset(max(0, offset))
    out: list[FeedItem] = []
    matches: dict[str, Match | None] = {}
    for h in s.scalars(q.limit(min(max(limit, 1), 200))).all():
        if h.match_id not in matches:
            matches[h.match_id] = s.get(Match, h.match_id)
        m = matches[h.match_id]
        if m is None:
            continue
        segs = [HighlightSegment.model_validate(x) for x in (h.segments or [])]
        out.append(FeedItem(
            highlight=FeedHighlight(id=h.id, kind=h.kind, title=h.title, description=h.description or "",
                                    round=h.round, player=HighlightPlayer(steamid=h.player_steamid, name=h.player_name),
                                    score=h.score, tags=list(h.tags or []), **curation_out(h),
                                    duration_s=round(_segment_seconds(segs, m.tickrate or 64), 1),
                                    segments=segs, kill_ticks=list(h.kill_ticks or [])),
            match=FeedMatch(id=m.id, tickrate=m.tickrate, map=m.map, team_a=TeamScore(name=m.team_a_name, score=m.team_a_score),
                            team_b=TeamScore(name=m.team_b_name, score=m.team_b_score), played_at=m.played_at),
            render=render_out(s, best[h.id])))
    return out


# ---------------------------------------------------------------------------------------------- sources

def _active_imports(s: Session, source_id: str) -> set[str]:
    rows = s.scalars(select(Job).where(Job.type == "import_remote", Job.status.in_(("queued", "running")))).all()
    return {str((j.payload or {}).get("remote_id")) for j in rows if (j.payload or {}).get("source_id") == source_id}


def remote_matches(s: Session, source) -> list[dict[str, Any]]:
    from . import sources as src

    imported = {m.remote_id: m for m in s.scalars(select(Match).where(Match.source_id == source.id)).all()}
    importing = _active_imports(s, source.id)
    out = []
    for r in src.list_remote(source):
        m = imported.get(r.remote_id)
        out.append({**r.model_dump(), "match_id": m.id if m else None,
                    "status": m.status if m else ("importing" if r.remote_id in importing else None)})
    return out


def import_remote(s: Session, source, remote_ids: list[str],
                  auto_render_top: int | None = None) -> tuple[list[Job], list[str]]:
    """Queue the download + analysis of matches listed by a source. Already imported ones are skipped."""
    from . import sources as src

    known = {r.remote_id: r for r in src.list_remote(source)}
    have = {m.remote_id for m in s.scalars(select(Match).where(Match.source_id == source.id)).all()
            if m.status != "failed"}
    busy = _active_imports(s, source.id)
    queued, skipped = [], []
    for rid in dict.fromkeys(remote_ids):
        r = known.get(rid)
        if r is None:
            raise NotFound(f"{source.name} has no match {rid!r}.")
        if not r.has_demo or rid in have or rid in busy:
            skipped.append(rid)
            continue
        queued.append(jobs.enqueue(s, "import_remote", {"source_id": source.id, "remote_id": rid,
                                                         "auto_render_top": auto_render_top}, priority=60))
    return queued, skipped


def produce(s: Session, match_id: str, *, renderers: list[str], formats: list[str],
            highlight_ids: list[str] | None = None, force: bool = False, pov: str = "featured") -> dict[str, Any]:
    """Queue every video of a match: each play in each requested renderer and shape.

    First-person videos are made for the *featured* plays only (``pov="featured"``, the default): the best of
    each team plus the stand-out plays, as picked by the curation or by hand. ``pov="all"``, or naming the
    plays in ``highlight_ids``, lifts that. 2D replays are made for every play.

    First-person videos need footage. Plays without a stored capture are recorded together in **one** game
    session (a ``capture_match`` job), and their renders are queued when it finishes; plays that already have
    footage, and all 2D renders, are queued right away."""
    from .compose import master as masters
    from .render.base import ClipSegment, segment_view

    settings = get_settings()
    m = get_match(s, match_id)
    analysis = load_analysis(m)
    rows = list_highlights(s, match_id, limit=500, category="all")
    if highlight_ids is not None:
        wanted = set(highlight_ids)
        rows = [h for h in rows if h.id in wanted]
    if not rows:
        raise ServiceError("This match has no highlights to produce.")
    if pov not in ("featured", "all"):
        raise ServiceError("pov must be 'featured' or 'all'.")
    # Matches analyzed before the curation existed have nobody featured: fall back to every play.
    curated = any(h.featured is not None for h in rows)
    pov_ids = {h.id for h in rows if pov == "all" or highlight_ids is not None or not curated or h.featured}
    done: set[tuple[str, str, str]] = set()
    if not force:
        versions = {h.id: h.version for h in rows}
        for r in s.scalars(select(Render).where(Render.match_id == match_id, Render.kind == "highlight",
                                                Render.status.in_(("ready", "queued", "running")))).all():
            for hid in r.highlight_ids or []:
                if (r.highlight_versions or {}).get(hid) == versions.get(hid):
                    done.add((hid, r.renderer, format_of(r.width, r.height, r.options)))
    queued = skipped = 0
    to_capture: list[str] = []
    capture_formats: dict[str, list[str]] = {}
    from . import workers

    # Recording can happen on this machine or on a remote worker that has the game.
    can_record = "ingame" in renderers and (records_here() or bool(workers.recording_workers(s)))
    no_footage = 0
    kw = {"width": settings.ingame_capture_width, "height": settings.ingame_capture_height, "extra": {},
          "kills": masters.xray.match_kills(analysis), "tickrate": float(m.tickrate or 64)}
    for h in rows:
        segs = [ClipSegment(x["start_tick"], x["end_tick"], segment_view(x), float(x.get("speed") or 1.0))
                for x in (h.segments or [])]
        segs = [sg for c in masters.recordable([Clip_(segs)], m.total_ticks or 0) for sg in c.segments]
        need = masters.capture_fps_for(segs, settings.render_fps)
        covered = bool(segs) and (
            masters.find_master(match_id, segs, min_fps=need, allow_lower_fps=not can_record, **kw) is not None
            or workers.footage_holder(s, match_id, segs, min_fps=need, allow_lower_fps=not can_record,
                                      **kw) is not None)
        for renderer in renderers:
            if renderer == "ingame" and h.id not in pov_ids:
                continue
            for fmt in formats:
                if (h.id, renderer, fmt) in done:
                    skipped += 1
                    continue
                if renderer == "radar" or covered:
                    create_render(s, match_id, [h.id], renderer=renderer, options=RenderOptionsIn(format=fmt),
                                  priority=150 if renderer == "ingame" else 170)
                    queued += 1
                elif can_record and segs:
                    capture_formats.setdefault(h.id, []).append(fmt)
                else:
                    no_footage += 1
        if h.id in capture_formats:
            to_capture.append(h.id)
    capture_job = None
    pending = sum(len(v) for v in capture_formats.values())
    if to_capture:
        fmts = sorted({f for v in capture_formats.values() for f in v})
        capture_job = jobs.enqueue(s, "capture_match", {"match_id": match_id, "highlight_ids": to_capture,
                                                        "then": {"formats": fmts}},
                                   capability="ingame", match_id=match_id, priority=120)
    detail = None
    if no_footage:
        detail = (f"{no_footage} first-person video(s) were not queued: the footage has not been recorded and this "
                  "server cannot run CS2 (set DEMOREEL_INGAME_ENABLED=1 on a machine with the game, or register "
                  "a remote worker that has it: docs/workers.md).")
    return {"capture_job": capture_job, "render_jobs": queued, "pending_after_capture": pending,
            "skipped": skipped, "detail": detail, "featured": len(pov_ids) if "ingame" in renderers else 0}


def Clip_(segments):  # noqa: N802 - tiny local constructor to avoid importing render.base at module import time
    from .render.base import Clip

    return Clip(segments=list(segments))


# ---------------------------------------------------------------------------------------------- jobs

def job_out(j: Job) -> JobOut:
    return JobOut(id=j.id, type=j.type, status=j.status, progress=round(j.progress or 0.0, 3), message=j.message,  # type: ignore[arg-type]
                  error=j.error, result=j.result, match_id=j.match_id, render_id=j.render_id,
                  created_at=j.created_at, started_at=j.started_at, finished_at=j.finished_at,
                  done=j.status in jobs.TERMINAL)


def get_job(s: Session, job_id: str) -> Job:
    j = s.get(Job, job_id)
    if j is None:
        raise NotFound(f"Job {job_id!r} not found.")
    return j


MAX_THREAD_WAITS = 8  # threads parked in wait_job at once; the other routes share the same ~40
MAX_TASK_WAITS = 256  # REST long-polls waiting at once (they hold no thread, only memory and a look every 0.5 s)
_thread_waits = threading.BoundedSemaphore(MAX_THREAD_WAITS)
_task_waits = 0  # only changed on the event loop


def _look_at_job(job_id: str) -> JobOut:
    with session_scope() as s:
        return job_out(get_job(s, job_id))


def wait_job(job_id: str, timeout_s: float) -> JobOut:
    """Block (up to ``timeout_s``) until the job finishes. Lets agents avoid tight polling loops.

    When enough threads are already waiting, the answer comes at once: a long wait must not take every thread
    the server has (``done`` says whether to ask again)."""
    deadline = time.monotonic() + max(0.0, min(timeout_s, 120.0))
    out = _look_at_job(job_id)
    if out.done or time.monotonic() >= deadline or not _thread_waits.acquire(blocking=False):
        return out
    try:
        while not out.done and time.monotonic() < deadline:
            time.sleep(0.5)
            out = _look_at_job(job_id)
        return out
    finally:
        _thread_waits.release()


async def wait_job_async(job_id: str, timeout_s: float) -> JobOut:
    """``wait_job`` without holding a thread while it waits (each look at the job borrows one for a moment)."""
    import anyio

    global _task_waits
    deadline = time.monotonic() + max(0.0, min(timeout_s, 120.0))
    out = await anyio.to_thread.run_sync(_look_at_job, job_id)
    if out.done or time.monotonic() >= deadline or _task_waits >= MAX_TASK_WAITS:
        return out
    _task_waits += 1
    try:
        while not out.done and time.monotonic() < deadline:
            await anyio.sleep(0.5)
            out = await anyio.to_thread.run_sync(_look_at_job, job_id)
        return out
    finally:
        _task_waits -= 1


def scoreboard(analysis: MatchAnalysis) -> dict[str, Any]:
    """Compact scoreboard: the most-used stats per player, grouped by team."""
    def row(p) -> dict[str, Any]:
        return {"steamid": p.steamid, "name": p.name, "kills": p.kills, "deaths": p.deaths, "assists": p.assists,
                "adr": p.adr, "kast": p.kast, "hs_pct": p.headshot_pct, "rating": p.rating,
                "opening_kills": p.opening_kills, "opening_deaths": p.opening_deaths,
                "clutches_won": p.clutches_won, "multi_kills": p.multi_kills, "mvps": p.mvps}
    teams = []
    for t in (analysis.team_a, analysis.team_b):
        teams.append({"key": t.key, "name": t.name, "score": t.score, "won": t.won,
                      "players": [row(p) for p in analysis.players if p.team == t.key]})
    return {"map": analysis.map, "rounds_played": analysis.rounds_played, "teams": teams}
