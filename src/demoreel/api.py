"""REST API (FastAPI). Interactive docs at ``/docs``, machine-readable contract at ``/openapi.json``.

Typical flow::

    POST /api/v1/matches            (multipart upload)      -> 202 {match, job}
    GET  /api/v1/jobs/{id}?wait=30  (long-poll)             -> job.done
    GET  /api/v1/matches/{id}/scoreboard | /analysis | /highlights
    POST /api/v1/highlights/{id}/edit    (trim, POV, speed…) -> highlight
    POST /api/v1/highlights/{id}/renders                     -> 202 {render, job}
    GET  /api/v1/renders/{id}                                -> video_url
"""

from __future__ import annotations

import hmac
import re
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import select
from starlette.datastructures import Headers, MutableHeaders

from . import __version__, insights, jobs, service, workers
from .analysis.schema import KillEvent, MatchAnalysis, PlayerStats, RoundSummary
from .config import get_settings, host_name
from .db import Job, Webhook, init_db, session_scope
from .schemas import (
    BrandOut,
    ChatRankingsOut,
    EditRequest,
    FeedItem,
    HighlightCreate,
    HighlightOut,
    HighlightPatch,
    ImportAccepted,
    ImportRequest,
    JobOut,
    MatchOut,
    NightOut,
    PlayerProfileOut,
    PlayerSummary,
    ProduceAccepted,
    ProduceRequest,
    ReelRequest,
    RemoteMatchOut,
    RenderOut,
    RenderRequest,
    RivalriesOut,
    SourceOut,
    TrophiesOut,
    UploadResult,
    WeaponRankingsOut,
    WebhookIn,
    WebhookOut,
    WorkerClaim,
    WorkerCreate,
    WorkerCreated,
    WorkerFinish,
    WorkerMasters,
    WorkerOut,
    WorkerProgress,
)
from .webhooks import EVENTS

WEB_DIR = Path(__file__).parent / "web"


_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _key_in(headers: Headers) -> str:
    auth = headers.get("authorization", "")
    return auth[7:] if auth.lower().startswith("bearer ") else headers.get("x-api-key", "")


def _given_key(request: Request) -> str:
    return _key_in(request.headers)


def _same(given: str, key: str) -> bool:
    return hmac.compare_digest(given.encode(), key.encode())


def is_admin(request: Request) -> bool:
    """True when this request may change things: no admin key is configured, or it carries the right one."""
    key = get_settings().admin_key
    return not key or _same(_given_key(request), key)


def _key_problem(method: str, given: str) -> tuple[int, str] | None:
    """``(status, message)`` when a request with this method and key must be refused, else None."""
    settings = get_settings()
    if settings.api_key and not (_same(given, settings.api_key)
                                 or (settings.admin_key and _same(given, settings.admin_key))):
        return 401, "Missing or invalid API key (send `Authorization: Bearer <key>` or `X-API-Key`)."
    if method not in _SAFE_METHODS and settings.admin_key and not _same(given, settings.admin_key):
        return 403, ("This server is read-only for visitors. Changing anything needs the organiser's "
                     "key (send `Authorization: Bearer <key>` or `X-API-Key`).")
    return None


def require_key(request: Request) -> None:
    problem = _key_problem(request.method, _given_key(request))
    if problem:
        raise HTTPException(*problem)


def _upload_too_large() -> str:
    mb = get_settings().max_upload_mb
    return (f"The file is larger than this server's upload limit of {mb} MB (DEMOREEL_MAX_UPLOAD_MB). Send "
            "the demo compressed (.gz, .bz2, .zip), or import it from a folder or a source instead.")


api = APIRouter(prefix="/api/v1", dependencies=[Depends(require_key)])


class RenderAccepted(BaseModel):
    render: RenderOut
    job: JobOut


class Capabilities(BaseModel):
    version: str
    renderers: list[dict[str, Any]]
    default_renderer: str
    auto_render_top: int
    webhook_events: list[str]
    max_upload_mb: int


# ------------------------------------------------------------------------------------------- meta

@api.get("/capabilities", response_model=Capabilities, tags=["meta"],
         summary="What this server can do (renderers available, limits)")
def capabilities() -> Capabilities:
    s = get_settings()
    return Capabilities(version=__version__, renderers=service.renderers_status(), default_renderer=s.default_renderer,
                        auto_render_top=s.auto_render_top, webhook_events=EVENTS, max_upload_mb=s.max_upload_mb)


@api.get("/session", tags=["meta"], summary="What this caller may do (visitor or organiser)")
def get_session(request: Request) -> dict[str, bool]:
    return {"admin": is_admin(request), "admin_key_required": bool(get_settings().admin_key)}


@api.get("/brand", response_model=BrandOut, tags=["meta"],
         summary="Identity of this instance (name, logo, colours, fonts) — drives the UI theme and the videos")
def get_brand() -> BrandOut:
    return service.brand_out()


_EXCLUDE = ("Match ids to leave out, whatever the other filters say (repeat the parameter: "
            "`exclude=m_1&exclude=m_2`). For a site that hid a match and wants it out of every number.")


@api.get("/feed", response_model=list[FeedItem], tags=["highlights"],
         summary="Best plays that already have a video, across all matches")
def get_feed(limit: int = Query(24, ge=1, le=200), player: str | None = Query(None, description="SteamID."),
             kind: str | None = None,
             featured: bool | None = Query(None, description="true: only the featured plays."),
             since: datetime | None = Query(None, description="Only plays of matches played at or after this "
                                                               "moment (ISO 8601) - e.g. one night or one week."),
             until: datetime | None = Query(None, description="…and before this moment."),
             offset: int = Query(0, ge=0, description="Skip this many plays (paging)."),
             category: str = Query("play", pattern="^(play|fail|all)$", description="play | fail | all."),
             exclude: list[str] = Query(default_factory=list, description=_EXCLUDE),
             response: Response = None) -> list[FeedItem]:  # type: ignore[assignment]
    total: list[int] = []
    with session_scope() as s:
        items = service.feed(s, limit=limit, player_steamid=player, kind=kind, featured=featured,
                             since=since, until=until, offset=offset, total=total, category=category,
                             exclude=exclude)
    if response is not None and total:
        response.headers["X-Total-Count"] = str(total[0])  # plays matching the filters, before limit/offset
        response.headers["Access-Control-Expose-Headers"] = "X-Total-Count"
    return items


# ------------------------------------------------------------------------------------------- sources

