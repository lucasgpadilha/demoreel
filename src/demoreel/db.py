"""Database models. SQLite by default (zero setup, works on Windows and Linux); any SQLAlchemy URL works.

Large blobs never go in the database: demos, analysis JSON and videos live on disk under ``data_dir`` and the
rows only keep relative paths.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Index, Integer, String, Text, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .config import get_settings


def now() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


class Base(DeclarativeBase):
    pass


class Match(Base):
    __tablename__ = "matches"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("m"))
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    filename: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)  # queued|analyzing|ready|failed
    error: Mapped[str | None] = mapped_column(Text, default=None)
    demo_path: Mapped[str] = mapped_column(String(255))
    analysis_path: Mapped[str | None] = mapped_column(String(255), default=None)
    map: Mapped[str | None] = mapped_column(String(64), default=None)
    server_name: Mapped[str | None] = mapped_column(String(255), default=None)
    team_a_name: Mapped[str | None] = mapped_column(String(128), default=None)
    team_b_name: Mapped[str | None] = mapped_column(String(128), default=None)
    team_a_score: Mapped[int | None] = mapped_column(Integer, default=None)
    team_b_score: Mapped[int | None] = mapped_column(Integer, default=None)
    rounds: Mapped[int | None] = mapped_column(Integer, default=None)
    duration_s: Mapped[float | None] = mapped_column(Float, default=None)
    tickrate: Mapped[int | None] = mapped_column(Integer, default=None)
    total_ticks: Mapped[int | None] = mapped_column(Integer, default=None)
    options: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    played_at: Mapped[datetime | None] = mapped_column(default=None)  # when the match happened (from its source)
    source_id: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    remote_id: Mapped[str | None] = mapped_column(String(128), default=None)
    page_url: Mapped[str | None] = mapped_column(String(512), default=None)
    brand: Mapped[str | None] = mapped_column(String(64), default=None)  # brand kit used for its videos
    created_at: Mapped[datetime] = mapped_column(default=now)
    analyzed_at: Mapped[datetime | None] = mapped_column(default=None)


class Highlight(Base):
    __tablename__ = "highlights"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("h"))
    match_id: Mapped[str] = mapped_column(ForeignKey("matches.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    round: Mapped[int | None] = mapped_column(Integer, default=None)
    player_steamid: Mapped[str | None] = mapped_column(String(64), default=None)
    player_name: Mapped[str | None] = mapped_column(String(128), default=None)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    segments: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    kill_ticks: Mapped[list[int]] = mapped_column(JSON, default=list)
    origin: Mapped[str] = mapped_column(String(16), default="auto")  # auto|manual|agent
    version: Mapped[int] = mapped_column(Integer, default=1)  # bumped on every edit
    # Curation (see analysis/highlights.py). All nullable: rows from before the scoring model have none.
    team: Mapped[str | None] = mapped_column(String(16), default=None)  # team_a|team_b
    tier: Mapped[str | None] = mapped_column(String(2), default=None)  # S|A|B|C
    breakdown: Mapped[dict[str, float] | None] = mapped_column(JSON, default=None)
    featured: Mapped[bool | None] = mapped_column(Boolean, default=None)  # gets first-person videos
    featured_rank: Mapped[int | None] = mapped_column(Integer, default=None)
    featured_by: Mapped[str | None] = mapped_column(String(16), default=None)  # auto|manual (manual survives re-scoring)
    # True once a person or an agent changed the cut: the model then leaves the segments alone.
    edited: Mapped[bool | None] = mapped_column(Boolean, default=None)
    category: Mapped[str | None] = mapped_column(String(16), default=None)  # play (also NULL) | fail
    action_ticks: Mapped[list[int] | None] = mapped_column(JSON, default=None)
    facts: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)  # fails: what happened, as data
    created_at: Mapped[datetime] = mapped_column(default=now)
    updated_at: Mapped[datetime] = mapped_column(default=now, onupdate=now)


class Render(Base):
    __tablename__ = "renders"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("r"))
    match_id: Mapped[str] = mapped_column(ForeignKey("matches.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16), default="highlight")  # highlight|reel
    title: Mapped[str | None] = mapped_column(String(255), default=None)
    highlight_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    highlight_versions: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    renderer: Mapped[str] = mapped_column(String(32))
    options: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)  # queued|running|ready|failed|canceled
    error: Mapped[str | None] = mapped_column(Text, default=None)
    job_id: Mapped[str | None] = mapped_column(String(32), default=None)
    video_path: Mapped[str | None] = mapped_column(String(255), default=None)
    thumbnail_path: Mapped[str | None] = mapped_column(String(255), default=None)
    duration_s: Mapped[float | None] = mapped_column(Float, default=None)
    size_bytes: Mapped[int | None] = mapped_column(Integer, default=None)
    width: Mapped[int | None] = mapped_column(Integer, default=None)
    height: Mapped[int | None] = mapped_column(Integer, default=None)
    fps: Mapped[int | None] = mapped_column(Integer, default=None)
    body_offset_s: Mapped[float | None] = mapped_column(Float, default=None)  # where the play starts (after the intro)
    kill_marks: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, default=None)  # [{"tick", "t"}], video time
    created_at: Mapped[datetime] = mapped_column(default=now)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_claim", "status", "capability", "priority", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("j"))
    type: Mapped[str] = mapped_column(String(32))  # analyze_match|render
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued|running|succeeded|failed|canceled
    capability: Mapped[str] = mapped_column(String(16), default="cpu")  # which kind of worker may run it
    priority: Mapped[int] = mapped_column(Integer, default=100)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    message: Mapped[str | None] = mapped_column(String(255), default=None)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested: Mapped[int] = mapped_column(Integer, default=0)
    match_id: Mapped[str | None] = mapped_column(String(32), default=None, index=True)
    render_id: Mapped[str | None] = mapped_column(String(32), default=None)
    worker_id: Mapped[str | None] = mapped_column(String(64), default=None)
    heartbeat_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=now)
    started_at: Mapped[datetime | None] = mapped_column(default=None)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)


class Webhook(Base):
    __tablename__ = "webhooks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("wh"))
    url: Mapped[str] = mapped_column(String(1024))
    events: Mapped[list[str]] = mapped_column(JSON, default=list)  # empty = all events
    secret: Mapped[str | None] = mapped_column(String(128), default=None)
    active: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(default=now)


@lru_cache(maxsize=1)
def get_engine():
    url = get_settings().db_url
    is_sqlite = url.startswith("sqlite")
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30} if is_sqlite else {},
                           pool_pre_ping=not is_sqlite)
    if is_sqlite:
        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn, _record):  # WAL lets the API and worker processes share the file safely
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()
    return engine


@lru_cache(maxsize=1)
def _factory() -> sessionmaker[Session]:
    return sessionmaker(get_engine(), expire_on_commit=False)


def _add_missing_columns() -> None:
    """Poor man's migration until Alembic lands: new *nullable* columns are added to existing tables in place,
    so upgrading never needs the database to be recreated. Anything else (renames, type changes) is out of scope."""
    from sqlalchemy import inspect, text

    engine = get_engine()
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have or not col.nullable or col.primary_key:
                    continue
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col.type.compile(engine.dialect)}'
                conn.execute(text(ddl))


def init_db() -> None:
    Base.metadata.create_all(get_engine())
    _add_missing_columns()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commits on success, rolls back on error."""
    s = _factory()()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
