"""Publish this instance to a read-only mirror over SSH.

Videos are recorded and composed where the game is (a desktop with CS2); the public site can live on a small
server that only serves them. ``publish`` copies what that server needs - a snapshot of the database, the
analyses, the finished videos with their posters and the brand kits - and nothing else: never demos, masters,
caches, queued work or webhook secrets.

It needs only the ``ssh`` client on this machine and ``find``, ``tar``, ``mv`` and ``rm`` on the other. Files
are compared by size and sent as a tar stream, so a run that was interrupted is simply run again. Order
matters: files first, the database last (the mirror never lists a video it does not have yet), stale videos
are removed after that.
"""

from __future__ import annotations

import io
import shlex
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .config import get_settings

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_BRAND_FILES = {".json", ".png", ".jpg", ".jpeg", ".webp", ".svg"}
_BATCH_BYTES = 256 * 1024 * 1024  # one tar stream; an interrupted run loses at most this much
_DB_NAME = "demoreel.db"
_RENDERERS = ("ingame", "radar")  # what DEMOREEL_PUBLISH_RENDERERS may name


class PublishError(RuntimeError):
    """Something the operator can act on: the message says what was wrong and what is valid."""


@dataclass
class Target:
    """``user@host:/absolute/data/dir`` (an SSH config alias works as the host)."""

    host: str
    path: str

    @classmethod
    def parse(cls, text: str | None) -> Target:
        value = (text or "").strip()
        host, sep, path = value.partition(":")
        if host.startswith("-"):  # ssh would read it as an option (-oProxyCommand=... runs a command here)
            raise PublishError(f"Publish target {value!r} starts with '-', which ssh would take as an option. "
                               "Use '[user@]host:/absolute/path/to/data', for example "
                               "'demoreel@example.org:/srv/demoreel/data'.")
        if not sep or not host or not path.startswith("/") or any(c.isspace() for c in value):
            raise PublishError(
                f"Publish target {value!r} is not valid. Use '[user@]host:/absolute/path/to/data', for example "
                "'demoreel@example.org:/srv/demoreel/data', as --target or DEMOREEL_PUBLISH_TARGET.")
        return cls(host=host, path=path.rstrip("/") or "/")


@dataclass
class Plan:
    """What a publish run found and did."""

    files: int = 0  # files the mirror should have
    bytes: int = 0
    sent: int = 0
    sent_bytes: int = 0
    removed: int = 0
    matches: int = 0
    renders: int = 0
    missing: list[str] = field(default_factory=list)  # listed by the database, not on this disk
    dry_run: bool = False
    restarted: bool = False