def _source(source_id: str):
    from . import sources as src

    source = src.get_source(source_id)
    if source is None:
        raise HTTPException(404, f"Source {source_id!r} is not configured on this server.")
    return source


@api.get("/sources", response_model=list[SourceOut], tags=["sources"],
         summary="Sites this server can import matches from")
def list_sources() -> list[SourceOut]:
    from . import sources as src

    out = []
    for x in src.list_sources():
        info = src.paused(x.id) or {}
        out.append(SourceOut(id=x.id, name=x.name, url=x.url, paused_until=info.get("until"),
                             paused_reason=info.get("reason")))
    return out


@api.get("/sources/{source_id}/matches", response_model=list[RemoteMatchOut], tags=["sources"],
         summary="Matches listed by a source, with their import status here")
def list_source_matches(source_id: str) -> list[RemoteMatchOut]:
    from . import sources as src

    source = _source(source_id)
    try:
        with session_scope() as s:
            return [RemoteMatchOut(**row) for row in service.remote_matches(s, source)]
    except src.SourceUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Could not reach {source.name}: {exc}") from exc


@api.post("/sources/{source_id}/import", response_model=ImportAccepted, status_code=202, tags=["sources"],
          summary="Download and analyze matches from a source (async)")
def import_from_source(source_id: str, body: ImportRequest) -> ImportAccepted:
    from . import sources as src

    source = _source(source_id)
    info = src.paused(source.id)
    if info:
        raise HTTPException(503, f"{source.name} reported overload ({info['reason']}). Imports are paused until "
                                 f"{info['until'][:16]} UTC so the site is left alone.")
    try:
        with session_scope() as s:
            queued, skipped = service.import_remote(s, source, body.remote_ids, body.auto_render_top)
            detail = None
            if len(queued) > 5:
                detail = (f"{len(queued)} demos will be downloaded through {source.name}, a few hundred MB each. "
                          "Small hosting plans meter bandwidth: a large batch can exhaust the site's quota. "
                          "Downloads are spaced out and stop at the first sign of overload.")
            return ImportAccepted(jobs=[service.job_out(j) for j in queued], skipped=skipped, detail=detail)
    except src.SourceUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Could not reach {source.name}: {exc}") from exc


# ------------------------------------------------------------------------------------------- matches

@api.post("/matches", response_model=UploadResult, status_code=202, tags=["matches"],
          summary="Upload a CS2 demo (.dem, optionally .gz/.bz2/.zst/.zip) and queue its analysis")
def upload_match(
    file: Annotated[UploadFile, File(description="The demo file.")],
    auto_render_top: Annotated[int | None, Query(ge=0, le=20, description="Render the N best highlights "
                               "automatically after analysis. Default: server setting.")] = None,
    renderer: Annotated[str | None, Query(description="Renderer for the automatic renders: auto | radar | ingame.")] = None,
) -> UploadResult:
    settings = get_settings()
    if renderer:
        service.resolve_renderer(renderer)  # fail before spooling a few hundred MB to disk
    tmp_dir = settings.data_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    limit, written = settings.max_upload_mb * 1024 * 1024, 0
    with tempfile.NamedTemporaryFile(dir=tmp_dir, suffix=".upload", delete=False) as tmp:
        tmp_path = Path(tmp.name)
        while chunk := file.file.read(4 * 1024 * 1024):
            written += len(chunk)
            if written > limit:
                break
            tmp.write(chunk)
    if written > limit:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(413, _upload_too_large())
    with session_scope() as s:
        match, job, created = service.ingest_demo(s, tmp_path, file.filename or "demo.dem",
                                                  auto_render_top=auto_render_top, renderer=renderer, keep_source=False)
        s.flush()
        return UploadResult(match=service.match_out(s, match), job=service.job_out(job) if job else None, created=created)


@api.get("/matches", response_model=list[MatchOut], tags=["matches"], summary="List matches, newest first")
def list_matches(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), status: str | None = None,
                 map: str | None = None) -> list[MatchOut]:
    with session_scope() as s:
        return [service.match_out(s, m) for m in service.list_matches(s, limit=limit, offset=offset, status=status, map_name=map)]


@api.get("/matches/{match_id}", response_model=MatchOut, tags=["matches"], summary="Match status and summary")
def get_match(match_id: str) -> MatchOut:
    with session_scope() as s:
        return service.match_out(s, service.get_match(s, match_id))


@api.delete("/matches/{match_id}", status_code=204, tags=["matches"], summary="Delete a match, its demo and videos")
def delete_match(match_id: str) -> None:
    with session_scope() as s:
        service.delete_match(s, match_id)


@api.post("/matches/{match_id}/reanalyze", response_model=JobOut, status_code=202, tags=["matches"],
          summary="Run the analysis again (keeps manual/agent highlights, replaces automatic ones)")
def reanalyze(match_id: str) -> JobOut:
    with session_scope() as s:
        m = service.get_match(s, match_id)
        m.status, m.error = "queued", None
        return service.job_out(jobs.enqueue(s, "analyze_match", {"match_id": m.id}, match_id=m.id, priority=50))


def _analysis(match_id: str) -> MatchAnalysis:
    with session_scope() as s:
        return service.load_analysis(service.get_match(s, match_id))


@api.get("/matches/{match_id}/analysis", response_model=MatchAnalysis, tags=["analysis"],
         summary="Full analysis: teams, per-player stats, every round with kills, highlight candidates")
def get_analysis(match_id: str, request: Request) -> MatchAnalysis:
    analysis = _analysis(match_id)
    if is_admin(request):
        return analysis
    # Visitors (an organiser's key is configured and this caller has none) never get free chat text: people
    # paste phone numbers and addresses into it. Banter that is safe to quote stays; the rest keeps its
    # classification but loses its text.
    from .analysis import chat as chat_model

    safe = [m if chat_model.shareable(str(m.get("text") or ""), m.get("kind"))
            else {**m, "text": "", "redacted": True} for m in analysis.chat]
    return analysis.model_copy(update={"chat": [{**m, "text": chat_model.display_text(str(m.get("text") or ""))}
                                                for m in safe]})


@api.get("/matches/{match_id}/scoreboard", tags=["analysis"], summary="Compact scoreboard grouped by team")
def get_scoreboard(match_id: str) -> dict[str, Any]:
    return service.scoreboard(_analysis(match_id))


