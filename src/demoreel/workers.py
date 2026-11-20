"""Remote workers: other machines that take jobs from this server over HTTPS (see ``docs/workers.md``).

The server owns the queue, the database and the finished videos. A remote worker - typically a Windows PC with
CS2 - asks for work, does the heavy half (parse a demo, record, edit) on its own disk and sends back only the
result. Every connection is opened by the worker, so that machine needs no open port and no fixed address.

What crosses the wire is deliberately narrow:

* a job is **data** (ticks, ids, options) - never a command to run;
* a worker may only touch the jobs it holds, read the inputs of those jobs, and send back the few files that
  job type produces; where a file is stored is decided here, never by the worker;
* a worker's token gives none of the organiser's rights, and the organiser's key is not a worker token.

Raw footage (*masters*) stays on the worker that recorded it. The server only keeps its description, which is
enough to know which plays can be edited again without the game and to address that job to the right worker.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import shutil
import time
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from . import jobs, service
from .analysis.schema import MatchAnalysis
from .brand import brand_for
from .config import get_settings
from .db import Highlight, Job, Render, Worker, WorkerMaster, now, session_scope
from .schemas import JobOut, WorkerFinish, WorkerOut
from .webhooks import emit

log = logging.getLogger("demoreel.workers")

TOKEN_PREFIX = "drw_"
CAPABILITIES = ("cpu", "ingame")
# Job types a remote worker can run. The others write straight to this server's disk (downloading a demo).
JOB_TYPES = ("analyze_match", "render", "capture_match")
HEARTBEAT_S = 30
ONLINE_FOR = timedelta(minutes=2)
MAX_MASTERS = 5000
MB = 1024 * 1024

_last_sweep = 0.0


class Unauthorized(service.ServiceError):
    status_code = 401


class TooLarge(service.ServiceError):
    status_code = 413


# ------------------------------------------------------------------------------------------- registration

def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _capabilities(given: list[str]) -> list[str]:
    caps = list(dict.fromkeys(str(c).strip().lower() for c in given if str(c).strip()))
    unknown = [c for c in caps if c not in CAPABILITIES]
    if unknown or not caps:
        raise service.ServiceError(f"Unknown capability {', '.join(map(repr, unknown)) or '(none given)'}. "
                                   f"Valid: {', '.join(CAPABILITIES)}.")
    return caps


def create_worker(s: Session, name: str, capabilities: list[str]) -> tuple[Worker, str]:
    """Register a worker. Returns it with its token - the only time the token exists in clear."""
    name = " ".join(name.split())
    if not name or len(name) > 64:
        raise service.ServiceError("A worker needs a name of 1 to 64 characters, e.g. 'recording-pc'.")
    if s.scalar(select(Worker.id).where(func.lower(Worker.name) == name.lower())) is not None:
        raise service.Conflict(f"A worker named {name!r} already exists. Pick another name, or remove that one "
                               "first (its token then stops working).")
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    w = Worker(name=name, token_hash=hash_token(token), capabilities=_capabilities(capabilities))
    s.add(w)
    s.flush()
    return w, token


def get_worker(s: Session, name_or_id: str) -> Worker:
    w = s.get(Worker, name_or_id) or s.scalar(select(Worker).where(func.lower(Worker.name) == name_or_id.lower()))
    if w is None:
        names = [x.name for x in list_workers(s)]
        raise service.NotFound(f"No worker {name_or_id!r}. Registered: {', '.join(names) or 'none'}.")
    return w


def list_workers(s: Session) -> list[Worker]:
    return list(s.scalars(select(Worker).order_by(Worker.created_at)).all())


def worker_out(s: Session, w: Worker) -> WorkerOut:
    seen = w.last_seen_at.replace(tzinfo=None) if w.last_seen_at else None
    running = s.scalar(select(Job.id).where(Job.worker_id == _job_owner(w.id), Job.status == "running").limit(1))
    masters = s.scalar(select(func.count()).select_from(WorkerMaster).where(WorkerMaster.worker_id == w.id)) or 0
    return WorkerOut(id=w.id, name=w.name, capabilities=list(w.capabilities or []), created_at=w.created_at,
                     last_seen_at=w.last_seen_at, info=w.info, running_job=running, masters=int(masters),
                     online=seen is not None and now().replace(tzinfo=None) - seen < ONLINE_FOR)


def remove_worker(s: Session, name_or_id: str) -> str:
    """Forget a worker: its token stops working at once. Work it held goes back to the queue; work only it
    could do (editing footage it keeps) fails with the reason. Returns its name."""
    w = get_worker(s, name_or_id)
    name, owner = w.name, _job_owner(w.id)
    for j in s.scalars(select(Job).where(Job.status.in_(("queued", "running")),
                                         (Job.target_worker == w.id) | (Job.worker_id == owner))).all():
        if j.target_worker == w.id:
            j.status, j.finished_at = "failed", now()
            j.error = (f"The worker that kept this footage ({name}) was removed. Ask for the video again: the "
                       "play will be recorded once more by a worker that has the game.")
            jobs._mark_target_failed(s, j, j.error)
        else:
            jobs.give_back(s, j, f"Re-queued: worker {name} was removed", counts=True)
    s.execute(delete(WorkerMaster).where(WorkerMaster.worker_id == w.id))
    s.delete(w)
    return name


def authenticate(s: Session, token: str | None) -> Worker:
    """The worker this token belongs to. The message never says whether the token was close."""
    w = None
    if token and token.startswith(TOKEN_PREFIX):
        digest = hash_token(token)
        w = s.scalar(select(Worker).where(Worker.token_hash == digest))
        if w is not None and not hmac.compare_digest(w.token_hash, digest):
            w = None
    if w is None:
        raise Unauthorized("Missing or unknown worker token. Send `Authorization: Bearer <token>`; the organiser "
                           "creates one with `demoreel workers add <name>`.")
    seen = w.last_seen_at.replace(tzinfo=None) if w.last_seen_at else None
    if seen is None or now().replace(tzinfo=None) - seen > timedelta(seconds=15):
        w.last_seen_at = now()  # throttled: this runs on every request of a worker
    return w


def recording_workers(s: Session) -> list[str]:
    """Names of the remote workers allowed to record with the game."""
    return [w.name for w in list_workers(s) if "ingame" in (w.capabilities or [])]


# ------------------------------------------------------------------------------------------- footage

def footage_holder(s: Session, match_id: str, segments: list, *, width: int, height: int, min_fps: int,
                   extra: dict, allow_lower_fps: bool = False, kills: list | None = None,
                   tickrate: float = 64) -> str | None:
    """Id of a remote worker whose stored footage can serve these segments (see ``compose.master``)."""
    from .compose import master as masters

    by_worker: dict[str, list] = {}
    for row in s.scalars(select(WorkerMaster).where(WorkerMaster.match_id == match_id)
                         .order_by(WorkerMaster.worker_id, WorkerMaster.created_at)).all():
        m = masters.master_from_dict(row.meta or {})
        if m is not None:
            by_worker.setdefault(row.worker_id, []).append(m)
    for worker_id, candidates in by_worker.items():
        if masters.pick_master(candidates, segments, width=width, height=height, min_fps=min_fps, extra=extra,
                               allow_lower_fps=allow_lower_fps, kills=kills, tickrate=tickrate) is not None:
            return worker_id
    return None


def sync_masters(s: Session, worker: Worker, items: list[dict[str, Any]], *, replace: bool = False) -> int:
    """Record which footage a worker keeps. ``replace``: the list is everything it has (what is missing from
    it was deleted there); otherwise the items are added to what is known. Returns how many it now keeps."""
    from .compose import master as masters

    if len(items) > MAX_MASTERS:
        raise service.ServiceError(f"Too many recordings in one report ({len(items)}); the limit is {MAX_MASTERS}.")
    parsed = []
    for i, item in enumerate(items):
        m = masters.master_from_dict(item) if isinstance(item, dict) else None
        if m is None or not m.id or not m.match_id:
            raise service.ServiceError(
                f"Recording #{i + 1} is not a master description. Send the content of the .json file stored next "
                "to the footage (id, match_id, width, height, fps, spans, covered, options, xray).")
        parsed.append(m)
    if replace:
        s.execute(delete(WorkerMaster).where(WorkerMaster.worker_id == worker.id))
    else:
        ids = [m.id for m in parsed]
        if ids:
            s.execute(delete(WorkerMaster).where(WorkerMaster.worker_id == worker.id,
                                                 WorkerMaster.master_id.in_(ids)))
    for m in parsed:
        s.add(WorkerMaster(worker_id=worker.id, match_id=m.match_id, master_id=m.id, meta=asdict(m)))
    s.flush()
    return int(s.scalar(select(func.count()).select_from(WorkerMaster)
                        .where(WorkerMaster.worker_id == worker.id)) or 0)


# ------------------------------------------------------------------------------------------- taking a job

def _job_owner(worker_id: str) -> str:
    return f"remote:{worker_id}"


def _clips(s: Session, highlight_ids: list[str]) -> list[dict[str, Any]]:
    rows = {h.id: h for h in s.scalars(select(Highlight).where(Highlight.id.in_(highlight_ids))).all()}
    out = []
    for hid in highlight_ids:
        h = rows.get(hid)
        if h is None:
            continue  # deleted after the job was queued
        out.append({"highlight_id": h.id, "title": h.title, "subtitle": h.description, "version": h.version,
                    "action_ticks": list(h.action_ticks or []),
                    "segments": [{"start_tick": x["start_tick"], "end_tick": x["end_tick"],
                                  "pov_steamid": x.get("pov_steamid"), "speed": float(x.get("speed") or 1.0),
                                  "caption": x.get("caption"), "role": x.get("role") or "play"}
                                 for x in (h.segments or [])]})
    return out


def job_spec(s: Session, job: Job) -> dict[str, Any]:
    """Everything a worker needs to do this job, as data. Also marks the thing being made as started."""
    settings = get_settings()
    if not job.match_id:
        raise service.ServiceError(f"Job {job.id} names no match.")
    m = service.get_match(s, job.match_id)
    demo = service.abs_path(m.demo_path) if m.demo_path else None
    analysis = service.abs_path(m.analysis_path) if m.analysis_path else None
    spec: dict[str, Any] = {
        "job": {"id": job.id, "type": job.type, "attempt": job.attempts, "payload": dict(job.payload or {})},
        "heartbeat_seconds": HEARTBEAT_S,
        "stale_after_seconds": int(jobs.STALE_AFTER.total_seconds()),
        "match": {"id": m.id, "filename": m.filename, "sha256": m.sha256, "size_bytes": m.size_bytes,
                  "map": m.map, "tickrate": m.tickrate, "total_ticks": m.total_ticks,
                  "source_id": m.source_id, "remote_id": m.remote_id},
        # What can be downloaded from this server for the job. A demo that is not here has to be found on the
        # worker's own disk, by its sha256 or file name.
        "inputs": {"demo.dem": bool(demo and demo.is_file()), "analysis.json": bool(analysis and analysis.is_file())},
        "brand": brand_for(m.brand).slug,
        "defaults": {"render_width": settings.render_width, "render_height": settings.render_height,
                     "render_fps": settings.render_fps, "ingame_capture_width": settings.ingame_capture_width,
                     "ingame_capture_height": settings.ingame_capture_height,
                     "ingame_capture_fps": settings.ingame_capture_fps},
        "uploads": {name: limit for name, limit in _uploads(job.type).items()},
    }
    if job.type == "analyze_match":
        m.status, m.error = "analyzing", None
    elif job.type == "render":
        r = service.get_render(s, job.render_id or str((job.payload or {}).get("render_id")))
        clips = _clips(s, list(r.highlight_ids or []))
        if not clips:
            raise service.ServiceError("All highlights of this render were deleted.")
        r.status, r.error = "running", None
        r.highlight_versions = {c["highlight_id"]: c["version"] for c in clips}
        spec["render"] = {"id": r.id, "kind": r.kind, "renderer": r.renderer, "title": r.title,
                          "options": dict(r.options or {})}
        spec["clips"] = clips
    elif job.type == "capture_match":
        spec["clips"] = _clips(s, list((job.payload or {}).get("highlight_ids") or []))
        if not spec["clips"]:
            raise service.ServiceError("All plays of this recording were deleted.")
    return spec


def claim(worker_id: str, capabilities: list[str], info: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Hand the next job this worker can run, with its spec; ``None`` when there is nothing to do.

    ``capabilities`` is what the worker can take *right now* - it leaves ``ingame`` out while people are
    playing on that machine - and is cut down to what the organiser allowed it."""
    global _last_sweep
    with session_scope() as s:
        w = s.get(Worker, worker_id)
        if w is None:
            raise Unauthorized("This worker was removed.")
        allowed = [c for c in _capabilities(capabilities) if c in (w.capabilities or [])]
        if not allowed:
            raise service.ServiceError(f"Worker {w.name!r} asked for {', '.join(capabilities)}, but it may only "
                                       f"take: {', '.join(w.capabilities or [])}.")
        if info is not None:
            w.info = {str(k)[:40]: (v if isinstance(v, int | float | bool) else str(v)[:200])
                      for k, v in list(info.items())[:20]}
    if time.monotonic() - _last_sweep > 30:  # a server without workers of its own has nobody else to do this
        _last_sweep = time.monotonic()
        jobs.requeue_stale()
    for _ in range(5):
        job = jobs.claim(_job_owner(worker_id), allowed, target=worker_id, types=list(JOB_TYPES))
        if job is None:
            return None
        try:
            with session_scope() as s:
                return job_spec(s, s.get(Job, job.id))
        except service.ServiceError as exc:  # the job can no longer be done (its match or plays are gone)
            jobs.close(job.id, "failed", None, exc.message)
    return None


