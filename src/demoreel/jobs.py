"""Tiny database-backed job queue.

Why not Celery/arq/RQ: they need Redis or RabbitMQ and are awkward on Windows. A table plus an atomic
"claim" UPDATE is enough here, works with SQLite and Postgres, and keeps the project a single install.

Workers are separate processes (`demoreel worker`, or started by `demoreel serve`). Each advertises
*capabilities* (``cpu`` for analysis + 2D renders, ``ingame`` for recording with the CS2 client) and only
claims jobs it can run — so a Linux box can analyze while a Windows gaming PC records.
"""

from __future__ import annotations

import logging
import os
import socket
import time
import traceback
from collections.abc import Callable
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from .db import Job, now, session_scope

log = logging.getLogger("demoreel.jobs")

STALE_AFTER = timedelta(minutes=3)
MAX_ATTEMPTS = 2
TERMINAL = {"succeeded", "failed", "canceled"}


class JobCancelled(Exception):
    pass


class JobDeferred(Exception):
    """Raised by a handler that cannot run its job on this machine right now (the game is recording something
    else). The job goes back to the queue and keeps its place; it is neither a failure nor an attempt."""


@dataclass
class JobContext:
    job_id: str
    payload: dict[str, Any]
    _last_write: float = 0.0

    def progress(self, fraction: float, message: str = "") -> None:
        """Report progress (throttled to ~1 write/s). Also acts as the worker heartbeat."""
        t = time.monotonic()
        if t - self._last_write < 1.0 and fraction < 1.0:
            return
        self._last_write = t
        with session_scope() as s:
            s.execute(update(Job).where(Job.id == self.job_id).values(
                progress=max(0.0, min(1.0, float(fraction))), message=(message or None) and message[:255],
                heartbeat_at=now()))

    def cancelled(self) -> bool:
        with session_scope() as s:
            return bool(s.scalar(select(Job.cancel_requested).where(Job.id == self.job_id)))

    def check_cancelled(self) -> None:
        if self.cancelled():
            raise JobCancelled()


Handler = Callable[[JobContext], dict[str, Any] | None]
HANDLERS: dict[str, Handler] = {}