@api.get("/matches/{match_id}/players", response_model=list[PlayerStats], tags=["analysis"],
         summary="Detailed stats for every player (sorted by rating)")
def get_players(match_id: str) -> list[PlayerStats]:
    return _analysis(match_id).players


@api.get("/matches/{match_id}/players/{steamid}", response_model=PlayerStats, tags=["analysis"],
         summary="Detailed stats for one player")
def get_player(match_id: str, steamid: str) -> PlayerStats:
    for p in _analysis(match_id).players:
        if p.steamid == steamid:
            return p
    raise HTTPException(404, f"Player {steamid!r} is not in this match.")


@api.get("/matches/{match_id}/rounds", response_model=list[RoundSummary], tags=["analysis"],
         summary="All rounds (winner, economy, bomb events, kills, clutches)")
def get_rounds(match_id: str) -> list[RoundSummary]:
    return _analysis(match_id).rounds


@api.get("/matches/{match_id}/rounds/{number}", response_model=RoundSummary, tags=["analysis"], summary="One round")
def get_round(match_id: str, number: int) -> RoundSummary:
    for r in _analysis(match_id).rounds:
        if r.number == number:
            return r
    raise HTTPException(404, f"Round {number} does not exist.")


@api.get("/matches/{match_id}/kills", response_model=list[KillEvent], tags=["analysis"],
         summary="Kill log with filters")
def get_kills(match_id: str, attacker: str | None = Query(None, description="Attacker SteamID."),
              victim: str | None = Query(None, description="Victim SteamID."), round: int | None = None,
              weapon: str | None = Query(None, description="Internal weapon name, e.g. ak47, awp, deagle."),
              headshot: bool | None = None, wallbang: bool | None = None, through_smoke: bool | None = None,
              noscope: bool | None = None, limit: int = Query(500, ge=1, le=2000)) -> list[KillEvent]:
    out: list[KillEvent] = []
    for r in _analysis(match_id).rounds:
        if round is not None and r.number != round:
            continue
        for k in r.kills:
            if attacker and (k.attacker is None or k.attacker.steamid != attacker):
                continue
            if victim and k.victim.steamid != victim:
                continue
            if weapon and k.weapon != weapon.lower():
                continue
            if any(want is not None and got != want for want, got in ((headshot, k.headshot), (wallbang, k.wallbang),
                                                                     (through_smoke, k.through_smoke), (noscope, k.noscope))):
                continue
            out.append(k)
    return out[:limit]


# ------------------------------------------------------------------------------------------- highlights

@api.get("/matches/{match_id}/highlights", response_model=list[HighlightOut], tags=["highlights"],
         summary="Highlights of a match, best first, each with its rendered videos")
def list_highlights(match_id: str, player: str | None = Query(None, description="SteamID."), kind: str | None = None,
                    min_score: float | None = None, limit: int = Query(100, ge=1, le=500),
                    featured: bool | None = Query(None, description="true: only the featured plays (the ones that "
                                                                    "get first-person videos); false: the rest."),
                    category: str = Query("play", pattern="^(play|fail|all)$",
                                          description="play (default) | fail (whiffs, team flashes…) | all.")
                    ) -> list[HighlightOut]:
    with session_scope() as s:
        rows = service.list_highlights(s, match_id, player_steamid=player, kind=kind, min_score=min_score,
                                       limit=limit, featured=featured, category=category)
        return [service.highlight_out(s, h) for h in rows]


@api.post("/matches/{match_id}/highlights", response_model=HighlightOut, status_code=201, tags=["highlights"],
          summary="Create a custom highlight (explicit segments, a tick range, or round + player)")
def create_highlight(match_id: str, body: HighlightCreate) -> HighlightOut:
    with session_scope() as s:
        return service.highlight_out(s, service.create_highlight(s, match_id, body))


@api.get("/highlights/{highlight_id}", response_model=HighlightOut, tags=["highlights"], summary="One highlight")
def get_highlight(highlight_id: str) -> HighlightOut:
    with session_scope() as s:
        return service.highlight_out(s, service.get_highlight(s, highlight_id))


@api.patch("/highlights/{highlight_id}", response_model=HighlightOut, tags=["highlights"],
           summary="Replace title / description / tags / all segments; feature a play; move a hand-made clip "
                   "between plays and fails")
def patch_highlight(highlight_id: str, body: HighlightPatch) -> HighlightOut:
    with session_scope() as s:
        return service.highlight_out(s, service.patch_highlight(s, highlight_id, body))


@api.post("/highlights/{highlight_id}/edit", response_model=HighlightOut, tags=["highlights"],
          summary="Apply edit operations (trim, set_pov, set_speed, add/remove/split/move segment, captions)",
          description="Operations run in order and are all-or-nothing. Existing videos are kept but flagged "
                      "`stale`; request a new render to see the edit.")
def edit_highlight(highlight_id: str, body: EditRequest) -> HighlightOut:
    with session_scope() as s:
        return service.highlight_out(s, service.edit_highlight(s, highlight_id, body))


@api.delete("/highlights/{highlight_id}", status_code=204, tags=["highlights"], summary="Delete a highlight")
def delete_highlight(highlight_id: str) -> None:
    with session_scope() as s:
        service.delete_highlight(s, highlight_id)


# ------------------------------------------------------------------------------------------- renders

@api.post("/highlights/{highlight_id}/renders", response_model=RenderAccepted, status_code=202, tags=["renders"],
          summary="Render a highlight to video (async)")
def render_highlight(highlight_id: str, body: RenderRequest | None = None) -> RenderAccepted:
    body = body or RenderRequest()
    with session_scope() as s:
        h = service.get_highlight(s, highlight_id)
        r, j = service.create_render(s, h.match_id, [h.id], renderer=body.renderer, options=body.options)
        return RenderAccepted(render=service.render_out(s, r), job=service.job_out(j))


@api.post("/matches/{match_id}/reels", response_model=RenderAccepted, status_code=202, tags=["renders"],
          summary="Render several highlights back to back into one video (async)")
def render_reel(match_id: str, body: ReelRequest | None = None) -> RenderAccepted:
    body = body or ReelRequest()
    with session_scope() as s:
        ids = body.highlight_ids or service.pick_top_highlights(s, match_id, body.top, body.player_steamid)
        r, j = service.create_render(s, match_id, ids, renderer=body.renderer, options=body.options, kind="reel",
                                     title=body.title or "Match highlights")
        return RenderAccepted(render=service.render_out(s, r), job=service.job_out(j))


