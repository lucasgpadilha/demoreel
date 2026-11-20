"""Job handlers executed by worker processes."""

from __future__ import annotations

import logging
import shutil
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import select

from . import jobs, service
from .analysis import analyze_demo
from .brand import brand_for
from .compose.pipeline import finish
from .config import get_settings
from .db import Highlight, session_scope
from .render import Clip, ClipSegment, RenderJob, RenderOptions, get_renderer
from .render.base import RecorderBusy, RenderCancelled, RenderError
from .schemas import RenderOptionsIn
from .webhooks import emit

log = logging.getLogger("demoreel.tasks")

WAITING_FOR_GAME = "Waiting for another in-game recording to finish"


@jobs.reserve("ingame")
@contextmanager
def _take_game() -> Iterator[None]:
    """A worker takes this machine's game before it takes a recording job, and keeps it for the job. While
    another process records, the workers here leave recording jobs in the queue, in their order, and stay
    free for the work that needs no game (editing what was already recorded)."""
    renderer = get_renderer("ingame")
    hold = getattr(renderer, "recorder", None)
    if hold is None:  # a renderer with nothing to share
        yield
        return
    taken = getattr(renderer, "recorder_taken", None)
    if taken is not None and taken():  # the usual answer while a match is being recorded, and a cheap one
        raise jobs.Taken()
    with ExitStack() as stack:
        try:
            stack.enter_context(hold())
        except RecorderBusy as exc:
            raise jobs.Taken() from exc
        yield


@contextmanager
def _recorder(ctx: jobs.JobContext, extra: dict) -> Iterator[None]:
    """One turn at this machine's game for everything a job records.

    When another recording has the game the job is not kept waiting on a worker (where it would give up after
    a while, and where the order of arrival is lost): it goes back to the queue and keeps its place. A job may
    still ask to wait a little first with ``ingame_lock_wait_s``."""
    hold = getattr(get_renderer("ingame"), "recorder", None)
    if hold is None:  # a renderer with nothing to share
        yield
        return
    try:
        wait_s = max(0.0, float(extra.get("ingame_lock_wait_s") or 0))
    except (TypeError, ValueError):
        wait_s = 0.0
    with ExitStack() as stack:
        try:
            stack.enter_context(hold(wait_s=wait_s, cancelled=ctx.cancelled,
                                     on_wait=lambda waited: ctx.progress(0.01, WAITING_FOR_GAME)))
        except RecorderBusy as exc:
            raise jobs.JobDeferred(WAITING_FOR_GAME) from exc
        yield


def _discard(*paths: Path | None) -> None:
    """Best-effort delete of partial outputs. A file can stay locked for a moment after an encoder is killed
    (Windows), and failing to clean up must never turn a cancellation into a failure."""
    for path in paths:
        if path is None:
            continue
        for _ in range(10):
            try:
                path.unlink(missing_ok=True)
                break
            except OSError:
                time.sleep(0.3)


@jobs.handler("analyze_match")
def analyze_match(ctx: jobs.JobContext) -> dict[str, Any]:
    match_id = ctx.payload["match_id"]
    with session_scope() as s:
        m = service.get_match(s, match_id)
        m.status, m.error = "analyzing", None
        demo_path = service.abs_path(m.demo_path)
    ctx.progress(0.05, "Parsing demo")
    try:
        analysis = analyze_demo(demo_path)
    except Exception as exc:
        with session_scope() as s:
            m = service.get_match(s, match_id)
            m.status, m.error = "failed", f"{type(exc).__name__}: {exc}"
        emit("match.failed", {"match_id": match_id, "error": str(exc)})
        raise
    ctx.progress(0.8, "Saving analysis")

    with session_scope() as s:
        result = service.store_analysis(s, match_id, analysis)
    score = result.pop("score")
    emit("match.ready", {"match_id": match_id, "map": result["map"], "highlights": result["highlights"],
                         "score": score})
    return result


