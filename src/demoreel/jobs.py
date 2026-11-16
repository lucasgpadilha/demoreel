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
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .db import Job, now, session_scope

log = logging.getLogger("demoreel.jobs")

STALE_AFTER = timedelta(minutes=3)
MAX_ATTEMPTS = 2
TERMINAL = {"succeeded", "failed", "canceled"}


class JobCancelled(Exception):
    pass


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


def enqueue(s: Session, job_type: str, payload: dict[str, Any], *, capability: str = "cpu", priority: int = 100,
            match_id: str | None = None, render_id: str | None = None) -> Job:
    job = Job(type=job_type, payload=payload, capability=capability, priority=priority, match_id=match_id,
              render_id=render_id)
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


def claim(worker_id: str, capabilities: list[str]) -> Job | None:
    """Atomically take the next queued job this worker can run."""
    with session_scope() as s:
        for _ in range(5):
            job_id = s.scalar(select(Job.id).where(Job.status == "queued", Job.capability.in_(capabilities))
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
    job = claim(worker_id, capabilities)
    if job is None:
        return False
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
    except Exception as exc:  # noqa: BLE001 - job boundary: record and keep the worker alive
        status, error = "failed", f"{type(exc).__name__}: {exc}"
        log.error("job %s failed: %s\n%s", job.id, error, traceback.format_exc())
    with session_scope() as s:
        row = s.get(Job, job.id)
        if row is not None:
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
    log.info("job %s %s", job.id, status)
    try:
        from .webhooks import emit

        emit(f"job.{status}", {"job_id": job.id, "type": job.type, "match_id": job.match_id,
                               "render_id": job.render_id, "result": result, "error": error})
    except Exception:  # noqa: BLE001 - notifications must never break the worker
        log.exception("webhook emit failed")
    return True


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