@api.post("/matches/{match_id}/produce", response_model=ProduceAccepted, status_code=202, tags=["renders"],
          summary="Make every video of a match: each play, first-person and 2D, 16:9 and 9:16 (async)",
          description="Plays whose first-person footage is not recorded yet are captured together in one game "
                      "session (`capture_job`); their renders are queued when it finishes.")
def produce_match(match_id: str, body: ProduceRequest | None = None) -> ProduceAccepted:
    body = body or ProduceRequest()
    with session_scope() as s:
        out = service.produce(s, match_id, renderers=list(body.renderers), formats=list(body.formats),
                              highlight_ids=body.highlight_ids, force=body.force, pov=body.pov)
        s.flush()
        job = out.pop("capture_job")
        return ProduceAccepted(capture_job=service.job_out(job) if job else None, **out)


@api.post("/matches/{match_id}/rescore", tags=["highlights"],
          summary="Re-run the highlight scoring and curation on an analyzed match (the demo is not read again)",
          description="Highlights keep their ids and videos. Use after upgrading, or to undo nothing: featured "
                      "plays chosen by hand are kept.")
def rescore_match(match_id: str) -> dict:
    with session_scope() as s:
        return service.rescore_match(s, match_id)


@api.get("/matches/{match_id}/renders", response_model=list[RenderOut], tags=["renders"], summary="Videos of a match")
def list_renders(match_id: str) -> list[RenderOut]:
    with session_scope() as s:
        return [service.render_out(s, r) for r in service.list_renders(s, match_id)]


@api.get("/renders/{render_id}", response_model=RenderOut, tags=["renders"], summary="Render status and video URL")
def get_render(render_id: str) -> RenderOut:
    with session_scope() as s:
        return service.render_out(s, service.get_render(s, render_id))


@api.delete("/renders/{render_id}", status_code=204, tags=["renders"], summary="Delete a video (cancels it if running)")
def delete_render(render_id: str) -> None:
    with session_scope() as s:
        service.delete_render(s, render_id)


# ------------------------------------------------------------------------------------------- jobs

@api.get("/jobs", response_model=list[JobOut], tags=["jobs"], summary="Recent jobs")
def list_jobs(status: str | None = None, match_id: str | None = None, limit: int = Query(50, ge=1, le=200)) -> list[JobOut]:
    with session_scope() as s:
        q = select(Job).order_by(Job.created_at.desc()).limit(limit)
        if status:
            q = q.where(Job.status == status)
        if match_id:
            q = q.where(Job.match_id == match_id)
        return [service.job_out(j) for j in s.scalars(q).all()]


@api.get("/jobs/{job_id}", response_model=JobOut, tags=["jobs"], summary="Job status (supports long-polling)")
async def get_job(job_id: str, wait: float = Query(0, ge=0, le=120, description="Block up to this many seconds "
                                                   "until the job finishes, instead of polling in a loop. The "
                                                   "answer may come earlier when many clients are waiting: look "
                                                   "at `done`.")) -> JobOut:
    # async: a long wait must not hold one of the threads every other route shares
    return await service.wait_job_async(job_id, wait)


@api.post("/jobs/{job_id}/cancel", response_model=JobOut, tags=["jobs"], summary="Cancel a queued or running job")
def cancel_job(job_id: str) -> JobOut:
    with session_scope() as s:
        return service.job_out(jobs.request_cancel(s, service.get_job(s, job_id)))


# ------------------------------------------------------------------------------------------- webhooks

@api.get("/webhooks", response_model=list[WebhookOut], tags=["webhooks"],
         summary="Registered webhooks (organiser only)")
def list_webhooks(request: Request) -> list[WebhookOut]:
    if not is_admin(request):  # a Discord or Slack webhook address is a password: whoever has it posts there
        raise HTTPException(403, "Only the organiser can see the webhooks (send the organiser's key).")
    with session_scope() as s:
        return [WebhookOut(id=w.id, url=w.url, events=w.events or [], active=bool(w.active), created_at=w.created_at)
                for w in s.scalars(select(Webhook)).all()]


@api.post("/webhooks", response_model=WebhookOut, status_code=201, tags=["webhooks"],
          summary="Get an HTTP POST when a match is analyzed or a video is ready")
def create_webhook(body: WebhookIn) -> WebhookOut:
    bad = [e for e in body.events if e not in EVENTS]
    if bad:
        raise HTTPException(400, f"Unknown events {bad}. Valid: {EVENTS}")
    if not body.url.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "Webhook url must be http(s).")
    with session_scope() as s:
        w = Webhook(url=body.url, events=body.events, secret=body.secret)
        s.add(w)
        s.flush()
        return WebhookOut(id=w.id, url=w.url, events=w.events or [], active=True, created_at=w.created_at)


@api.delete("/webhooks/{webhook_id}", status_code=204, tags=["webhooks"], summary="Remove a webhook")
def delete_webhook(webhook_id: str) -> None:
    with session_scope() as s:
        w = s.get(Webhook, webhook_id)
        if w is None:
            raise HTTPException(404, "Webhook not found.")
        s.delete(w)


# ------------------------------------------------------------------------------------------- remote workers
# Two sides. The organiser registers and removes workers (below, on the main router). The worker itself talks
# to /api/v1/worker/* with its own token: those routes do not accept the organiser's key, and a worker token
# opens nothing else. See docs/workers.md.

@api.get("/workers", response_model=list[WorkerOut], tags=["workers"],
         summary="Remote workers registered on this server (organiser only)")
def list_remote_workers(request: Request) -> list[WorkerOut]:
    if not is_admin(request):
        raise HTTPException(403, "Only the organiser can see the workers (send the organiser's key).")
    with session_scope() as s:
        return [workers.worker_out(s, w) for w in workers.list_workers(s)]


@api.post("/workers", response_model=WorkerCreated, status_code=201, tags=["workers"],
          summary="Register a remote worker; the answer carries its token, shown only this once")
def create_remote_worker(body: WorkerCreate) -> WorkerCreated:
    with session_scope() as s:
        w, token = workers.create_worker(s, body.name, body.capabilities)
        return WorkerCreated(worker=workers.worker_out(s, w), token=token)


@api.delete("/workers/{name_or_id}", status_code=204, tags=["workers"],
            summary="Remove a worker: its token stops working at once")
def delete_remote_worker(name_or_id: str) -> None:
    with session_scope() as s:
        workers.remove_worker(s, name_or_id)


