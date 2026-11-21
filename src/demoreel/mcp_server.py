"""MCP server: lets LLM agents query matches and edit / render highlights.

Two transports:

* Streamable HTTP, mounted by the API at ``/mcp`` (remote agents, claude.ai connectors, etc.)
* stdio, via ``demoreel mcp`` (local desktop clients such as Claude Desktop / Claude Code)

Tools are thin wrappers over :mod:`demoreel.service`, so agents and the REST API always behave the same.
Outputs are deliberately compact (agents pay for every token) and every long operation returns a job id
that ``get_job`` can wait on.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

import anyio
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__, insights, jobs, service
from .analysis.schema import HighlightSegment, MatchAnalysis
from .config import get_settings
from .db import init_db, session_scope
from .schemas import EditOp, EditRequest, HighlightCreate, RenderOptionsIn

INSTRUCTIONS = """\
demoreel analyzes Counter-Strike 2 demos and produces highlight videos.

Concepts
- match: one uploaded demo. `get_match` returns the scoreboard and its best highlights.
- highlight: an editable clip definition = ordered `segments`, each {start_tick, end_tick, pov_steamid, speed,
  caption}. Ticks are demo ticks; seconds = ticks / tickrate (64 for CS2).
- render: a video file produced from one highlight (or several = a "reel"). Renders are asynchronous: tools
  return a `job_id`; call `get_job(job_id, wait_seconds=30)` until `done` is true, then read `video_url`.
- Editing a highlight never changes existing videos; they are flagged `stale`. Render again to see the edit.
- Videos are branded automatically (the community's animated intro/outro, HUD with nameplate and kill
  counter, captions). First-person footage is recorded once and kept: trims, slow motion, captions and
  re-branding reuse it, so `renderer: "ingame"` often works even when the game is not available.
- Slow motion: split a segment around the moment and set its speed (0.3–0.5 reads well for a final kill).

Typical flows
- "What happened in this match?" -> list_matches -> get_match -> get_player_stats / list_rounds / get_round.
- "Make X's clutch tighter and slow down the last kill" -> list_highlights -> get_highlight -> edit_highlight
  (trim / split_segment / set_speed) -> render_highlight -> get_job.
- "Clip the moment at round 12 where Y throws the smoke" -> get_round to find ticks -> create_highlight.
- "Give me a 5-clip montage of player Z" -> render_reel(player=Z, top=5) -> get_job.
- "Bring in last Friday's games" -> list_source_matches() -> list_source_matches(source_id, only_missing=true)
  -> import_matches -> get_job for each -> get_match.
- "What were the best plays this week?" -> get_feed.
- "Who owns whom? Who cried the most last night? Give out the trophies" -> list_nights -> get_rivalries /
  get_trophies / get_chat_rankings (all take night=YYYY-MM-DD or match_id; no filter = every match) ->
  get_player_profile for one player across matches.
- "Who has the most AWP kills this season?" -> get_weapon_rankings(weapon="AWP").

Players can be referred to by SteamID or by (part of) their in-game name.
"""

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)


def _threaded(fn):
    """Run a blocking tool body in a worker thread so the MCP event loop stays responsive."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
        except service.ServiceError as exc:
            raise ToolError(exc.message) from exc  # anticipated failure: the agent sees the message and can fix it
    return wrapper


def _analysis(match_id: str) -> MatchAnalysis:
    with session_scope() as s:
        return service.load_analysis(service.get_match(s, match_id))


def _resolve_player(analysis: MatchAnalysis, player: str) -> str:
    """SteamID or (partial, case-insensitive) name -> SteamID."""
    for p in analysis.players:
        if p.steamid == player:
            return p.steamid
    exact = [p for p in analysis.players if p.name.lower() == player.lower()]
    partial = exact or [p for p in analysis.players if player.lower() in p.name.lower()]
    if len(partial) == 1:
        return partial[0].steamid
    names = ", ".join(f"{p.name} ({p.steamid})" for p in analysis.players)
    if not partial:
        raise service.ServiceError(f"No player matches {player!r}. Players in this match: {names}")
    raise service.ServiceError(f"{player!r} is ambiguous. Use a SteamID. Players in this match: {names}")


def _highlight_brief(h) -> dict[str, Any]:
    ready = next((r for r in h.renders if r.status == "ready"), None)
    return {"id": h.id, "kind": h.kind, "title": h.title, "round": h.round, "player": h.player.name,
            "player_steamid": h.player.steamid, "score": h.score, "tier": h.tier, "featured": h.featured,
            "category": h.category, "chat": [f"{c.get('name')}: {c.get('text')}" for c in h.chat],
            "team": h.team, "duration_s": h.duration_s, "tags": h.tags,
            "segments": len(h.segments), "origin": h.origin,
            "video_url": ready.video_url if ready else None,
            "video_stale": ready.stale if ready else None,
            "render_status": h.renders[0].status if h.renders else "not_rendered"}


# Scope of the insight tools (module level: tool signatures are resolved against the module's names).
Night = Annotated[str | None, Field(description="One night, YYYY-MM-DD (the evening it started; games after "
                                                "midnight belong to the night before). See list_nights.")]