@jobs.handler("import_remote")
def import_remote(ctx: jobs.JobContext) -> dict[str, Any]:
    """Download one match's demo from a source site and hand it to the normal ingest + analysis path."""
    from . import sources as src

    settings = get_settings()
    source = src.get_source(ctx.payload["source_id"])
    if source is None:
        raise RuntimeError(f"Source {ctx.payload['source_id']!r} is not configured on this server.")
    remote_id = str(ctx.payload["remote_id"])
    remote = next((r for r in src.list_remote(source) if r.remote_id == remote_id), None)
    tmp = settings.data_dir / "tmp" / f"{ctx.job_id}.download"
    try:
        ctx.progress(0.02, f"Downloading demo from {source.name}")
        src.download_demo(source, remote_id, tmp, ctx.progress)
        ctx.check_cancelled()
        ctx.progress(0.92, "Registering match")
        with session_scope() as s:
            m, job, created = service.ingest_demo(
                s, tmp, f"{source.id}-{remote_id}.dem", auto_render_top=ctx.payload.get("auto_render_top"))
            m.source_id, m.remote_id = source.id, remote_id
            m.brand = m.brand or source.brand
            if remote is not None:
                m.played_at = remote.played_at.replace(tzinfo=None) if remote.played_at else m.played_at
                m.page_url = remote.page_url or m.page_url
            return {"match_id": m.id, "created": created, "analyze_job_id": job.id if job else None}
    finally:
        _discard(tmp)


def _record(ctx: jobs.JobContext, *, match_id: str, demo_path: Path, analysis, clips: list[Clip], fps: int,
            extra: dict, work_dir: Path, span: tuple[float, float] = (0.02, 0.62)) -> tuple[Any, dict]:
    """Record the given clips with the game (one session) and keep the footage as a master.

    Footage is recorded at the configured capture size (1080p by default), with handles around every segment,
    whatever the shape of the video being made — 16:9, 9:16 and 1:1 are all cut from the same master."""
    from .compose import master as masters

    settings = get_settings()
    record, covered = masters.capture_clips(clips, analysis.rounds, float(analysis.tickrate), analysis.total_ticks)
    if not record:
        raise RenderError("Nothing to record: the clips are empty or outside the part of the demo that can be played.")
    lo, hi = span
    capture = RenderJob(
        demo_path=demo_path, analysis=analysis, clips=record, out_path=work_dir / "master.mp4",
        work_dir=work_dir / "capture",
        options=RenderOptions(width=settings.ingame_capture_width, height=settings.ingame_capture_height,
                              fps=max(fps, settings.ingame_capture_fps), crf=17, title_cards=False, extra=extra),
        progress=lambda f, msg: ctx.progress(lo + (hi - lo) * f, msg), cancelled=ctx.cancelled)
    capture.work_dir.mkdir(parents=True, exist_ok=True)
    with _recorder(ctx, extra):
        res = get_renderer("ingame").render(capture)
    m = masters.save_master(match_id, capture.out_path, width=res.width, height=res.height, fps=res.fps,
                            spans=res.details["spans"], covered=covered, extra=extra)
    return m, {"captured": True, "capture_fps": res.fps, "capture_size": f"{res.width}x{res.height}",
               "takes": len(record), "frames": res.details.get("frames"),
               "game_seconds": res.details.get("game_seconds"), "frame_format": res.details.get("frame_format"),
               "warnings": res.details.get("warnings") or []}