def current_worker(request: Request) -> str:
    """Id of the worker whose token this request carries."""
    with session_scope() as s:
        return workers.authenticate(s, _given_key(request)).id


worker_api = APIRouter(prefix="/api/v1/worker", tags=["worker protocol"])
WorkerId = Annotated[str, Depends(current_worker)]


@worker_api.get("/me", response_model=WorkerOut, summary="Who this token is (a quick way to test it)")
def worker_me(worker_id: WorkerId) -> WorkerOut:
    with session_scope() as s:
        return workers.worker_out(s, workers.get_worker(s, worker_id))


@worker_api.post("/claim", summary="Take the next job this worker can run (204 when there is none)",
                 responses={204: {"description": "Nothing to do right now."}})
def worker_claim(body: WorkerClaim, worker_id: WorkerId):
    spec = workers.claim(worker_id, body.capabilities, body.info)
    return spec if spec is not None else Response(status_code=204)


@worker_api.post("/jobs/{job_id}/heartbeat",
                 summary="Say the job is alive and how far it is; the answer says when to stop")
def worker_heartbeat(job_id: str, body: WorkerProgress, worker_id: WorkerId) -> dict[str, Any]:
    with session_scope() as s:
        return workers.heartbeat(s, worker_id, job_id, body.progress, body.message)


@worker_api.put("/jobs/{job_id}/files/{name}",
                summary="Send one result file (raw body; its SHA-256 goes in X-Content-SHA256)")
async def worker_upload(job_id: str, name: str, request: Request, worker_id: WorkerId) -> dict[str, Any]:
    with session_scope() as s:
        upload = workers.open_upload(s, worker_id, job_id, name, request.headers.get("x-content-sha256"))
    try:
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > upload.limit:  # answer before reading the body
            raise workers.TooLarge(f"{name} is {int(declared) // workers.MB} MB; this server accepts up to "
                                   f"{upload.limit // workers.MB} MB for it.")
        async for chunk in request.stream():
            upload.write(chunk)
    except Exception:  # too large, or the connection dropped: leave no half file behind
        upload.abort()
        raise
    return upload.close()


@worker_api.get("/jobs/{job_id}/files/{name}", summary="Fetch an input of the job: demo.dem or analysis.json")
def worker_download(job_id: str, name: str, worker_id: WorkerId):
    with session_scope() as s:
        path = workers.input_file(s, worker_id, job_id, name)
    return FileResponse(path, filename=name)


@worker_api.get("/jobs/{job_id}/brand", summary="The brand kit of the job's match: its files, sizes and hashes")
def worker_brand(job_id: str, worker_id: WorkerId) -> dict[str, Any]:
    with session_scope() as s:
        return workers.brand_manifest(s, worker_id, job_id)


@worker_api.get("/jobs/{job_id}/brand/{path:path}", summary="One file of that brand kit")
def worker_brand_file(job_id: str, path: str, worker_id: WorkerId):
    with session_scope() as s:
        target = workers.brand_file(s, worker_id, job_id, path)
    return FileResponse(target)


@worker_api.post("/jobs/{job_id}/finish", response_model=JobOut,
                 summary="Close the job: succeeded (after its files were sent), failed, canceled or released")
def worker_finish(job_id: str, body: WorkerFinish, worker_id: WorkerId) -> JobOut:
    return workers.finish(worker_id, job_id, body)


@worker_api.put("/masters", summary="Tell the server every recording this worker keeps (replaces the list)")
def worker_masters(body: WorkerMasters, worker_id: WorkerId) -> dict[str, int]:
    with session_scope() as s:
        return {"masters": workers.sync_masters(s, workers.get_worker(s, worker_id), body.masters, replace=True)}


# ------------------------------------------------------------------------------------------- insights

def _insight_scope(
    since: datetime | None = Query(None, description="Only matches played at or after this moment (ISO 8601)."),
    until: datetime | None = Query(None, description="…and before this moment."),
    night: date | None = Query(None, description="One night, YYYY-MM-DD: the evening it started, local time "
                                                 "(matches after midnight belong to the night before)."),
    match_id: str | None = Query(None, description="One match."),
    exclude: list[str] = Query(default_factory=list, description=_EXCLUDE),
) -> insights.Scope:
    return insights.Scope(since=since, until=until, night=night, match_id=match_id, exclude=frozenset(exclude))


@api.get("/insights/nights", response_model=list[NightOut], tags=["insights"],
         summary="Nights that have analyzed matches, newest first")
def insight_nights(since: datetime | None = None, until: datetime | None = None,
                   exclude: list[str] = Query(default_factory=list, description=_EXCLUDE)) -> list[NightOut]:
    with session_scope() as s:
        return insights.nights(s, insights.Scope(since=since, until=until, exclude=frozenset(exclude)))


@api.get("/insights/rivalries", response_model=RivalriesOut, tags=["insights"],
         summary="Who kills whom across matches: pairs, and each player's nemesis and favourite victim")
def insight_rivalries(scope: insights.Scope = Depends(_insight_scope),
                      player: str | None = Query(None, description="SteamID: only the pairs of this player."),
                      min_duels: int | None = Query(None, ge=1, description="Leave out pairs with fewer kills "
                                                    "between them. Default: two per match in the scope, 3 to 10."),
                      sort: str = Query("duels", pattern="^(duels|diff|imbalance)$"),
                      limit: int = Query(50, ge=1, le=500)) -> RivalriesOut:
    with session_scope() as s:
        return insights.rivalries(s, scope, player, min_duels, sort=sort, limit=limit)


@api.get("/insights/players", response_model=list[PlayerSummary], tags=["insights"],
         summary="Everybody who played in the scope (bots excluded), most matches first")
def insight_players(scope: insights.Scope = Depends(_insight_scope)) -> list[PlayerSummary]:
    with session_scope() as s:
        return insights.players(s, scope)


@api.get("/insights/players/{steamid}", response_model=PlayerProfileOut, tags=["insights"],
         summary="One player across matches: totals, best plays, fails, nemesis, chat numbers, history")
def insight_player(steamid: str, scope: insights.Scope = Depends(_insight_scope)) -> PlayerProfileOut:
    with session_scope() as s:
        return insights.player_profile(s, steamid, scope)


@api.get("/insights/weapons", response_model=WeaponRankingsOut, tags=["insights"],
         summary="Kills by weapon: for each weapon, who killed the most with it",
         description="The same kills as the scoreboard (no suicides, no team kills), bots left out. Weapons "
                     "and players come most kills first; each player row has his headshots with the weapon, "
                     "the share of his kills made with it and the matches he played in the scope.")
