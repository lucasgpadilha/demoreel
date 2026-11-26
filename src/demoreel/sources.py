"""Match sources: pull demos from a community / matchmaking site instead of uploading them by hand.

A source is a JSON file ``<data_dir>/sources/<id>.json`` describing a site that exposes a list of matches as
JSON and one URL per demo. No code is needed for a new site that fits this shape::

    {
      "name": "Mix da Vila",
      "url": "https://example.com/partidas",
      "brand": "mixdavila",
      "list_url": "https://example.com/api/partidas",
      "demo_url": "https://example.com/api/partidas/{id}/demo",
      "page_url": "https://example.com/partidas/{id}",
      "fields": {"id": "id", "map": "mapa", "played_at": "data", "score_a": "placarA",
                 "score_b": "placarB", "has_demo": "demo"}
    }

Source files are written by the operator of the server, so their URLs are trusted configuration. What the
sites answer is not: redirects are followed without the source's ``headers`` (often a token) once they leave
the configured host, a match list may be at most ``MAX_LIST_BYTES`` and a download at most
``DOWNLOAD_DEADLINE_S`` long.

**Be a polite guest.** Demos are hundreds of megabytes each, and community sites often sit on small hosting
plans: a bulk import through such a site can exhaust its bandwidth quota and take the whole site down (this
happened to the first site this was used with). So downloads are spaced out, and the moment a source answers
"too many requests" / "service unavailable" / "usage exceeded" it is **paused**: remaining imports fail
immediately without touching the site, and the match list is served from the last good copy.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field

from .config import get_settings

_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_USER_AGENT = "demoreel"
_CACHE: dict[str, tuple[float, list[RemoteMatch]]] = {}
CACHE_SECONDS = 60
PAUSE_HOURS = 6.0  # how long a source is left alone after it reports overload
MIN_DOWNLOAD_INTERVAL_S = 5.0  # breathing room between two demo downloads from the same source
MAX_REDIRECTS = 5
MAX_LIST_BYTES = 32 * 1024 * 1024  # a match list of a few thousand rows is well under a megabyte
DOWNLOAD_DEADLINE_S = 3600.0  # a demo that trickles in for longer is given up (the read timeout alone never fires)
_REDIRECTS = {301, 302, 303, 307, 308}
_last_download: dict[str, float] = {}


class SourceUnavailable(RuntimeError):
    """The source refused or cannot serve right now; it has been paused."""


class SourceError(RuntimeError):
    """The source answered something this server will not use (too large, not JSON, endless redirects)."""


def _origin(url: str | httpx.URL) -> tuple[str, str, int | None]:
    u = httpx.URL(str(url))
    return u.scheme, u.host, u.port


@contextmanager
def _get(source: Source, url: str, *, timeout: Any) -> Iterator[httpx.Response]:
    """GET ``url`` as a stream, following redirects by hand: the source's own headers go only to the scheme,
    host and port they were configured for, never to the host a redirect points at (a CDN, or anywhere)."""
    home = _origin(url)
    for _ in range(MAX_REDIRECTS + 1):
        headers = {"User-Agent": _USER_AGENT, **(source.headers if _origin(url) == home else {})}
        with httpx.stream("GET", url, headers=headers, timeout=timeout, follow_redirects=False) as r:
            location = r.headers.get("location") if r.status_code in _REDIRECTS else None
            if not location:
                yield r
                return
            target = r.url.join(location)
            if target.scheme not in ("http", "https"):
                raise SourceError(f"{source.name} redirected {url} to {target.scheme}:, which is not http(s).")
            url = str(target)
    raise SourceError(f"{source.name} redirected more than {MAX_REDIRECTS} times; the last address was {url}. "
                      "Check the source's URLs.")


def _read_json(source: Source, r: httpx.Response) -> Any:
    declared = str(r.headers.get("content-length") or "")
    too_large = SourceError(f"{source.name}'s match list is larger than {MAX_LIST_BYTES // (1024 * 1024)} MB; "
                            "this server does not read lists that large. Check the source's list_url.")
    if declared.isdigit() and int(declared) > MAX_LIST_BYTES:
        raise too_large
    body = bytearray()
    for chunk in r.iter_bytes():
        body += chunk
        if len(body) > MAX_LIST_BYTES:
            raise too_large
    try:
        return json.loads(bytes(body))
    except ValueError as exc:
        raise SourceError(f"{source.name}'s match list is not JSON ({exc}). Check the source's list_url.") from exc


def _pause_file(source_id: str) -> Path:
    return sources_dir() / f"{source_id}.paused"


def paused(source_id: str) -> dict | None:
    """``{"until": iso, "reason": str}`` while the source is paused, else None."""
    f = _pause_file(source_id)
    if not f.is_file():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        if datetime.fromisoformat(data["until"]) > datetime.now(UTC):
            return data
    except (ValueError, KeyError, OSError):
        pass
    f.unlink(missing_ok=True)
    return None


def pause(source_id: str, reason: str, hours: float = PAUSE_HOURS) -> dict:
    from datetime import timedelta

    data = {"until": (datetime.now(UTC) + timedelta(hours=hours)).isoformat(), "reason": reason}
    _pause_file(source_id).write_text(json.dumps(data), encoding="utf-8")
    return data


def resume(source_id: str) -> None:
    _pause_file(source_id).unlink(missing_ok=True)


def _overloaded(status: int, body: str = "") -> bool:
    return status in (429, 503, 509) or "usage_exceeded" in body or "rate limit" in body.lower()


def _check(source: Source, response: httpx.Response) -> None:
    """Pause the source and raise if it signals overload."""
    body = ""
    if response.status_code >= 400:
        try:
            body = response.text[:300]
        except Exception:  # noqa: BLE001 - streamed responses may not have a body loaded
            body = ""
    if _overloaded(response.status_code, body):
        info = pause(source.id, f"HTTP {response.status_code} {body.strip()[:120]}".strip())
        raise SourceUnavailable(
            f"{source.name} answered {response.status_code} ({body.strip()[:80] or 'unavailable'}). To protect the "
            f"site, imports from it are paused until {info['until'][:16]} UTC; nothing more will be requested.")


def _cache_file(source_id: str) -> Path:
    return sources_dir() / f"{source_id}.matches-cache.json"


class SourceFields(BaseModel):
    id: str = "id"
    map: str | None = "map"
    played_at: str | None = "played_at"
    score_a: str | None = "score_a"
    score_b: str | None = "score_b"
    has_demo: str | None = None


class Source(BaseModel):
    id: str
    name: str
    url: str | None = None
    brand: str | None = Field(None, description="Brand kit applied to matches imported from this source.")
    list_url: str
    demo_url: str = Field(description="Template with {id}.")
    page_url: str | None = None
    fields: SourceFields = Field(default_factory=SourceFields)
    headers: dict[str, str] = Field(default_factory=dict)


class RemoteMatch(BaseModel):
    remote_id: str
    map: str | None = None
    played_at: datetime | None = None
    score_a: int | None = None
    score_b: int | None = None
    has_demo: bool = True
    page_url: str | None = None


def sources_dir() -> Path:
    d = get_settings().data_dir / "sources"
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_sources() -> list[Source]:
    out = []
    for f in sorted(sources_dir().glob("*.json")):
        if _ID.match(f.stem):
            try:
                out.append(Source.model_validate({**json.loads(f.read_text(encoding="utf-8")), "id": f.stem}))
            except (ValueError, OSError):
                continue
    return out


def get_source(source_id: str) -> Source | None:
    return next((s for s in list_sources() if s.id == source_id), None)


def save_source(source: Source) -> Path:
    if not _ID.match(source.id):
        raise ValueError("Source id must be lowercase letters, digits, '-' or '_'.")
    f = sources_dir() / f"{source.id}.json"
    f.write_text(json.dumps(source.model_dump(exclude={"id"}), indent=2, ensure_ascii=False), encoding="utf-8")
    _CACHE.pop(source.id, None)
    return f


def _when(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, UTC)
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    except (ValueError, OSError, OverflowError):
        return None


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def list_remote(source: Source, *, fresh: bool = False) -> list[RemoteMatch]:
    """Matches the site currently lists (cached for a minute so the UI can poll without hammering the site)."""
    hit = _CACHE.get(source.id)
    if hit and not fresh and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    disk = _cache_file(source.id)

    def last_good() -> list[RemoteMatch] | None:
        try:
            return [RemoteMatch.model_validate(x) for x in json.loads(disk.read_text(encoding="utf-8"))]
        except (OSError, ValueError):
            return None

    if paused(source.id):  # leave the site alone; show what we last saw
        saved = last_good()
        if saved is not None:
            return saved
        raise SourceUnavailable(f"{source.name} is paused ({paused(source.id)['reason']}) and no saved list exists.")
    try:
        with _get(source, source.list_url, timeout=30) as r:
            if r.status_code >= 400:
                r.read()
            _check(source, r)
            r.raise_for_status()
            rows = _read_json(source, r)
    except (httpx.HTTPError, SourceUnavailable, SourceError):
        saved = last_good()
        if saved is not None:
            return saved
        raise
    if isinstance(rows, dict):  # {"data": [...]} style envelopes
        rows = next((v for v in rows.values() if isinstance(v, list)), [])
    f = source.fields
    out: list[RemoteMatch] = []
    for row in rows:
        if not isinstance(row, dict) or row.get(f.id) is None:
            continue
        rid = str(row[f.id])
        out.append(RemoteMatch(
            remote_id=rid, map=str(row.get(f.map)) if f.map and row.get(f.map) else None,
            played_at=_when(row.get(f.played_at)) if f.played_at else None,
            score_a=_int(row.get(f.score_a)) if f.score_a else None,
            score_b=_int(row.get(f.score_b)) if f.score_b else None,
            has_demo=bool(row.get(f.has_demo)) if f.has_demo else True,
            page_url=source.page_url.format(id=rid) if source.page_url else None))
    out.sort(key=lambda m: (m.played_at or datetime.min.replace(tzinfo=UTC)), reverse=True)
    _CACHE[source.id] = (time.monotonic(), out)
    try:
        disk.write_text(json.dumps([m.model_dump(mode="json") for m in out]), encoding="utf-8")
    except OSError:
        pass
    return out


def download_demo(source: Source, remote_id: str, dest: Path, progress=lambda fraction, message: None) -> int:
    """Stream a demo to ``dest``. Returns the number of bytes written."""
    limit = get_settings().max_upload_mb * 1024 * 1024
    url = source.demo_url.format(id=remote_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    info = paused(source.id)
    if info:
        raise SourceUnavailable(f"{source.name} is paused until {info['until'][:16]} UTC ({info['reason']}); "
                                f"match {remote_id} was not requested.")
    wait = MIN_DOWNLOAD_INTERVAL_S - (time.monotonic() - _last_download.get(source.id, -1e9))
    if wait > 0:
        time.sleep(wait)
    written, started = 0, time.monotonic()
    with _get(source, url, timeout=httpx.Timeout(30, read=120)) as r:
        _last_download[source.id] = time.monotonic()
        if r.status_code == 404:
            raise RuntimeError(f"{source.name} has no demo for match {remote_id}.")
        if r.status_code >= 400:
            r.read()
            _check(source, r)
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0)
        with dest.open("wb") as out:
            for chunk in r.iter_bytes(1024 * 1024):
                written += len(chunk)
                if written > limit:
                    raise RuntimeError(f"Demo is larger than the {get_settings().max_upload_mb} MB limit.")
                if time.monotonic() - started > DOWNLOAD_DEADLINE_S:
                    raise RuntimeError(f"{source.name} took more than {DOWNLOAD_DEADLINE_S / 60:.0f} minutes to "
                                       f"send the demo of match {remote_id} ({written // (1024 * 1024)} MB so far). "
                                       "Try again later.")
                out.write(chunk)
                progress(min(0.9, 0.9 * written / total) if total else 0.3,
                         f"Downloading demo ({written // (1024 * 1024)} MB)")
    if written == 0:
        raise RuntimeError(f"{source.name} returned an empty file for match {remote_id}.")
    return written