def _ingame_body(ctx: jobs.JobContext, job: RenderJob, *, match_id: str, brand, hud: bool, ramp: bool = True,
                 chat: bool | None = None) -> tuple[float, dict]:
    """First-person body: reuse a stored capture when one covers the clips, otherwise record with the game and
    keep the capture; then retime it, lay it out for the requested shape and draw the HUD.
    Writes ``job.out_path`` and the thumbnail. The details carry ``kill_marks``: when each kill happens in the
    body (``[{"tick", "t"}]``, seconds before the intro bumper is added)."""
    from .compose import master as masters
    from .compose.overlay import compose_body
    from .render import ffmpeg

    settings = get_settings()
    opts, analysis = job.options, job.analysis
    width, height = opts.width - opts.width % 2, opts.height - opts.height % 2
    clips = masters.recordable(job.clips, analysis.total_ticks)
    if not clips:
        raise RenderError("This clip lies outside the part of the demo that can be played back.")
    segments = [s for c in clips for s in c.segments]
    need_fps = masters.capture_fps_for(segments, opts.fps)
    can_record = settings.ingame_enabled and get_renderer("ingame").availability().ok
    recapture = bool(opts.extra.get("ingame_recapture")) and can_record
    kw = {"width": settings.ingame_capture_width, "height": settings.ingame_capture_height, "extra": opts.extra,
          "kills": masters.xray.match_kills(analysis), "tickrate": float(analysis.tickrate or 64)}
    m = None if recapture else masters.find_master(match_id, segments, min_fps=need_fps,
                                                   allow_lower_fps=not can_record, **kw)
    if m is None:
        if not can_record:
            raise RenderError("This clip needs footage that has not been recorded yet, and this server cannot run "
                              "CS2 (in-game recording is disabled or unavailable). Use the 2D renderer, or run a "
                              "worker with the game: see docs/ingame-recording.md.")
        m, details = _record(ctx, match_id=match_id, demo_path=job.demo_path, analysis=analysis, clips=clips,
                             fps=need_fps, extra=opts.extra, work_dir=job.work_dir)
    else:
        details = {"captured": False, "capture_fps": m.fps, "capture_size": f"{m.width}x{m.height}", "master": m.id}
        notes = []
        if m.fps < need_fps:
            notes.append(f"Slow motion uses footage recorded at {m.fps} fps (wanted {need_fps}): it will look less "
                         "smooth until a server with the game re-records it.")
        if m.height < min(settings.ingame_capture_height, max(width, height)):
            notes.append(f"Footage was recorded at {m.width}x{m.height}; the picture is upscaled.")
        if notes:
            details["warnings"] = notes
    body = compose_body(
        m.path, job.out_path, spans=m.as_spans(), clips=clips, analysis=analysis, brand=brand, src_fps=m.fps,
        fps=opts.fps, width=width, height=height, crf=opts.crf, hud=hud, master_has_audio=m.has_audio,
        ramp=ramp, chat=chat, progress=lambda f, msg: ctx.progress(0.64 + 0.24 * f, msg), cancelled=ctx.cancelled)
    duration = body.duration
    details["kill_marks"] = body.marks
    if job.thumbnail_path is not None:
        # The poster is the moment of the last kill (the crosshair on the enemy), not an arbitrary frame.
        at = duration * 0.45
        kills = body.timeline.kills[0] if clips else []
        moment = body.timeline.time_of_tick(kills[-1], clip=0) if kills else None
        if moment is not None:
            at = min(max(0.05, moment - 0.10), max(0.05, duration - 0.2))
        ffmpeg.thumbnail(job.out_path, job.thumbnail_path, at_s=at, width=min(width, 720))
    return duration, details