def _owned(s: Session, worker_id: str, job_id: str) -> Job:
    job = s.get(Job, job_id)
    if job is None:
        raise service.NotFound(f"Job {job_id!r} not found.")
    if job.worker_id != _job_owner(worker_id) or job.status != "running":
        minutes = int(jobs.STALE_AFTER.total_seconds() // 60)
        raise service.Conflict(
            f"Job {job_id} is not yours any more (it is {job.status}). A job goes back to the queue after "
            f"{minutes} minutes without a heartbeat, and can be cancelled by the organiser. Stop working on it "
            "and discard its files.")
    return job


def heartbeat(s: Session, worker_id: str, job_id: str, progress: float = 0.0,
              message: str | None = None) -> dict[str, Any]:
    """Say the job is alive and how far it is. The answer tells the worker when to stop."""
    job = _owned(s, worker_id, job_id)
    job.progress, job.heartbeat_at = max(0.0, min(1.0, float(progress))), now()
    if message:
        job.message = message[:255]
    return {"cancel_requested": bool(job.cancel_requested)}


# ------------------------------------------------------------------------------------------- files

def _uploads(job_type: str) -> dict[str, int]:
    """Files a job of this type may send back, with the size limit of each (bytes)."""
    big = get_settings().worker_max_file_mb * MB
    return {"analyze_match": {"analysis.json": 64 * MB},
            "render": {"video.mp4": big, "thumbnail.jpg": 8 * MB}}.get(job_type, {})


def _upload_dir(job_id: str) -> Path:
    return get_settings().data_dir / "tmp" / "worker-uploads" / job_id


class Upload:
    """One file being received from a worker: written next to its final name, checked, then put in place."""

    def __init__(self, path: Path, limit: int, sha256: str):
        self.path, self.limit, self.expected = path, limit, sha256.strip().lower()
        self.size, self._hash = 0, hashlib.sha256()
        self._part = path.with_name(path.name + ".part")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self._part.open("wb")

    def write(self, chunk: bytes) -> None:
        self.size += len(chunk)
        if self.size > self.limit:
            self.abort()
            raise TooLarge(f"{self.path.name} is larger than the {self.limit // MB} MB this server accepts for it "
                           "(DEMOREEL_WORKER_MAX_FILE_MB sets the limit for videos).")
        self._hash.update(chunk)
        self._file.write(chunk)

    def abort(self) -> None:
        self._file.close()
        self._part.unlink(missing_ok=True)

    def close(self) -> dict[str, Any]:
        self._file.close()
        digest = self._hash.hexdigest()
        if self.size == 0 or not hmac.compare_digest(digest, self.expected):
            self._part.unlink(missing_ok=True)
            raise service.ServiceError(
                f"{self.path.name} arrived {'empty' if self.size == 0 else 'damaged'}: its SHA-256 is {digest}, the "
                "X-Content-SHA256 header said " + (self.expected or "nothing") + ". Send the file again.")
        self._part.replace(self.path)
        return {"name": self.path.name, "size_bytes": self.size, "sha256": digest}


def open_upload(s: Session, worker_id: str, job_id: str, name: str, sha256: str | None) -> Upload:
    """Start receiving one result file of a job this worker holds."""
    job = _owned(s, worker_id, job_id)
    allowed = _uploads(job.type)
    if name not in allowed:
        raise service.NotFound(f"A {job.type} job does not take a file named {name!r}. "
                               f"It takes: {', '.join(allowed) or 'no files'}.")
    if not sha256 or len(sha256.strip()) != 64:
        raise service.ServiceError("Send the file's SHA-256 (64 hex characters) in the X-Content-SHA256 header: "
                                   "it is how the server knows the file arrived whole.")
    job.heartbeat_at = now()
    return Upload(_upload_dir(job_id) / name, allowed[name], sha256)


def input_file(s: Session, worker_id: str, job_id: str, name: str) -> Path:
    """A file the worker may read for a job it holds: the demo or the analysis of that job's match."""
    job = _owned(s, worker_id, job_id)
    m = service.get_match(s, job.match_id or "")
    if name == "demo.dem":
        path = service.abs_path(m.demo_path) if m.demo_path else None
        if path is None or not path.is_file():
            raise service.NotFound(
                f"This server does not keep the demo of match {m.id}. Look for it on the worker's own disk: "
                f"{m.filename}, {m.size_bytes} bytes, SHA-256 {m.sha256}.")
        return path
    if name == "analysis.json":
        path = service.abs_path(m.analysis_path) if m.analysis_path else None
        if path is None or not path.is_file():
            raise service.NotFound(f"Match {m.id} has no analysis yet.")
        return path
    raise service.NotFound(f"No input named {name!r}. A job's inputs are: demo.dem, analysis.json.")


def _brand_dir(s: Session, worker_id: str, job_id: str) -> tuple[str, Path]:
    job = _owned(s, worker_id, job_id)
    brand = brand_for(service.get_match(s, job.match_id or "").brand)
    return brand.slug, brand.dir


def brand_manifest(s: Session, worker_id: str, job_id: str) -> dict[str, Any]:
    """The brand kit of the job's match: its files with size and SHA-256, so the worker fetches what changed."""
    slug, root = _brand_dir(s, worker_id, job_id)
    files = []
    if root.is_dir():
        for p in sorted(root.rglob("*")):
            if p.is_file():
                files.append({"path": p.relative_to(root).as_posix(), "size_bytes": p.stat().st_size,
                              "sha256": hashlib.sha256(p.read_bytes()).hexdigest()})
    return {"slug": slug, "files": files}


def brand_file(s: Session, worker_id: str, job_id: str, rel: str) -> Path:
    _slug, root = _brand_dir(s, worker_id, job_id)
    path = (root / rel).resolve()
    if not root.is_dir() or not path.is_relative_to(root.resolve()) or not path.is_file():
        raise service.NotFound(f"The brand kit of this job has no file {rel!r}.")
    return path


# ------------------------------------------------------------------------------------------- finishing

class _RenderResult(BaseModel):
    duration_s: float = Field(gt=0, le=3600)
    width: int = Field(ge=16, le=7680)
    height: int = Field(ge=16, le=4320)
    fps: int = Field(ge=1, le=240)
    body_offset_s: float | None = Field(None, ge=0)
    kill_marks: list[dict[str, float]] | None = None
    warnings: list[str] = Field(default_factory=list)
    composition: dict[str, Any] = Field(default_factory=dict)
    masters: list[dict[str, Any]] = Field(default_factory=list)


def _result_error(job_type: str, exc: ValidationError) -> service.ServiceError:
    first = exc.errors()[0]
    where = ".".join(str(x) for x in first.get("loc", ())) or "result"
    return service.ServiceError(f"The result of this {job_type} job is not valid: {where}: {first.get('msg')}.")


def _apply_analysis(s: Session, job: Job) -> tuple[dict[str, Any], list[tuple[str, dict]]]:
    src = _upload_dir(job.id) / "analysis.json"
    if not src.is_file():
        raise service.ServiceError("Upload analysis.json before finishing an analyze_match job "
                                   f"(PUT /api/v1/worker/jobs/{job.id}/files/analysis.json).")
    try:
        analysis = MatchAnalysis.model_validate_json(src.read_bytes())
    except ValidationError as exc:
        first = exc.errors()[0]
        raise service.ServiceError("analysis.json does not follow the analysis schema: "
                                   f"{'.'.join(str(x) for x in first.get('loc', ()))}: {first.get('msg')}.") from exc
    result = service.store_analysis(s, job.match_id or "", analysis)
    score = result.pop("score")
    return result, [("match.ready", {"match_id": job.match_id, "map": result["map"],
                                     "highlights": result["highlights"], "score": score})]


def _apply_render(s: Session, job: Job, worker: Worker, given: dict[str, Any]) -> tuple[dict[str, Any], list]:
    settings = get_settings()
    try:
        res = _RenderResult.model_validate(given)
    except ValidationError as exc:
        raise _result_error("render", exc) from exc
    render_id = job.render_id or ""
    video, thumb = _upload_dir(job.id) / "video.mp4", _upload_dir(job.id) / "thumbnail.jpg"
    if not video.is_file():
        raise service.ServiceError("Upload video.mp4 before finishing a render job "
                                   f"(PUT /api/v1/worker/jobs/{job.id}/files/video.mp4).")
    with video.open("rb") as f:
        head = f.read(12)
    if head[4:8] != b"ftyp":
        raise service.ServiceError("video.mp4 is not an MP4 file (no 'ftyp' box at its start).")
    if thumb.is_file() and not thumb.read_bytes()[:3] == b"\xff\xd8\xff":
        raise service.ServiceError("thumbnail.jpg is not a JPEG image.")
    if s.get(Render, render_id) is None:
        raise _Gone()
    if res.masters:  # checked before any file is moved: a refused result must leave the uploads as they are
        sync_masters(s, worker, res.masters)
    final = settings.path("renders", f"{render_id}.mp4")
    video.replace(final)
    poster = final.with_suffix(".jpg")
    if thumb.is_file():
        thumb.replace(poster)
    else:
        poster.unlink(missing_ok=True)  # a poster left by an earlier attempt would not belong to this video
    marks = None
    if res.kill_marks is not None:
        marks = [{"tick": int(k.get("tick", 0)), "t": float(k.get("t", 0.0))} for k in res.kill_marks]
    out = service.finish_render(s, render_id, duration_s=res.duration_s, width=res.width, height=res.height,
                                fps=res.fps, body_offset_s=res.body_offset_s, kill_marks=marks)
    assert out is not None  # checked above, in this same transaction
    data = out.model_dump(mode="json")
    result = {"render_id": render_id, "video_url": data["video_url"], "thumbnail_url": data["thumbnail_url"],
              "duration_s": data["duration_s"], "renderer": data["renderer"], "composition": res.composition,
              "warnings": res.warnings, "worker": worker.name}
    return result, [("render.ready", {"render_id": render_id, "match_id": data["match_id"], "kind": data["kind"],
                                      "video_url": data["video_url"], "thumbnail_url": data["thumbnail_url"],
                                      "duration_s": data["duration_s"]})]


def _apply_capture(s: Session, job: Job, worker: Worker, given: dict[str, Any]) -> tuple[dict[str, Any], list]:
    masters = given.get("masters")
    if not isinstance(masters, list) or not masters:
        raise service.ServiceError("The result of a capture_match job needs `masters`: the description of each "
                                   "recording made (the .json stored next to the footage).")
    sync_masters(s, worker, masters)  # before queueing: the videos are then addressed to this worker
    payload = dict(job.payload or {})
    formats = list((payload.get("then") or {}).get("formats") or [])
    queued = service.queue_after_capture(s, job.match_id or "", list(payload.get("highlight_ids") or []), formats)
    details = {k: v for k, v in given.items() if k != "masters" and isinstance(v, str | int | float | bool | list)}
    return {**details, "match_id": job.match_id, "plays": len(payload.get("highlight_ids") or []),
            "renders_queued": queued, "masters": len(masters), "worker": worker.name}, []


class _Gone(Exception):
    """What the job was making was deleted meanwhile: that is a cancellation, not a failure."""


def finish(worker_id: str, job_id: str, body: WorkerFinish) -> JobOut:
    """Close a job this worker holds: apply what it made, or record why it did not make it.

    A result that cannot be accepted (a file missing, a field out of range) is answered with the reason and the
    job stays with the worker, so it can send what was missing and finish again."""
    events: list[tuple[str, dict]] = []
    status, result, error = body.status, None, body.error
    with session_scope() as s:
        job = _owned(s, worker_id, job_id)
        worker = s.get(Worker, worker_id)
        if worker is None:
            raise Unauthorized("This worker was removed.")
        if status == "failed" and body.retry and job.attempts < jobs.MAX_ATTEMPTS:
            status = "released"
        if status == "released":
            # "released": the work never started, so it is not one of the job's attempts
            jobs.give_back(s, job, f"Given back by {worker.name}" + (f": {error[:160]}" if error else ""),
                           counts=body.status != "released")
            shutil.rmtree(_upload_dir(job_id), ignore_errors=True)
            return service.job_out(job)
        if status == "succeeded":
            try:
                if job.type == "analyze_match":
                    result, events = _apply_analysis(s, job)
                elif job.type == "render":
                    result, events = _apply_render(s, job, worker, body.result)
                elif job.type == "capture_match":
                    result, events = _apply_capture(s, job, worker, body.result)
                else:
                    raise service.ServiceError(f"A remote worker cannot finish a {job.type} job.")
            except _Gone:
                status, error = "canceled", "Deleted while it was being made"
        elif status == "failed":
            error = error or "The worker reported a failure without saying why."
            if job.type == "render" and job.render_id:
                events = [("render.failed", {"render_id": job.render_id, "error": error})]
            elif job.type == "analyze_match":
                events = [("match.failed", {"match_id": job.match_id, "error": error})]
        else:
            error = "Canceled"
    jobs.close(job_id, status, result, error if status != "succeeded" else None)
    shutil.rmtree(_upload_dir(job_id), ignore_errors=True)
    for name, data in events:
        try:
            emit(name, data)
        except Exception:  # noqa: BLE001 - notifications must never undo a finished job
            log.exception("webhook emit failed")
    with session_scope() as s:
        return service.job_out(service.get_job(s, job_id))