Since = Annotated[str | None, Field(description="Only matches played at or after this ISO 8601 moment.")]
Until = Annotated[str | None, Field(description="Only matches played before this ISO 8601 moment.")]
Exclude = Annotated[list[str] | None, Field(description="Match ids to leave out, whatever the other filters "
                                                       "say (e.g. matches hidden on the community's site).")]


def build_server() -> MCPServer:
    init_db()
    mcp = MCPServer("demoreel", title="demoreel — CS2 demo analysis & highlights", version=__version__,
                    instructions=INSTRUCTIONS)

    # ------------------------------------------------------------------ read: matches
    @mcp.tool(annotations=READ, description="List analyzed matches (newest first) with map, score and status.")
    @_threaded
    def list_matches(limit: Annotated[int, Field(ge=1, le=100)] = 20, status: str | None = None) -> list[dict[str, Any]]:
        with session_scope() as s:
            out = []
            for m in service.list_matches(s, limit=limit, status=status):
                o = service.match_out(s, m)
                out.append({"id": o.id, "status": o.status, "map": o.map, "filename": o.filename,
                            "score": f"{o.team_a.name} {o.team_a.score} : {o.team_b.score} {o.team_b.name}"
                            if o.status == "ready" else None,
                            "rounds": o.rounds, "highlights": o.highlight_count, "error": o.error,
                            "created_at": o.created_at.isoformat()})
            return out

    @mcp.tool(annotations=READ, description="Match overview: status, map, teams, scoreboard (K/D/A, ADR, KAST, "
                                            "rating per player) and the top highlights. Start here.")
    @_threaded
    def get_match(match_id: str) -> dict[str, Any]:
        with session_scope() as s:
            m = service.get_match(s, match_id)
            o = service.match_out(s, m)
            base = {"id": o.id, "status": o.status, "error": o.error, "filename": o.filename, "map": o.map,
                    "server_name": o.server_name, "tickrate": o.tickrate, "total_ticks": o.total_ticks,
                    "duration_s": o.duration_s}
            if o.status != "ready":
                return base
            analysis = service.load_analysis(m)
            tops = [_highlight_brief(service.highlight_out(s, h)) for h in service.list_highlights(s, match_id, limit=8)]
        return {**base, **service.scoreboard(analysis), "top_highlights": tops, "warnings": analysis.warnings}

    @mcp.tool(annotations=READ, description="Every stat of one player: rating, ADR, KAST, opening duels, trades, "
                                            "multi-kills, clutches, utility, per-weapon kills, CT/T split.")
    @_threaded
    def get_player_stats(match_id: str, player: Annotated[str, Field(description="SteamID or player name.")]) -> dict[str, Any]:
        a = _analysis(match_id)
        sid = _resolve_player(a, player)
        return next(p for p in a.players if p.steamid == sid).model_dump()

    @mcp.tool(annotations=READ, description="One line per round: winner, reason, score, buys, bomb, first kill, "
                                            "tick range. Use get_round for the kill-by-kill detail.")
    @_threaded
    def list_rounds(match_id: str) -> list[dict[str, Any]]:
        out = []
        for r in _analysis(match_id).rounds:
            first = next((k for k in r.kills if k.is_opening), None)
            out.append({"round": r.number, "winner_side": r.winner_side, "winner_team": r.winner_team,
                        "reason": r.reason, "score": f"{r.score_team_a}-{r.score_team_b}", "duration_s": r.duration_s,
                        "ct_buy": r.ct_buy, "t_buy": r.t_buy, "bomb_planted": r.bomb_planted, "bomb_site": r.bomb_site,
                        "kills": len(r.kills), "clutches": [f"{c.player.name} 1v{c.versus} {'won' if c.won else 'lost'}"
                                                            for c in r.clutches],
                        "first_kill": f"{first.attacker.name} > {first.victim.name}" if first and first.attacker else None,
                        "start_tick": r.start_tick, "freeze_end_tick": r.freeze_end_tick, "end_tick": r.end_tick})
        return out

    @mcp.tool(annotations=READ, description="Full detail of one round: every kill (tick, weapon, headshot, "
                                            "wallbang, smoke, trade…), clutches, bomb events, economy.")
    @_threaded
    def get_round(match_id: str, round_number: int) -> dict[str, Any]:
        for r in _analysis(match_id).rounds:
            if r.number == round_number:
                return r.model_dump(exclude_none=True)
        raise service.ServiceError(f"Round {round_number} does not exist.")

    @mcp.tool(annotations=READ, description="Search the kill log. All filters are optional and combined with AND.")
    @_threaded
    def search_kills(match_id: str, attacker: str | None = None, victim: str | None = None,
                     round_number: int | None = None,
                     weapon: Annotated[str | None, Field(description="Internal name: ak47, awp, deagle, knife…")] = None,
                     headshot: bool | None = None, wallbang: bool | None = None, through_smoke: bool | None = None,
                     noscope: bool | None = None, limit: Annotated[int, Field(ge=1, le=300)] = 60) -> list[dict[str, Any]]:
        a = _analysis(match_id)
        att = _resolve_player(a, attacker) if attacker else None
        vic = _resolve_player(a, victim) if victim else None
        out = []
        for r in a.rounds:
            if round_number is not None and r.number != round_number:
                continue
            for k in r.kills:
                if att and (k.attacker is None or k.attacker.steamid != att):
                    continue
                if vic and k.victim.steamid != vic:
                    continue
                if weapon and k.weapon != weapon.lower():
                    continue
                if any(w is not None and g != w for w, g in ((headshot, k.headshot), (wallbang, k.wallbang),
                                                             (through_smoke, k.through_smoke), (noscope, k.noscope))):
                    continue
                flags = [f for f, on in (("headshot", k.headshot), ("wallbang", k.wallbang), ("noscope", k.noscope),
                                         ("through_smoke", k.through_smoke), ("blind", k.attacker_blind),
                                         ("airborne", k.attacker_airborne), ("opening", k.is_opening),
                                         ("trade", k.is_trade), ("teamkill", k.is_teamkill)) if on]
                out.append({"round": k.round, "tick": k.tick, "clock_s": k.clock_s,
                            "attacker": k.attacker.name if k.attacker else None,
                            "attacker_steamid": k.attacker.steamid if k.attacker else None,
                            "victim": k.victim.name, "weapon": k.weapon_name, "distance_m": k.distance_m, "flags": flags})
        return out[:limit]

    # ------------------------------------------------------------------ highlights
    @mcp.tool(annotations=READ, description=(
        "Highlights of a match, best first, with video URL when rendered. `score` is 0-100 and `tier` S/A/B/C; "
        "`featured` plays are the curated best of each team plus stand-out plays - they get first-person "
        "videos, the others only 2D replays. get_highlight shows the score `breakdown` (feat, impact, style, "
        "economy, stakes, outcome). Pass featured=true for just the curated ones."))
    @_threaded
    def list_highlights(match_id: str, player: str | None = None, kind: str | None = None,
                        featured: bool | None = None,
                        category: Annotated[Literal["play", "fail", "all"], Field(
                            description="fail = the match's fails (whiffs, team flashes, team kills…).")] = "play",
                        limit: Annotated[int, Field(ge=1, le=100)] = 25) -> list[dict[str, Any]]:
        sid = _resolve_player(_analysis(match_id), player) if player else None
        with session_scope() as s:
            rows = service.list_highlights(s, match_id, player_steamid=sid, kind=kind, limit=limit,
                                           featured=featured, category=category)
            return [_highlight_brief(service.highlight_out(s, h)) for h in rows]

    @mcp.tool(annotations=READ, description="Full highlight: description, every segment (ticks, POV, speed, "
                                            "caption), kill ticks and all its renders. Read this before editing.")
    @_threaded
    def get_highlight(highlight_id: str) -> dict[str, Any]:
        with session_scope() as s:
            h = service.get_highlight(s, highlight_id)
            out = service.highlight_out(s, h).model_dump(mode="json")
            out["tickrate"] = service.get_match(s, h.match_id).tickrate
            return out

    @mcp.tool(annotations=WRITE, description=(
        "Promote a play to the featured ones (they get first-person videos when the match is produced) or "
        "remove it from them. Overrides the automatic curation and survives re-scoring."))
    @_threaded
    def set_featured(highlight_id: str, featured: bool) -> dict[str, Any]:
        from .schemas import HighlightPatch

        with session_scope() as s:
            h = service.patch_highlight(s, highlight_id, HighlightPatch(featured=featured))
            return _highlight_brief(service.highlight_out(s, h))

    @mcp.tool(annotations=WRITE, description=(
        "Re-run the highlight scoring and curation on a match that is already analyzed (fast, the demo is not "
        "read again). Highlights keep their ids and videos; featured plays chosen by hand are kept."))
    @_threaded
    def rescore_match(match_id: str) -> dict[str, Any]:
        with session_scope() as s:
            return service.rescore_match(s, match_id)

    @mcp.tool(annotations=WRITE, description=(
        "Create a custom highlight. Three ways: (1) `segments` = explicit list; (2) `start_tick` + `end_tick` "
        "(+ `pov`) for one continuous moment; (3) `round_number` + `player` to auto-clip that player's kills in "
        "that round. Returns the new highlight (not rendered yet)."))
    @_threaded
    def create_highlight(match_id: str, title: str | None = None, description: str | None = None,
                         segments: list[HighlightSegment] | None = None, start_tick: int | None = None,
                         end_tick: int | None = None, pov: Annotated[str | None, Field(description="Player to watch.")] = None,
                         round_number: int | None = None, player: str | None = None,
                         tags: list[str] | None = None) -> dict[str, Any]:
        a = _analysis(match_id)
        body = HighlightCreate(title=title, description=description, tags=tags or [], segments=segments,
                               start_tick=start_tick, end_tick=end_tick,
                               pov_steamid=_resolve_player(a, pov) if pov else None, round=round_number,
                               player_steamid=_resolve_player(a, player) if player else None, origin="agent")
        with session_scope() as s:
            return service.highlight_out(s, service.create_highlight(s, match_id, body)).model_dump(mode="json")

    @mcp.tool(annotations=WRITE, description=(
        "Edit a highlight with a list of operations applied in order (all-or-nothing). Operations: "
        "trim{segment,start_delta_s,end_delta_s} · set_range{segment,start_tick,end_tick} · "
        "set_pov{segment|null,steamid} · set_speed{segment|null,speed} · set_caption{segment,caption} · "
        "add_segment{start_tick,end_tick,pov_steamid,speed,caption,index} · remove_segment{segment} · "
        "move_segment{segment,to} · split_segment{segment,at_tick} · set_meta{title,description,tags}. "
        "Segment indexes are 0-based. Example slow-motion on the last second of segment 0 that ends at tick "
        "5000: [{op:'split_segment',segment:0,at_tick:4936},{op:'set_speed',segment:1,speed:0.5}]. "
        "Call render_highlight afterwards to produce the new video."))
    @_threaded
    def edit_highlight(highlight_id: str, ops: list[EditOp]) -> dict[str, Any]:
        with session_scope() as s:
            h = service.edit_highlight(s, highlight_id, EditRequest(ops=ops, origin="agent"))
            return service.highlight_out(s, h).model_dump(mode="json")

    @mcp.tool(annotations=DESTRUCTIVE, description="Delete a highlight definition (its rendered videos are kept).")
    @_threaded
    def delete_highlight(highlight_id: str) -> dict[str, Any]:
        with session_scope() as s:
            service.delete_highlight(s, highlight_id)
        return {"deleted": highlight_id}

    # ------------------------------------------------------------------ renders & jobs
    def _render_opts(width: int | None, height: int | None, fps: int | None, extra: dict[str, Any] | None) -> RenderOptionsIn:
        return RenderOptionsIn(width=width, height=height, fps=fps, extra=extra or {})

    @mcp.tool(annotations=WRITE, description=(
        "Render a highlight to an mp4 (asynchronous). renderer: 'auto' (in-game POV if this server can, else "
        "2D), 'radar' (2D tactical replay; extra: {camera:'follow'|'full', zoom:1-3}) or 'ingame' (real CS2 "
        "footage, slower). Returns job_id; if wait_seconds > 0 it waits and includes video_url when done."))
    @_threaded
    def render_highlight(highlight_id: str, renderer: str | None = None, width: int | None = None,
                         height: int | None = None, fps: int | None = None, extra: dict[str, Any] | None = None,
                         wait_seconds: Annotated[float, Field(ge=0, le=120)] = 0) -> dict[str, Any]:
        with session_scope() as s:
            h = service.get_highlight(s, highlight_id)
            r, j = service.create_render(s, h.match_id, [h.id], renderer=renderer,
                                         options=_render_opts(width, height, fps, extra))
            render_id, job_id, name = r.id, j.id, r.renderer
        return _job_result(job_id, wait_seconds, {"render_id": render_id, "renderer": name})

    @mcp.tool(annotations=WRITE, description=(
        "Render a reel: several highlights back to back in one video, with title cards. Pass `highlight_ids` "
        "for an explicit order, or let the server pick the `top` best (optionally only for one `player`)."))
    @_threaded
    def render_reel(match_id: str, highlight_ids: list[str] | None = None,
                    top: Annotated[int, Field(ge=1, le=30)] = 5, player: str | None = None, title: str | None = None,
                    renderer: str | None = None, width: int | None = None, height: int | None = None,
                    fps: int | None = None, extra: dict[str, Any] | None = None,
                    wait_seconds: Annotated[float, Field(ge=0, le=120)] = 0) -> dict[str, Any]:
        sid = _resolve_player(_analysis(match_id), player) if player else None
        with session_scope() as s:
            ids = highlight_ids or service.pick_top_highlights(s, match_id, top, sid)
            r, j = service.create_render(s, match_id, ids, renderer=renderer, kind="reel",
                                         title=title or "Match highlights", options=_render_opts(width, height, fps, extra))
            render_id, job_id, name = r.id, j.id, r.renderer
        return _job_result(job_id, wait_seconds, {"render_id": render_id, "renderer": name, "highlight_ids": ids})

    def _job_result(job_id: str, wait_seconds: float, extra: dict[str, Any]) -> dict[str, Any]:
        j = service.wait_job(job_id, wait_seconds)
        out = {"job_id": j.id, "status": j.status, "done": j.done, "progress": j.progress, "message": j.message,
               "error": j.error, **extra}
        if j.result:
            out.update({k: v for k, v in j.result.items() if k in {"video_url", "thumbnail_url", "duration_s"}})
        if not j.done:
            out["next"] = f"call get_job('{j.id}', wait_seconds=30) until done"
        return out

    @mcp.tool(annotations=READ, description="Status of an analysis or render job. `wait_seconds` blocks until it "
                                            "finishes (max 120) — prefer this over polling in a tight loop.")
    @_threaded
    def get_job(job_id: str, wait_seconds: Annotated[float, Field(ge=0, le=120)] = 30) -> dict[str, Any]:
        with session_scope() as s:
            service.get_job(s, job_id)
        return service.wait_job(job_id, wait_seconds).model_dump(mode="json", exclude_none=True)

    @mcp.tool(annotations=READ, description="A rendered video: status, video_url, thumbnail_url, size, and "
                                            "whether it is stale (highlight edited after rendering).")
    @_threaded
    def get_render(render_id: str) -> dict[str, Any]:
        with session_scope() as s:
            return service.render_out(s, service.get_render(s, render_id)).model_dump(mode="json", exclude_none=True)

    @mcp.tool(annotations=DESTRUCTIVE, description="Cancel a queued or running job.")
    @_threaded
    def cancel_job(job_id: str) -> dict[str, Any]:
        with session_scope() as s:
            return service.job_out(jobs.request_cancel(s, service.get_job(s, job_id))).model_dump(mode="json")

    # ------------------------------------------------------------------ ingest & meta
    @mcp.tool(annotations=WRITE, description=(
        "Analyze a demo file that already exists on the server's disk (inside one of the configured import "
        "folders). To upload from elsewhere use the REST API: POST /api/v1/matches (multipart field `file`). "
        "Returns match_id and job_id; wait with get_job, then call get_match."))
    @_threaded
    def import_demo(path: Annotated[str, Field(description="Absolute path to a .dem (or .dem.gz/.bz2/.zip) file.")],
                    auto_render_top: Annotated[int | None, Field(ge=0, le=20)] = None) -> dict[str, Any]:
        settings = get_settings()
        src = Path(path).expanduser().resolve()
        roots = [d.expanduser().resolve() for d in settings.import_dirs]
        if not roots:
            raise service.ServiceError("Server-side import is disabled. Set DEMOREEL_IMPORT_DIRS to a JSON list of "
                                       "folders (e.g. '[\"C:/demos\"]'), or upload through POST /api/v1/matches.")
        if not any(src.is_relative_to(r) for r in roots):
            raise service.ServiceError(f"Path is outside the allowed import folders: {[str(r) for r in roots]}")
        if not src.is_file():
            raise service.ServiceError(f"File not found: {src}")
        with session_scope() as s:
            m, j, created = service.ingest_demo(s, src, src.name, auto_render_top=auto_render_top)
            return {"match_id": m.id, "status": m.status, "created": created, "job_id": j.id if j else None,
                    "next": f"call get_job('{j.id}') then get_match('{m.id}')" if j else f"call get_match('{m.id}')"}

    @mcp.tool(annotations=READ, description=(
        "The best plays that already have a video, across all matches, best first. Each item has the play "
        "(player, kind, round, tags), its match and the video_url. Use it to answer 'show me the best plays' "
        "or 'what did <player> do lately' without walking every match."))
    @_threaded
    def get_feed(limit: Annotated[int, Field(ge=1, le=100)] = 12, player: str | None = None,
                 kind: str | None = None, exclude: Exclude = None) -> list[dict[str, Any]]:
        with session_scope() as s:
            sid = player
            if player and not player.isdigit():  # a name: look it up in the most recent matches
                for m in service.list_matches(s, limit=30, status="ready"):
                    try:
                        sid = _resolve_player(service.load_analysis(m), player)
                        break
                    except service.ServiceError:
                        continue
            out = []
            for i in service.feed(s, limit=limit, player_steamid=sid, kind=kind, exclude=exclude or ()):
                out.append({"highlight_id": i.highlight.id, "title": i.highlight.title, "kind": i.highlight.kind,
                            "player": i.highlight.player.name, "round": i.highlight.round, "score": i.highlight.score,
                            "tags": i.highlight.tags, "duration_s": i.render.duration_s, "match_id": i.match.id,
                            "map": i.match.map, "renderer": i.render.renderer, "video_url": i.render.video_url})
            return out

    @mcp.tool(annotations=READ, description=(
        "Sites this server can import matches from (for example the community's own match history), and for "
        "one source the matches it lists with their import status here. Call without source_id to list sources."))
    @_threaded
    def list_source_matches(source_id: str | None = None,
                            only_missing: Annotated[bool, Field(description="Only matches with a demo that are "
                                                                            "not imported yet.")] = False,
                            limit: Annotated[int, Field(ge=1, le=200)] = 40) -> dict[str, Any]:
        from . import sources as src

        if source_id is None:
            return {"sources": [{"id": x.id, "name": x.name, "url": x.url} for x in src.list_sources()]}
        source = src.get_source(source_id)
        if source is None:
            raise service.ServiceError(f"Source {source_id!r} is not configured. Available: "
                                       f"{[x.id for x in src.list_sources()]}")
        with session_scope() as s:
            rows = service.remote_matches(s, source)
        if only_missing:
            rows = [r for r in rows if r["has_demo"] and r["match_id"] is None and r["status"] is None]
        return {"source": source.name, "matches": [
            {"remote_id": r["remote_id"], "map": r["map"], "score": f"{r['score_a']}-{r['score_b']}",
             "played_at": r["played_at"].isoformat() if r["played_at"] else None, "has_demo": r["has_demo"],
             "match_id": r["match_id"], "status": r["status"]} for r in rows[:limit]]}

    @mcp.tool(annotations=WRITE, description=(
        "Download and analyze matches from a source (see list_source_matches). Returns one job per match; wait "
        "with get_job, whose result carries the new match_id. Matches already imported or without a demo are "
        "reported in `skipped`."))
    @_threaded
    def import_matches(source_id: str, remote_ids: Annotated[list[str], Field(min_length=1, max_length=20)],
                       auto_render_top: Annotated[int | None, Field(ge=0, le=20)] = None) -> dict[str, Any]:
        from . import sources as src

        source = src.get_source(source_id)
        if source is None:
            raise service.ServiceError(f"Source {source_id!r} is not configured.")
        with session_scope() as s:
            queued, skipped = service.import_remote(s, source, [str(x) for x in remote_ids], auto_render_top)
            return {"jobs": [{"job_id": j.id, "remote_id": (j.payload or {}).get("remote_id")} for j in queued],
                    "skipped": skipped, "next": "call get_job(job_id) for each; result.match_id is the new match"}

    @mcp.tool(annotations=WRITE, description=(
        "Make the videos of a match in one call: 2D replays ('radar') of every highlight and first-person POV "
        "('ingame') of the featured ones - pov='all' records every play instead - in each shape ('16:9', '9:16' "
        "ready to post on phones, '1:1'). Videos that already exist and are up to date are skipped. First-person plays without recorded footage are captured together "
        "in one game session (`capture_job_id`, wait with get_job); their videos are queued when it finishes. "
        "`detail` explains anything that could not be queued (for example: recording is not enabled here)."))
    @_threaded
    def produce_match(match_id: str,
                      renderers: Annotated[list[Literal["ingame", "radar"]] | None, Field(min_length=1)] = None,
                      formats: Annotated[list[Literal["16:9", "9:16", "1:1"]] | None, Field(min_length=1)] = None,
                      highlight_ids: list[str] | None = None, pov: Literal["featured", "all"] = "featured",
                      force: bool = False) -> dict[str, Any]:
        with session_scope() as s:
            out = service.produce(s, match_id, renderers=list(renderers or ["ingame", "radar"]),
                                  formats=list(formats or ["16:9", "9:16"]), highlight_ids=highlight_ids,
                                  force=force, pov=pov)
            s.flush()
            job = out.pop("capture_job")
            return {"capture_job_id": job.id if job else None, **out,
                    "next": "list_highlights(match_id) shows each video URL as it becomes ready"}

    @mcp.tool(annotations=READ, description=(
        "Identity of this instance: the community's name, tagline, site, colours and logo. Videos carry this "
        "brand automatically (animated intro/outro built from the logo, HUD, captions); use the name and tone "
        "when writing titles or captions."))
    @_threaded
    def get_brand() -> dict[str, Any]:
        return service.brand_out().model_dump()

    @mcp.tool(annotations=READ, description=(
        "Remote workers registered on this server: the other machines that take jobs from it (typically a PC "
        "with CS2 that records first-person footage). Shows what each may do, whether it is online, the job it "
        "is working on and how many recordings it keeps. When first-person videos stay queued, look here: the "
        "recording machine may be off or busy. Registering or removing a worker is done by the organiser with "
        "`demoreel workers add|remove` - tokens are never handed out through this tool."))
    @_threaded
    def list_workers() -> list[dict[str, Any]]:
        from . import workers

        with session_scope() as s:
            return [workers.worker_out(s, w).model_dump(mode="json") for w in workers.list_workers(s)]

    @mcp.tool(annotations=READ, description="Server capabilities: which renderers are available here and why not.")
    @_threaded
    def get_capabilities() -> dict[str, Any]:
        s = get_settings()
        return {"version": __version__, "renderers": service.renderers_status(), "default_renderer": s.default_renderer,
                "upload_endpoint": f"{s.base_url}/api/v1/matches", "import_dirs": [str(d) for d in s.import_dirs]}

    # ------------------------------------------------------------------ insights across matches
    def _scope(night: str | None, match_id: str | None, since: str | None, until: str | None,
               exclude: list[str] | None = None) -> insights.Scope:
        from datetime import date, datetime

        try:
            return insights.Scope(night=date.fromisoformat(night) if night else None, match_id=match_id or None,
                                  since=datetime.fromisoformat(since) if since else None,
                                  until=datetime.fromisoformat(until) if until else None,
                                  exclude=frozenset(exclude or ()))
        except ValueError as exc:
            raise service.ServiceError(f"Bad date ({exc}). night is YYYY-MM-DD; since / until are ISO 8601, "
                                       "e.g. 2026-11-13T18:00:00-03:00.") from exc

    def _scope_brief(scope) -> dict[str, Any]:
        out = {"matches": len(scope.matches), "nights": scope.nights, "rounds": scope.rounds}
        return {**out, "match_ids": scope.matches} if len(scope.matches) <= 8 else out

    def _versus(ref) -> str | None:
        return f"{ref.player.name} ({ref.kills}-{ref.deaths})" if ref else None

    @mcp.tool(annotations=READ, description=(
        "Nights of play, newest first: date, match ids, maps, how many players and rounds. A night is every "
        "match from noon to noon, so games after midnight count for the evening before. Use the date as "
        "`night` in get_trophies / get_rivalries / get_chat_rankings / get_player_profile."))
    @_threaded
    def list_nights(exclude: Exclude = None) -> list[dict[str, Any]]:
        with session_scope() as s:
            return [n.model_dump(mode="json", exclude={"first_played_at", "last_played_at"})
                    for n in insights.nights(s, _scope(None, None, None, None, exclude))]

    @mcp.tool(annotations=READ, description=(
        "Who kills whom across matches. `pairs`: two players, kills of each on the other (`score`, the one "
        "ahead first), matches they faced each other, opening kills and knife/Zeus kills of each on the other, "
        "`imbalance` (0 even, 1 one-way). `players`: each player's nemesis ('carrasco', who beats him the most) "
        "and favourite victim ('freguês') as 'name (his kills-his deaths)'. No filter = every match; pass "
        "`player` for one player's rivalries. sort: duels (most kills between them) | diff | imbalance."))
    @_threaded
    def get_rivalries(player: Annotated[str | None, Field(description="SteamID or player name.")] = None,
                      night: Night = None, match_id: str | None = None, since: Since = None, until: Until = None,
                      min_duels: Annotated[int | None, Field(ge=1, description="Leave out pairs with fewer kills "
                                                             "between them (default: 2 per match, 3 to 10).")] = None,
                      sort: Literal["duels", "diff", "imbalance"] = "duels",
                      limit: Annotated[int, Field(ge=1, le=200)] = 15, exclude: Exclude = None) -> dict[str, Any]:
        scope = _scope(night, match_id, since, until, exclude)
        with session_scope() as s:
            sid = insights.resolve_player(s, player, scope) if player else None
            out = insights.rivalries(s, scope, sid, min_duels, sort=sort, limit=limit)
        return {"scope": _scope_brief(out.scope), "min_duels": out.min_duels, "total_pairs": out.total_pairs,
                "pairs": [{"a": p.a.name, "b": p.b.name, "score": f"{p.a_kills}-{p.b_kills}", "duels": p.duels,
                           "imbalance": p.imbalance, "matches": p.matches,
                           "openings": f"{p.a_openings}-{p.b_openings}",
                           "knife_zeus": f"{p.a_humiliations}-{p.b_humiliations}",
                           "a_steamid": p.a.steamid, "b_steamid": p.b.steamid} for p in out.pairs],
                "players": [{"player": p.player.name, "steamid": p.player.steamid, "kills": p.kills,
                             "deaths": p.deaths, "nemesis": _versus(p.nemesis), "victim": _versus(p.victim)}
                            for p in out.players]}

    @mcp.tool(annotations=READ, description=(
        "One player across matches: matches, wins, K/D/A, ADR, KAST and rating (weighted by rounds), HS%, "
        "clutches, opening duels, multi-kills, best plays (highlight ids and scores), fails by kind, nemesis "
        "and favourite victim, chat numbers and the last matches. No filter = every match he played."))
    @_threaded
    def get_player_profile(player: Annotated[str, Field(description="SteamID or player name.")],
                           night: Night = None, match_id: str | None = None, since: Since = None,
                           until: Until = None, exclude: Exclude = None) -> dict[str, Any]:
        scope = _scope(night, match_id, since, until, exclude)
        with session_scope() as s:
            out = insights.player_profile(s, insights.resolve_player(s, player, scope), scope)
        data = out.model_dump(mode="json", exclude_none=True, exclude={"scope"})
        return {**data, "scope": _scope_brief(out.scope), "history": data["history"][:10]}

    @mcp.tool(annotations=READ, description=(
        "Kills by weapon across matches: for each weapon, who killed the most with it (scoreboard kills: no "
        "suicides, no team kills; bots left out). Weapons come most kills first; each player has his kills and "
        "headshots with the weapon, the share of his own kills made with it and the matches he played. Give "
        "`weapon` ('AK-47', 'awp', 'Desert Eagle') for one weapon; an unknown one lists the weapons with kills. "
        "No filter = every match."))
    @_threaded
    def get_weapon_rankings(weapon: Annotated[str | None, Field(description="One weapon, by display or game "
                                                                               "name. Default: all.")] = None,
                            night: Night = None, match_id: str | None = None, since: Since = None,
                            until: Until = None,
                            limit: Annotated[int, Field(ge=1, le=100, description="Players per weapon.")] = 5,
                            exclude: Exclude = None) -> dict[str, Any]:
        with session_scope() as s:
            out = insights.weapon_rankings(s, _scope(night, match_id, since, until, exclude), weapon, limit)
        return {"scope": _scope_brief(out.scope),
                "weapons": [{"weapon": w.weapon, "kills": w.kills, "headshot_pct": w.headshot_pct,
                             "total_players": w.total_players,
                             "players": [{"player": p.player.name, "steamid": p.player.steamid, "kills": p.kills,
                                          "headshots": p.headshots, "share_pct": p.share_pct, "matches": p.matches}
                                         for p in w.players]} for w in out.weapons]}

    @mcp.tool(annotations=READ, description=(
        "Trophies of one match (`match_id`) or one night (`night`; default: the most recent night): MVP, clutch "
        "king, entry fragger, longest kill streak, longest drought, kills on 10 HP or less, friendly flasher, "
        "friendly fire, best and worst aim, died with the knife out, baiter, economist, headshot machine. "
        "Each has titles in Portuguese and English, the winner, the value, the numbers behind it and the "
        "runners-up; a trophy nobody deserved is left out."))
    @_threaded
    def get_trophies(night: Night = None, match_id: str | None = None, exclude: Exclude = None) -> dict[str, Any]:
        with session_scope() as s:
            out = insights.trophies(s, _scope(night, match_id, None, None, exclude))
        return {"kind": out.kind, "night": out.scope.night, "scope": _scope_brief(out.scope),
                "trophies": [{"key": t.key, "title_pt": t.title_pt, "title_en": t.title_en, "player": t.player.name,
                              "steamid": t.player.steamid, "value": t.value, "detail": t.detail,
                              "detail_en": t.detail_en, "match_id": t.match_id, "round": t.round,
                              "runners_up": [f"{r.player.name} ({r.value:g})" for r in t.runners_up]}
                             for t in out.trophies]}

    @mcp.tool(annotations=READ, description=(
        "All-chat rankings for friends to tease each other (a word list, NOT moderation - it misses sarcasm). "
        "Podiums: toxic ('boca suja': swearing), whine ('chorão': cheating accusations, luck, complaining), "
        "death_cry (whining or swearing within 20 s of his own death - the real cry-baby number), talker "
        "('tagarela': who types the most lines, whatever they say; no examples), laugh ('risadinha'), "
        "nice ('gente boa': praise, gg), shouting (caps). Each podium entry has the count, the rate per round "
        "played and one or two example lines (slurs masked). `players` has every count per player."))
    @_threaded
    def get_chat_rankings(night: Night = None, match_id: str | None = None, since: Since = None,
                          until: Until = None,
                          limit: Annotated[int, Field(ge=1, le=100, description="Rows of `players`.")] = 15,
                          exclude: Exclude = None) -> dict[str, Any]:
        with session_scope() as s:
            out = insights.chat_rankings(s, _scope(night, match_id, since, until, exclude))
        fields = ("messages", "messages_per_round", "commands", "whine", "death_cries", "toxic", "laugh", "nice",
                  "shouting", "rounds")
        return {"scope": _scope_brief(out.scope), "messages": out.messages, "note": out.note,
                "podiums": {p.key: {"title_pt": p.title_pt, "title_en": p.title_en, "entries": [
                    {"player": e.player.name, "steamid": e.player.steamid, "count": e.count, "per_round": e.per_round,
                     "examples": [x.text + (f" (after dying to {x.about})" if x.about else "") for x in e.examples]}
                    for e in p.entries]} for p in out.podiums},
                "players": [{"player": p.player.name, "steamid": p.player.steamid,
                             **{f: getattr(p, f) for f in fields},
                             "cried_about": f"{p.cried_about.name} ({p.cried_about_count})" if p.cried_about else None}
                            for p in out.players[:limit]]}

    # ------------------------------------------------------------------ resources & prompts
    @mcp.resource("demoreel://matches/{match_id}/analysis", mime_type="application/json",
                  description="Complete analysis JSON of a match (large: every round, kill and player stat).")
    def analysis_resource(match_id: str) -> str:
        return _analysis(match_id).model_dump_json()

    @mcp.prompt(description="Turn a match into a polished highlight reel, step by step.")
    def make_highlight_reel(match_id: str, player: str = "") -> str:
        who = f" focused on the player {player!r}" if player else ""
        return (f"Create a highlight reel for demoreel match {match_id}{who}.\n"
                "1. get_match to understand the story of the game (score, MVPs, top highlights).\n"
                "2. list_highlights and pick the 4-6 best moments; prefer variety (clutch, multi-kill, flashy kill) "
                "and moments that mattered for the result.\n"
                "3. For each pick, get_highlight and tighten it with edit_highlight: cut dead time, keep ~3s before "
                "the first kill and ~2s after the last, split long gaps, add a short caption, consider slow motion "
                "on the best kill.\n"
                "4. render_reel with the chosen highlight_ids in chronological order and a good title.\n"
                "5. get_job until done and report the video_url with a one-paragraph summary of the reel.")

    return mcp