@jobs.handler("capture_match")
def capture_match(ctx: jobs.JobContext) -> dict[str, Any]:
    """One game session for a whole match: record every requested play, then queue the videos made from it.
    Start-up is the largest fixed cost of in-game recording, so this is how a match should be produced."""
    from .compose import master as masters

    settings = get_settings()
    match_id = ctx.payload["match_id"]
    with session_scope() as s:
        m = service.get_match(s, match_id)
        analysis = service.load_analysis(m)
        demo_path = service.abs_path(m.demo_path)
        rows = s.scalars(select(Highlight).where(Highlight.id.in_(ctx.payload["highlight_ids"]))).all()
        clips = [Clip(segments=[ClipSegment(x["start_tick"], x["end_tick"], x.get("pov_steamid"),
                                            float(x.get("speed") or 1.0), None, x.get("role") or "play")
                                for x in h.segments], highlight_id=h.id)
                 for h in rows]
    clips = masters.recordable(clips, analysis.total_ticks)
    work_dir = settings.data_dir / "tmp" / ctx.job_id
    work_dir.mkdir(parents=True, exist_ok=True)
    # Plays that need more frames per second (slow motion) are recorded apart, so they do not multiply the
    # frames - and the time - of everything else.
    groups = masters.capture_groups(clips, settings.render_fps, settings.ingame_capture_fps)
    details: dict[str, Any] = {}
    extra = dict(ctx.payload.get("extra") or {})
    try:
        # The whole match is one turn at the game: no other recording gets in between its sessions.
        with _recorder(ctx, extra):
            ctx.progress(0.01, f"Recording {len(clips)} plays with the game")
            done = 0
            for fps, group in groups.items():
                lo = 0.02 + 0.95 * done / max(1, len(clips))
                done += len(group)
                hi = 0.02 + 0.95 * done / max(1, len(clips))
                sub = work_dir / f"fps{fps}"
                sub.mkdir(parents=True, exist_ok=True)
                _master, d = _record(ctx, match_id=match_id, demo_path=demo_path, analysis=analysis, clips=group,
                                     fps=fps, extra=dict(extra), work_dir=sub, span=(lo, hi))
                if not details:
                    details = {**d, "sessions": 1}
                else:
                    for key in ("takes", "frames", "game_seconds"):
                        details[key] = (details.get(key) or 0) + (d.get(key) or 0)
                    details["warnings"] = [*(details.get("warnings") or []), *(d.get("warnings") or [])]
                    details["sessions"] += 1
                    details["capture_fps"] = max(details["capture_fps"], d["capture_fps"])
    except RenderCancelled as exc:  # cancelled while the game was recording: a cancellation, not a failure
        raise jobs.JobCancelled() from exc
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
    queued = 0
    then = ctx.payload.get("then") or {}
    if then.get("formats"):
        with session_scope() as s:
            queued = service.queue_after_capture(s, match_id, ctx.payload["highlight_ids"], then["formats"])
    return {"match_id": match_id, "plays": len(clips), "renders_queued": queued, **details}