def insight_weapons(scope: insights.Scope = Depends(_insight_scope),
                    weapon: str | None = Query(None, description="Only this weapon: display name ('AK-47') or "
                                                                 "the game's name ('ak47')."),
                    limit: int | None = Query(None, ge=1, le=500, description="Players per weapon. Default: all.")
                    ) -> WeaponRankingsOut:
    with session_scope() as s:
        return insights.weapon_rankings(s, scope, weapon, limit)


@api.get("/insights/trophies", response_model=TrophiesOut, tags=["insights"],
         summary="Trophies of one match or one night (default: the most recent night)")
def insight_trophies(scope: insights.Scope = Depends(_insight_scope)) -> TrophiesOut:
    with session_scope() as s:
        return insights.trophies(s, scope)


@api.get("/insights/chat", response_model=ChatRankingsOut, tags=["insights"],
         summary="All-chat rankings for banter: potty mouth, cry-baby, chatterbox, giggler, good sport, caps",
         description="A word list tuned on Brazilian mix chat, made for friends to tease each other - not "
                     "moderation. Example lines have slurs masked.")
def insight_chat(scope: insights.Scope = Depends(_insight_scope)) -> ChatRankingsOut:
    with session_scope() as s:
        return insights.chat_rankings(s, scope)


# ------------------------------------------------------------------------------------------- app

async def _answer(send, status: int, detail: str, headers: list[tuple[bytes, bytes]] | None = None) -> None:
    """A JSON error straight from a middleware, shaped like FastAPI's (``{"detail": ...}``)."""
    import json as _json

    body = _json.dumps({"detail": detail}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                            *(headers or [])]})
    await send({"type": "http.response.body", "body": body})


def _origin_of(url: str) -> str | None:
    """``https://Example.org/path`` -> ``https://example.org``; None for what is not an http(s) origin."""
    try:
        u = urlparse(url.strip())
    except ValueError:
        return None
    netloc = u.netloc.lower()
    if u.scheme not in ("http", "https") or not _NETLOC.match(netloc):
        return None  # also keeps anything that could break out of a header value (spaces, ';', quotes)
    return f"{u.scheme}://{netloc}"


_NETLOC = re.compile(r"^[a-z0-9.\-]+(:\d{1,5})?$|^\[[0-9a-f:.]+\](:\d{1,5})?$")


_POLICY_TTL_S = 5.0  # the brand kits are read again after this long
_policy_cache: dict[str, tuple[Any, float, Any]] = {}


def _embed_parents() -> set[str]:
    """Origins of the sites the brand kits say embed this UI (``ui.embed.parents``)."""
    from .brand import list_brands, load_brand

    out: set[str] = set()
    for slug in list_brands():
        try:
            kit = load_brand(slug)
        except Exception:  # noqa: BLE001 - a broken kit must not break every request
            continue
        embed = ((kit.ui if kit else None) or {}).get("embed") or {}
        parents = embed.get("parents") if isinstance(embed, dict) else None
        out |= {o for o in (_origin_of(x) for x in parents or [] if isinstance(x, str)) if o}
    return out


def _site_origins() -> frozenset[str]:
    """Other sites whose pages may use this server from the browser: DEMOREEL_CORS_ORIGINS and the kits'
    embed parents. ``*`` in the setting allows every site."""
    settings = get_settings()
    key = (id(settings), tuple(settings.cors_origins), str(settings.data_dir))
    hit = _policy_cache.get("origins")
    if hit and hit[0] == key and time.monotonic() - hit[1] < _POLICY_TTL_S:
        return hit[2]
    origins = {o for o in map(_origin_of, settings.cors_origins) if o} | _embed_parents()
    if any(x.strip() == "*" for x in settings.cors_origins):
        origins.add("*")
    value = frozenset(origins)
    _policy_cache["origins"] = (key, time.monotonic(), value)
    return value


def _origin_allowed(origin: str, host: str) -> bool:
    """May pages of ``origin`` use this server fully (any method, CORS)? Its own pages, pages served by one of
    its own names (localhost on another port: a development UI), and the sites it lists."""
    own = _origin_of(origin)
    if own is None:  # "null" (sandboxed frames, local files) and other schemes
        return False
    netloc = own.split("://", 1)[1]
    if netloc == host.strip().lower() or host_name(netloc) in get_settings().own_hosts():
        return True
    sites = _site_origins()
    return "*" in sites or own in sites


_CORS_ALL = b"GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS"
_CORS_READ = b"GET, HEAD, OPTIONS"