def handler(job_type: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        HANDLERS[job_type] = fn
        return fn
    return deco


class Taken(Exception):
    """What jobs of a capability need for themselves on this machine is in use right now."""


Reserve = Callable[[], AbstractContextManager[Any]]
RESERVE: dict[str, Reserve] = {}


def reserve(capability: str) -> Callable[[Reserve], Reserve]:
    """Register how this machine takes what jobs of ``capability`` need for themselves - the game, for
    recording. The function returns a context manager and raises :class:`Taken` when someone else has it.

    A worker takes it *before* it looks at the queue and keeps it while the job runs. So only the worker that
    has the game takes a recording job: the others leave those jobs in the queue, where they stay in the order
    they arrived, and do other work meanwhile."""
    def deco(fn: Reserve) -> Reserve:
        RESERVE[capability] = fn
        return fn
    return deco


def _reserved(capabilities: list[str], held: ExitStack) -> list[str]:
    """The capabilities this worker may take a job of right now; what they need is held in ``held``."""
    out = []
    for capability in capabilities:
        take = RESERVE.get(capability)
        if take is not None:
            try:
                held.enter_context(take())
            except Taken:
                continue
        out.append(capability)
    return out


def enqueue(s: Session, job_type: str, payload: dict[str, Any], *, capability: str = "cpu", priority: int = 100,
            match_id: str | None = None, render_id: str | None = None, target_worker: str | None = None) -> Job:
    """``target_worker``: the id of the one remote worker that can run this job (it keeps the footage)."""
    job = Job(type=job_type, payload=payload, capability=capability, priority=priority, match_id=match_id,
              render_id=render_id, target_worker=target_worker)
    s.add(job)
    s.flush()
    return job


def request_cancel(s: Session, job: Job) -> Job:
    if job.status == "queued":
        job.status, job.finished_at, job.message = "canceled", now(), "Canceled before start"
        if job.render_id:  # nobody will ever pick it up: do not leave its video "queued" forever
            from .db import Render

            render = s.get(Render, job.render_id)
            if render is not None and render.status in ("queued", "running"):
                render.status, render.finished_at = "canceled", now()
    elif job.status == "running":
        job.cancel_requested = 1
    return job


def requeue_stale() -> int:
    """Jobs whose worker died (no heartbeat) go back to the queue, or fail after too many attempts."""
    n = 0
    with session_scope() as s:
        cutoff = now() - STALE_AFTER
        for job in s.scalars(select(Job).where(Job.status == "running")).all():
            beat = job.heartbeat_at or job.started_at
            if beat is not None and beat.replace(tzinfo=cutoff.tzinfo) > cutoff:
                continue
            if job.attempts >= MAX_ATTEMPTS:
                job.status, job.error, job.finished_at = "failed", "Worker died while running this job.", now()
                _mark_target_failed(s, job, job.error)
            else:
                job.status, job.worker_id, job.message = "queued", None, "Re-queued after worker loss"
            n += 1
    return n


def give_back(s: Session, job: Job, message: str, *, counts: bool = False) -> None:
    """Put a job a worker holds back in the queue. It keeps its age, so it is the next of its priority again.
    ``counts``: the work did start, so this was one of the job's attempts."""
    from .db import Match, Render

    job.status, job.worker_id, job.progress, job.message = "queued", None, 0.0, message[:255] or None
    if not counts:
        job.attempts = max(0, job.attempts - 1)
    # Undo what taking the job marked on the thing it makes.
    if job.type == "analyze_match" and job.match_id:
        m = s.get(Match, job.match_id)
        if m is not None and m.status == "analyzing":
            m.status = "queued"
    if job.render_id:
        r = s.get(Render, job.render_id)
        if r is not None and r.status == "running":
            r.status = "queued"


def _defer(job_id: str, message: str) -> bool:
    """Give a deferred job back. ``False`` when it was cancelled meanwhile (it must end as canceled instead)."""
    with session_scope() as s:
        row = s.get(Job, job_id)
        if row is None or row.status != "running" or row.cancel_requested:
            return False
        give_back(s, row, message or "Waiting for its turn")
    log.info("job %s back in the queue: %s", job_id, message)
    return True


def _mark_target_failed(s: Session, job: Job, error: str) -> None:
    from .db import Match, Render

    if job.type == "analyze_match" and job.match_id:
        m = s.get(Match, job.match_id)
        if m and m.status != "ready":
            m.status, m.error = "failed", error
    if job.render_id:
        r = s.get(Render, job.render_id)
        if r and r.status != "ready":
            r.status, r.error, r.finished_at = "failed", error, now()


def claim(worker_id: str, capabilities: list[str], *, target: str | None = None,
          types: list[str] | None = None) -> Job | None:
    """Atomically take the next queued job this worker can run.

    Jobs addressed to one remote worker are only handed to it (``target`` = its id); the workers of this
    machine never take them, because the footage those jobs need is not on this disk. ``types`` narrows the
    kinds of job taken (a remote worker cannot run the ones that write to this server's disk)."""
    mine = Job.target_worker.is_(None) if target is None else or_(Job.target_worker.is_(None),
                                                                   Job.target_worker == target)
    if types is not None:
        mine = and_(mine, Job.type.in_(types))
    with session_scope() as s:
        for _ in range(5):
            job_id = s.scalar(select(Job.id).where(Job.status == "queued", Job.capability.in_(capabilities), mine)
                              .order_by(Job.priority, Job.created_at).limit(1))
            if job_id is None:
                return None
            res = s.execute(update(Job).where(Job.id == job_id, Job.status == "queued").values(
                status="running", worker_id=worker_id, started_at=now(), heartbeat_at=now(),
                attempts=Job.attempts + 1, progress=0.0, message="Starting"))
            if res.rowcount == 1:
                s.commit()
                return s.get(Job, job_id)
            s.rollback()  # another worker won the race; try the next one
    return None


def run_one(worker_id: str, capabilities: list[str]) -> bool:
    """Run the next job this worker can take. ``False`` when there was nothing to do right now."""
    with ExitStack() as held:
        usable = _reserved(capabilities, held)
        job = claim(worker_id, usable) if usable else None
        if job is None:
            return False
        if job.capability not in RESERVE:
            held.close()  # this job needs nothing for itself: let go before the work, not after it
        return _run(job)


def _run(job: Job) -> bool:
    log.info("job %s (%s) started", job.id, job.type)
    ctx = JobContext(job_id=job.id, payload=dict(job.payload or {}))
    status, result, error = "succeeded", None, None
    try:
        fn = HANDLERS.get(job.type)
        if fn is None:
            raise RuntimeError(f"No handler for job type {job.type!r}")
        result = fn(ctx)
    except JobCancelled:
        status, error = "canceled", "Canceled"
    except JobDeferred as exc:
        if _defer(job.id, str(exc)):
            return False  # nothing ran: the caller waits a moment before looking at the queue again
        status, error = "canceled", "Canceled"
    except Exception as exc:  # noqa: BLE001 - job boundary: record and keep the worker alive
        status, error = "failed", f"{type(exc).__name__}: {exc}"
        log.error("job %s failed: %s\n%s", job.id, error, traceback.format_exc())
    close(job.id, status, result, error)
    return True


def close(job_id: str, status: str, result: dict[str, Any] | None, error: str | None) -> None:
    """Record how a job ended (``succeeded``, ``failed`` or ``canceled``), mark what it was making, and tell
    the webhooks. Used for jobs run on this machine and for jobs a remote worker reports back."""
    with session_scope() as s:
        row = s.get(Job, job_id)
        if row is None:
            return
        row.status, row.result, row.error, row.finished_at = status, result, error, now()
        if status == "succeeded":
            row.progress, row.message = 1.0, "Done"
        else:
            _mark_target_failed(s, row, error or status)
            if status == "canceled" and row.render_id:
                from .db import Render

                r = s.get(Render, row.render_id)
                if r:
                    r.status = "canceled"
        event = {"job_id": row.id, "type": row.type, "match_id": row.match_id, "render_id": row.render_id,
                 "result": result, "error": error}
    log.info("job %s %s", job_id, status)
    try:
        from .webhooks import emit

        emit(f"job.{status}", event)
    except Exception:  # noqa: BLE001 - notifications must never break the worker
        log.exception("webhook emit failed")


def worker_loop(capabilities: list[str], poll_s: float = 1.0, stop: Callable[[], bool] = lambda: False) -> None:
    from . import tasks  # noqa: F401 - registers handlers
    from .db import init_db

    init_db()
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    log.info("worker %s up, capabilities=%s", worker_id, capabilities)
    last_sweep = 0.0
    while not stop():
        if time.monotonic() - last_sweep > 60:
            requeue_stale()
            last_sweep = time.monotonic()
        if not run_one(worker_id, capabilities):
            time.sleep(poll_s)


def worker_process_main(capabilities: list[str]) -> None:
    """Entry point for worker subprocesses."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        worker_loop(capabilities)
    except KeyboardInterrupt:
        pass