class _ApiKeyGate:
    """ASGI wrapper enforcing the API key on the mounted MCP endpoint."""

    def __init__(self, app, key: str):
        self.app, self.key = app, key

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            auth = headers.get("authorization", "")
            given = auth[7:] if auth.lower().startswith("bearer ") else headers.get("x-api-key", "")
            if given != self.key:
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b'{"error":"invalid or missing API key"}'})
                return
        await self.app(scope, receive, send)


def build_http_app():
    """Returns ``(server, asgi_app)``; the host app must run ``server.session_manager.run()`` in its lifespan."""
    settings = get_settings()
    hosts = {"localhost", "127.0.0.1", "[::1]", settings.host}
    if settings.public_url:
        hosts.add(urlparse(settings.public_url).hostname or "")
    hosts |= set(settings.mcp_allowed_hosts)
    allowed = sorted({h for h in hosts if h} | {f"{h}:*" for h in hosts if h})
    origins = sorted({f"{scheme}://{h}" for h in allowed for scheme in ("http", "https")})
    server = build_server()
    app = server.streamable_http_app(
        streamable_http_path="/", stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=settings.host not in {"0.0.0.0", "::"} or bool(settings.mcp_allowed_hosts),
            allowed_hosts=allowed, allowed_origins=origins))
    gate = settings.admin_key or settings.api_key  # MCP has write tools: the organiser's key guards it
    return server, (_ApiKeyGate(app, gate) if gate else app)