class _BrowserGuard:
    """What a web page may make a browser do here.

    * While no key is set, a request must name one of this server's hosts. Otherwise a page on evil.example
      that points its own name at 127.0.0.1 (DNS rebinding) talks to this server as if it were its own site.
    * A page of another site changes nothing (POST/PUT/PATCH/DELETE with a foreign ``Origin``) and gets CORS
      answers only when the site is listed (DEMOREEL_CORS_ORIGINS, the kits' ``ui.embed.parents``) - or, to
      read, while a key is set: then what a visitor can read is public anyway.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        settings = get_settings()
        headers = Headers(scope=scope)
        host, method = headers.get("host", ""), scope.get("method", "GET").upper()
        if not settings.keyed:
            trusted = settings.trusted_hosts()
            if trusted is not None and host_name(host) not in trusted:
                await _answer(send, 421, (
                    f"This server does not answer to the host name {host_name(host) or '(none)'!r}; it answers to "
                    f"{', '.join(sorted(trusted))}. Add the name to DEMOREEL_ALLOWED_HOSTS, or set "
                    "DEMOREEL_PUBLIC_URL to the address people use."))
                return
        origin = headers.get("origin")
        if origin is None:  # not a cross-site browser request: nothing to decide here
            await self.app(scope, receive, send)
            return
        trusted_origin = _origin_allowed(origin, host)
        if method == "OPTIONS" and "access-control-request-method" in headers:  # CORS preflight
            wanted = headers["access-control-request-method"].upper()
            if trusted_origin:
                allow, methods = origin.encode("latin-1"), _CORS_ALL
            elif settings.keyed and wanted in ("GET", "HEAD"):
                allow, methods = b"*", _CORS_READ
            else:
                await _answer(send, 403, (
                    f"Pages of {origin} may not send {wanted} requests here: "
                    + ("only reading (GET) is open to other sites. " if settings.keyed else
                       "this server has no key, so it only serves its own pages. ")
                    + "If that site is yours, add it to DEMOREEL_CORS_ORIGINS."))
                return
            asked = headers.get("access-control-request-headers", "").encode("latin-1")
            await send({"type": "http.response.start", "status": 200, "headers": [
                (b"access-control-allow-origin", allow), (b"access-control-allow-methods", methods),
                *([(b"access-control-allow-headers", asked)] if asked else []),
                (b"access-control-max-age", b"600"), (b"vary", b"Origin"), (b"content-length", b"0")]})
            await send({"type": "http.response.body", "body": b""})
            return
        if method not in _SAFE_METHODS and not trusted_origin:
            await _answer(send, 403, (
                f"This server takes no changes from pages of other sites (Origin: {origin}). Use the interface "
                "it serves; if that site is yours, add it to DEMOREEL_CORS_ORIGINS."))
            return
        cors = origin if trusted_origin else ("*" if settings.keyed and method in _SAFE_METHODS else None)

        async def send_with_cors(message):
            if message["type"] == "http.response.start" and cors:
                h = MutableHeaders(scope=message)
                h["access-control-allow-origin"] = cors
                h.add_vary_header("Origin")
            await send(message)

        await self.app(scope, receive, send_with_cors)


def _frame_ancestors() -> str:
    """CSP ``frame-ancestors``: this server, the kits' ``ui.embed.parents`` and DEMOREEL_FRAME_ANCESTORS."""
    settings = get_settings()
    key = (id(settings), tuple(settings.frame_ancestors), str(settings.data_dir))
    hit = _policy_cache.get("frames")
    if hit and hit[0] == key and time.monotonic() - hit[1] < _POLICY_TTL_S:
        return hit[2]
    if any(x.strip() == "*" for x in settings.frame_ancestors):
        value = "*"
    else:
        sources = {o for o in map(_origin_of, settings.frame_ancestors) if o} | _embed_parents()
        value = " ".join(["'self'", *sorted(sources)])
    _policy_cache["frames"] = (key, time.monotonic(), value)
    return value


class _SecurityHeaders:
    """Headers every answer carries unless the route set its own: no content sniffing, no full URLs in the
    Referer sent to other sites, and only this server and the sites that embed it may frame its pages (a
    page of another site framing the UI could trick clicks on it). A proxy in front may replace them, as the
    mirror's Caddyfile does with its own frame-ancestors."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                h = MutableHeaders(scope=message)
                h.setdefault("x-content-type-options", "nosniff")
                h.setdefault("referrer-policy", "strict-origin-when-cross-origin")
                if "content-security-policy" not in h:
                    h["content-security-policy"] = f"frame-ancestors {_frame_ancestors()}"
            await send(message)

        await self.app(scope, receive, send_with_headers)


class _BodyTooLarge(Exception):
    pass


_WORKER_JSON_BYTES = 16 * 1024 * 1024  # a worker's list of recordings can be long


class _RequestGate:
    """Refuse from the headers alone, before a byte of the body is read or stored.

    FastAPI resolves the key dependency only after it has loaded the body: a visitor could make the server
    spool an upload to disk or parse a huge JSON before hearing "403". Here a request under ``/api/v1`` without
    the key it needs is answered at once, and every body is held to a size (``max_json_kb`` for JSON,
    ``max_upload_mb`` for a demo upload) whether or not it declares its length.
    """

    def __init__(self, app):
        self.app = app

    @staticmethod
    def _limit(path: str, method: str) -> int | None:
        settings = get_settings()
        if method in _SAFE_METHODS or not path.startswith("/api/"):
            return None  # /mcp limits its own bodies; other paths take no body
        if path.rstrip("/") == "/api/v1/matches":
            return settings.max_upload_mb * 1024 * 1024 + 1024 * 1024  # the multipart wrapping
        if path.startswith("/api/v1/worker/"):
            # a result file: the route knows its limit and checks it before and while reading
            return None if "/files/" in path else max(_WORKER_JSON_BYTES, settings.max_json_kb * 1024)
        return settings.max_json_kb * 1024

    def _too_large(self, path: str, limit: int) -> str:
        if path.rstrip("/") == "/api/v1/matches":
            return _upload_too_large()
        return (f"The request body is larger than this server accepts here ({limit // 1024} KB, "
                "DEMOREEL_MAX_JSON_KB). Send less at once (fewer ids, shorter texts).")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path, method = scope.get("path", ""), scope.get("method", "GET").upper()
        headers = Headers(scope=scope)
        if path.startswith("/api/v1/worker/"):
            if not _key_in(headers):
                await _answer(send, 401, "Missing or unknown worker token. Send `Authorization: Bearer <token>`; "
                                         "the organiser creates one with `demoreel workers add <name>`.")
                return
        elif path.startswith("/api/"):
            problem = _key_problem(method, _key_in(headers))
            if problem:
                await _answer(send, *problem)
                return
        limit = self._limit(path, method)
        if limit is None:
            await self.app(scope, receive, send)
            return
        declared = headers.get("content-length", "")
        if declared.isdigit() and int(declared) > limit:
            await _answer(send, 413, self._too_large(path, limit))
            return
        seen, over, started = 0, False, False

        async def counted():
            nonlocal seen, over
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body") or b"")
                if seen > limit:
                    over = True
                    raise _BodyTooLarge()
            return message

        async def watched(message):
            nonlocal started
            if over:
                return  # the app's answer to the cut body is replaced by ours below
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counted, watched)
        except Exception:
            if not over:
                raise
        if over and not started:
            await _answer(send, 413, self._too_large(path, limit))


class _GzipApi:
    """Compress JSON answers of the API. Videos and other files are left alone: compressing an mp4 costs CPU
    for nothing and breaks range requests (seeking)."""

    def __init__(self, app):
        from starlette.middleware.gzip import GZipMiddleware

        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=1024)

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "") if scope["type"] == "http" else ""
        # ...nor the files a remote worker fetches (a demo is hundreds of MB).
        worker_file = path.startswith("/api/v1/worker/jobs/") and ("/files/" in path or "/brand/" in path)
        if path.startswith("/api/") and not worker_file:
            await self.gzip(scope, receive, send)
        else:
            await self.app(scope, receive, send)


_SHARE_PAGE = """<!doctype html>
<html lang="{lang}"><head><meta charset="utf-8">
<title>{title}</title>
<meta name="description" content="{description}">
<meta property="og:type" content="video.other">
<meta property="og:site_name" content="{site}">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{description}">
{media}<meta name="twitter:card" content="summary_large_image">
<meta http-equiv="refresh" content="0; url={target}">
<link rel="canonical" href="{target}">
</head><body><p><a href="{target}">{title}</a></p>
<script>location.replace({target_js});</script></body></html>"""


class _McpNoRedirect:
    """`/mcp` and `/mcp/` are the same endpoint. Without this, Starlette answers `/mcp` with a 307 redirect,
    which some MCP clients do not follow for POST requests."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") == "/mcp":
            scope = dict(scope, path="/mcp/", raw_path=b"/mcp/")
        await self.app(scope, receive, send)