@jobs.handler("render")
def render(ctx: jobs.JobContext) -> dict[str, Any]:
    settings = get_settings()
    render_id = ctx.payload["render_id"]
    with session_scope() as s:
        r = service.get_render(s, render_id)
        m = service.get_match(s, r.match_id)
        analysis = service.load_analysis(m)
        demo_path = service.abs_path(m.demo_path)
        rows = {h.id: h for h in s.scalars(select(Highlight).where(Highlight.id.in_(r.highlight_ids))).all()}
        clips = []
        for hid in r.highlight_ids:
            h = rows.get(hid)
            if h is None:
                continue  # deleted after the render was requested
            clips.append(Clip(
                segments=[ClipSegment(start_tick=x["start_tick"], end_tick=x["end_tick"],
                                      pov_steamid=x.get("pov_steamid"), speed=float(x.get("speed") or 1.0),
                                      caption=x.get("caption"), role=x.get("role") or "play") for x in h.segments],
                title=h.title, subtitle=h.description, highlight_id=h.id, action_ticks=list(h.action_ticks or [])))
        if not clips:
            raise RuntimeError("All highlights of this render were deleted.")
        r.status, r.error = "running", None
        r.highlight_versions = {hid: rows[hid].version for hid in r.highlight_ids if hid in rows}
        renderer_name, kind = r.renderer, r.kind
        o = RenderOptionsIn.model_validate(r.options or {})
        brand = brand_for(m.brand)
        match_id = m.id

    out_width, out_height = service.output_size(o)
    options = RenderOptions(
        width=out_width, height=out_height, fps=o.fps or settings.render_fps, crf=o.crf or 20,
        title_cards=o.title_cards if o.title_cards is not None else True,
        extra={"brand_name": brand.name, "brand": brand.slug, **o.extra})
    video_rel, thumb_rel = f"renders/{render_id}.mp4", f"renders/{render_id}.jpg"
    final_path = settings.path(*video_rel.split("/"))
    work_dir = settings.data_dir / "tmp" / render_id
    work_dir.mkdir(parents=True, exist_ok=True)
    # Two stages: the renderer captures the body (what happened in the game), composition turns it into the
    # finished video (brand intro / outro, music, loudness). Re-theming never has to touch the renderer.
    job = RenderJob(demo_path=demo_path, analysis=analysis, clips=clips, out_path=work_dir / "body.mp4",
                    work_dir=work_dir, options=options, progress=lambda f, msg: ctx.progress(f * 0.88, msg),
                    cancelled=ctx.cancelled, thumbnail_path=settings.path(*thumb_rel.split("/")))
    warnings: list[str] = []
    out_w, out_h, out_fps = options.width - options.width % 2, options.height - options.height % 2, options.fps
    try:
        ctx.progress(0.01, f"Rendering with {renderer_name}")
        if renderer_name == "ingame":
            body_duration, extra_details = _ingame_body(ctx, job, match_id=match_id, brand=brand,
                                                        hud=o.hud is not False, ramp=o.ramp is not False, chat=o.chat)
            warnings = list(extra_details.get("warnings") or [])
        else:
            result = get_renderer(renderer_name).render(job)
            out_w, out_h, out_fps, body_duration = result.width, result.height, result.fps, result.duration_s
            warnings, extra_details = list(result.details.get("warnings") or []), {}
        if ctx.cancelled():
            raise jobs.JobCancelled()
        if not job.out_path.exists() or job.out_path.stat().st_size == 0:
            raise RuntimeError("Renderer finished without producing a video.")
        # Vertical videos open cold: the brand is on screen the whole time there (header and footer), and
        # two seconds of bumper before the play is where short-video viewers leave.
        intro = o.intro if o.intro is not None else (out_h <= out_w or brand.bumper.intro_vertical)
        duration, composed = finish(
            job.out_path, final_path, brand, width=out_w, height=out_h, fps=out_fps,
            crf=options.crf, body_duration=body_duration, intro=intro, outro=o.outro is not False, music=o.music is not False,
            progress=ctx.progress)
        if ctx.cancelled():
            raise jobs.JobCancelled()
    except (jobs.JobCancelled, RenderCancelled) as exc:
        _discard(final_path, job.thumbnail_path)
        raise jobs.JobCancelled() from exc
    except jobs.JobDeferred:  # the game is taken: nothing was made and nothing failed
        raise
    except Exception as exc:
        _discard(final_path)
        emit("render.failed", {"render_id": render_id, "error": str(exc)})
        raise
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    marks = extra_details.get("kill_marks")
    if marks is not None:  # body time -> video time
        offset = float(composed.get("body_offset_s") or 0.0)
        marks = [{"tick": int(m["tick"]), "t": float(m["t"]) + offset} for m in marks]
    with session_scope() as s:
        done = service.finish_render(s, render_id, duration_s=duration, width=out_w, height=out_h, fps=out_fps,
                                     body_offset_s=composed.get("body_offset_s"), kill_marks=marks)
        if done is None:  # the render was deleted while it was being produced: a cancellation, not a failure
            _discard(final_path, job.thumbnail_path)
            raise jobs.JobCancelled()
        out = done.model_dump(mode="json")
    emit("render.ready", {"render_id": render_id, "match_id": out["match_id"], "kind": kind,
                          "video_url": out["video_url"], "thumbnail_url": out["thumbnail_url"],
                          "duration_s": out["duration_s"]})
    return {"render_id": render_id, "video_url": out["video_url"], "thumbnail_url": out["thumbnail_url"],
            "duration_s": out["duration_s"], "renderer": renderer_name,
            "composition": {**composed, **extra_details}, "warnings": warnings}