def _ssh(target: Target, command: str, *, stdin: bytes | None = None, timeout: float | None = 300) -> str:
    exe = shutil.which("ssh")
    if not exe:
        raise PublishError("The 'ssh' client was not found on this machine. Install OpenSSH (on Windows: "
                           "Settings > Optional features > OpenSSH Client) and try again.")
    try:
        r = subprocess.run([exe, "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "--", target.host, command],
                           input=stdin, capture_output=True, timeout=timeout, creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired as e:
        raise PublishError(f"The server {target.host} did not answer within {timeout:.0f} s.") from e
    if r.returncode != 0:
        detail = r.stderr.decode("utf-8", "replace").strip()[-400:]
        raise PublishError(f"Command failed on {target.host} (exit {r.returncode}): {detail or 'no message'}. "
                           "Check that key-based SSH login works ('ssh " + target.host + "') and that the "
                           "target folder exists and is writable.")
    return r.stdout.decode("utf-8", "replace")


def snapshot_database(out: Path) -> Path:
    """A consistent copy of the SQLite database holding only what a mirror shows: matches, highlights and
    finished videos. Jobs, webhooks and remote workers (they carry secrets) and unfinished renders are left out,
    and so are the videos of renderers not in ``DEMOREEL_PUBLISH_RENDERERS`` when it is set."""
    settings = get_settings()
    keep = settings.publish_renderers
    if keep is not None:
        unknown = [r for r in keep if r not in _RENDERERS]
        if unknown or not keep:
            raise PublishError(f"DEMOREEL_PUBLISH_RENDERERS names {', '.join(map(repr, unknown)) or 'nothing'}; "
                               f"valid renderers: {', '.join(_RENDERERS)}.")
    url = settings.database_url or ""
    if url and not url.startswith("sqlite"):
        raise PublishError("Publishing copies a SQLite database; this instance uses another database "
                           f"({url.split(':', 1)[0]}). Point the mirror at that database instead.")
    src_path = Path(url.split("///", 1)[1]) if url else settings.data_dir / _DB_NAME
    if not src_path.is_file():
        raise PublishError(f"No database at {src_path}. Nothing has been analyzed on this instance yet.")
    out.unlink(missing_ok=True)
    src = sqlite3.connect(f"file:{src_path.as_posix()}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(out)
        try:
            src.backup(dst)
            dst.execute("DELETE FROM jobs")
            dst.execute("DELETE FROM webhooks")
            for table in ("worker_masters", "workers"):  # token hashes stay here; older databases have neither
                if dst.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    dst.execute(f"DELETE FROM {table}")
            dst.execute("DELETE FROM renders WHERE status != 'ready'")
            if keep is not None:
                dst.execute(f"DELETE FROM renders WHERE renderer NOT IN ({','.join('?' * len(keep))})", keep)
            dst.execute("UPDATE renders SET job_id = NULL")
            dst.commit()
            dst.execute("PRAGMA journal_mode=DELETE")
            dst.execute("VACUUM")
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()
    return out


def wanted_files(snapshot: Path, data_dir: Path) -> tuple[dict[str, Path], list[str], int, int]:
    """``relative path -> local file`` for everything the mirror needs besides the database, the paths the
    database lists that are not on this disk, and how many matches and videos the snapshot holds."""
    files: dict[str, Path] = {}
    missing: list[str] = []
    db = sqlite3.connect(f"file:{snapshot.as_posix()}?mode=ro", uri=True)
    try:
        listed = [r[0] for r in db.execute("SELECT analysis_path FROM matches WHERE status = 'ready'")]
        matches = len(listed)
        renders = 0
        for video, thumb in db.execute("SELECT video_path, thumbnail_path FROM renders"):
            renders += 1
            listed += [video, thumb]
    finally:
        db.close()
    root = data_dir.resolve()
    for rel in listed:
        if not rel:
            continue
        p = (root / rel).resolve()
        if not p.is_relative_to(root):
            continue  # never follow a path out of the data directory
        if p.is_file():
            files[PurePosixPath(*p.relative_to(root).parts).as_posix()] = p
        else:
            missing.append(str(rel))
    brands = root / "brands"
    if brands.is_dir():
        for p in sorted(brands.rglob("*")):
            if p.is_file() and p.suffix.lower() in _BRAND_FILES:
                files[PurePosixPath(*p.relative_to(root).parts).as_posix()] = p
    return files, missing, matches, renders


def stale_files(there: Iterable[str], files: Iterable[str], missing: Iterable[str]) -> list[str]:
    """What pruning removes from the mirror: videos and analyses the database no longer lists. A video the
    database lists but this disk no longer has is kept: the mirror's copy may be the only one left."""
    keep = set(files) | {str(m).replace("\\", "/") for m in missing}
    return sorted(rel for rel in there if rel not in keep and rel.startswith(("renders/", "matches/")))


def _remote_listing(target: Target) -> dict[str, int]:
    q = shlex.quote(target.path)
    out = _ssh(target, f"mkdir -p {q}/renders {q}/matches {q}/brands && cd {q} && "
                       "find renders matches brands -type f -printf '%s\\t%p\\n'")
    listing: dict[str, int] = {}
    for line in out.splitlines():
        size, _, name = line.partition("\t")
        if name and size.isdigit():
            listing[name] = int(size)
    return listing


def _batches(items: list[tuple[str, Path]]) -> Iterable[list[tuple[str, Path]]]:
    batch: list[tuple[str, Path]] = []
    size = 0
    for rel, p in items:
        batch.append((rel, p))
        size += p.stat().st_size
        if size >= _BATCH_BYTES:
            yield batch
            batch, size = [], 0
    if batch:
        yield batch


def _send(target: Target, batch: list[tuple[str, Path]], on_file: Callable[[int], None]) -> None:
    exe = shutil.which("ssh")
    assert exe  # _ssh ran before and checked
    proc = subprocess.Popen([exe, "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "--", target.host,
                             f"tar -xf - -C {shlex.quote(target.path)}"],
                            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            creationflags=_NO_WINDOW)
    try:
        with tarfile.open(fileobj=proc.stdin, mode="w|", format=tarfile.PAX_FORMAT) as tar:
            for rel, p in batch:
                info = tar.gettarinfo(str(p), arcname=rel)
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mode = 0o644
                with p.open("rb") as f:
                    tar.addfile(info, f)
                on_file(info.size)
        proc.stdin.close()
    except (OSError, tarfile.TarError) as e:
        proc.kill()
        err = (proc.stderr.read() if proc.stderr else b"").decode("utf-8", "replace").strip()[-300:]
        raise PublishError(f"The upload to {target.host} was interrupted ({e}; {err or 'no message'}). "
                           "Run publish again: it continues from what already arrived.") from e
    err = proc.stderr.read().decode("utf-8", "replace").strip()[-300:] if proc.stderr else ""
    if proc.wait() != 0:
        raise PublishError(f"The server {target.host} could not unpack the upload: {err or 'no message'}. "
                           "Run publish again: it continues from what already arrived.")


def publish(target: str | None = None, *, restart: str | None = None, dry_run: bool = False, prune: bool = True,
            progress: Callable[[str], None] = lambda message: None) -> Plan:
    """Bring the mirror at ``target`` up to date. ``restart``: a command run on the server after the database
    is swapped (the mirror keeps the old one open until then), e.g. ``sudo systemctl restart demoreel``."""
    settings = get_settings()
    where = Target.parse(target or settings.publish_target)
    restart = restart if restart is not None else settings.publish_restart
    plan = Plan(dry_run=dry_run)
    with tempfile.TemporaryDirectory(prefix="demoreel-publish-") as tmp:
        snap = snapshot_database(Path(tmp) / _DB_NAME)
        files, plan.missing, plan.matches, plan.renders = wanted_files(snap, settings.data_dir)
        plan.files, plan.bytes = len(files), sum(p.stat().st_size for p in files.values())
        progress(f"{plan.matches} matches, {plan.renders} videos, {plan.files} files ({plan.bytes / 1e9:.2f} GB)")
        there = _remote_listing(where)
        todo = [(rel, p) for rel, p in sorted(files.items()) if there.get(rel) != p.stat().st_size]
        stale = stale_files(there, files, plan.missing)
        total = sum(p.stat().st_size for _, p in todo)
        progress(f"to send: {len(todo)} files ({total / 1e9:.2f} GB); stale on the server: {len(stale)}")
        if dry_run:
            plan.sent, plan.sent_bytes, plan.removed = len(todo), total, len(stale) if prune else 0
            return plan

        step = max(total // 20, 1)
        mark = step

        def on_file(size: int) -> None:
            nonlocal mark
            plan.sent += 1
            plan.sent_bytes += size
            if plan.sent_bytes >= mark:
                mark = (plan.sent_bytes // step + 1) * step
                progress(f"sent {plan.sent}/{len(todo)} files, {plan.sent_bytes / 1e9:.2f} of {total / 1e9:.2f} GB")

        for batch in _batches(todo):
            _send(where, batch, on_file)

        q = shlex.quote(where.path)
        _send(where, [(f"{_DB_NAME}.new", snap)], lambda size: None)
        _ssh(where, f"cd {q} && rm -f {_DB_NAME}-wal {_DB_NAME}-shm && mv -f {_DB_NAME}.new {_DB_NAME}")
        progress("database swapped")
        if restart:
            _ssh(where, restart)
            plan.restarted = True
            progress("server restarted")
        if prune and stale:
            buf = io.BytesIO("\0".join(stale).encode("utf-8") + b"\0")
            _ssh(where, f"cd {q} && xargs -0 rm -f --", stdin=buf.getvalue())
            plan.removed = len(stale)
            progress(f"removed {plan.removed} stale files")
    return plan