def create_app() -> FastAPI:
    settings = get_settings()
    init_db()

    mcp_app = None
    mcp_server = None
    try:
        from .mcp_server import build_http_app

        mcp_server, mcp_app = build_http_app()
    except Exception as exc:  # noqa: BLE001 - the REST API must still start if MCP cannot
        import logging

        logging.getLogger("demoreel").warning("MCP endpoint disabled: %s", exc)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if mcp_server is not None:
            async with mcp_server.session_manager.run():
                yield
        else:
            yield

    app = FastAPI(
        title="demoreel", version=__version__, lifespan=lifespan,
        description="Upload a CS2 demo, get match metrics and automatic highlight videos. "
                    "Everything is asynchronous: uploads and renders return a job you can long-poll "
                    "(`GET /api/v1/jobs/{id}?wait=30`) or receive via webhooks. An MCP server for LLM agents "
                    "is mounted at `/mcp`.",
    )
    app.add_middleware(_GzipApi)
    app.add_middleware(_RequestGate)
    app.add_middleware(_BrowserGuard)
    app.add_middleware(_SecurityHeaders)
    app.add_middleware(_McpNoRedirect)

    @app.exception_handler(service.ServiceError)
    async def _service_error(_request: Request, exc: service.ServiceError):
        return JSONResponse({"detail": exc.message}, status_code=exc.status_code)

    @app.get("/health", tags=["meta"], summary="Liveness probe")
    def health() -> dict[str, Any]:
        with session_scope() as s:
            queued = len(s.scalars(select(Job.id).where(Job.status == "queued")).all())
            running = len(s.scalars(select(Job.id).where(Job.status == "running")).all())
        return {"status": "ok", "version": __version__, "jobs": {"queued": queued, "running": running}}

    @app.get("/files/{path:path}", include_in_schema=False)
    def files(path: str, request: Request, exp: int | None = None, sig: str | None = None):
        root = settings.data_dir.resolve()
        target = (root / path).resolve()
        # Videos, and the public assets of brand kits (logos). Never demos, the database or analyses.
        allowed = (target.is_relative_to(root / "renders")
                   or (target.is_relative_to(root / "brands")
                       and target.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".svg"}))
        if not allowed or not target.is_file():
            raise HTTPException(404, "File not found.")
        if settings.api_key:
            signed = (exp is not None and sig is not None and exp >= time.time()
                      and hmac.compare_digest(sig, service.sign_path(path, exp)))
            if not signed:
                require_key(request)
        if target.suffix.lower() == ".svg":
            # A brand kit's SVG is served from the UI's own origin: opened directly, a script inside it could
            # read the key the UI keeps in the browser. Sandboxed, it is only a picture.
            return FileResponse(target, headers={"Content-Security-Policy": "sandbox"})
        return FileResponse(target)

    @app.get("/p/{highlight_id}", include_in_schema=False)
    def share_page(highlight_id: str, view: str | None = Query(None, pattern=r"^[a-z0-9:x_-]{1,24}$")):
        """A real URL per play, so a link pasted in a chat shows the play (Open Graph tags), then opens the app:
        on this server, or where the brand kit says the app lives (``ui.embed.share_base``)."""
        import html
        import json as _json

        from fastapi.responses import HTMLResponse

        from .brand import brand_for

        with session_scope() as s:
            try:
                h = service.get_highlight(s, highlight_id)
            except service.ServiceError as exc:
                raise HTTPException(404, "Play not found.") from exc
            m = service.get_match(s, h.match_id)
            brand = brand_for(m.brand)
            best = next((i.render for i in service.feed(s, limit=200) if i.highlight.id == h.id), None)
            title = h.title or "Highlight"
            bits = [x for x in (m.map, f"round {h.round}" if h.round else None,
                                f"{m.team_a_name} {m.team_a_score}:{m.team_b_score} {m.team_b_name}"
                                if m.team_a_score is not None else None) if x]
        embed = (brand.ui or {}).get("embed") or {}
        base = str(embed.get("share_base") or f"{settings.base_url}/").rstrip("#")
        target = f"{base}#/play/{highlight_id}" + (f"?view={view}" if view else "")
        media = ""
        if best is not None and best.thumbnail_url:
            media += f'<meta property="og:image" content="{html.escape(best.thumbnail_url, quote=True)}">\n'
        if best is not None and best.video_url:
            media += f'<meta property="og:video" content="{html.escape(best.video_url, quote=True)}">\n'
            media += '<meta property="og:video:type" content="video/mp4">\n'
        page = _SHARE_PAGE.format(
            lang=html.escape(brand.locale or "en", quote=True), title=html.escape(title, quote=True),
            description=html.escape(" · ".join(bits), quote=True), site=html.escape(brand.name, quote=True),
            media=media, target=html.escape(target, quote=True),
            target_js=_json.dumps(target).replace("</", "<\\/"))  # "</script>" in a kit's URL ends no script
        return HTMLResponse(page)

    @app.get("/llms.txt", include_in_schema=False, response_class=PlainTextResponse)
    def llms_txt() -> str:
        p = WEB_DIR / "llms.txt"
        return p.read_text(encoding="utf-8").replace("{BASE_URL}", settings.base_url) if p.exists() else ""

    @app.middleware("http")
    async def _revalidate_ui(request, call_next):
        response = await call_next(request)
        path = request.url.path
        if not path.startswith(("/api/", "/files/", "/mcp")) and "cache-control" not in response.headers:
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.include_router(api)
    app.include_router(worker_api)
    if mcp_app is not None:
        app.mount("/mcp", mcp_app)
    if (WEB_DIR / "index.html").exists():
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app
