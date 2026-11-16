# Architecture

## Goals that shaped the design

1. **One install, zero infrastructure** for a single PC — and the same code hostable on a Linux server.
2. **Agents are first-class users.** Anything a human can do in the UI, an LLM can do through tools.
3. **Capture and editing are separate.** A highlight is data (what to show); renderers and — soon — the
   composition stage decide how it looks. This is what makes automatic editing possible.
4. **The game is optional.** Analysis and 2D video never need CS2; first-person video is an add-on capability
   of some workers.

## Processes

```text
demoreel serve
 ├─ API process (uvicorn / FastAPI)
 │    REST /api/v1 · MCP /mcp · web UI / · files /files/renders/*
 └─ N worker processes (spawned; or run `demoreel worker` anywhere that shares the DB + data dir)
      capabilities: cpu  → analyze_match, render (radar)
                    ingame → render (ingame)        [only with DEMOREEL_INGAME_ENABLED=1 or `-c ingame`]
```

All state is in two places: the **database** (SQLite by default, WAL mode, shared by API and workers) and
the **data directory**:

```text
data/
  demoreel.db
  demos/<match_id>.dem              the uploaded demo (needed for every render)
  matches/<match_id>/analysis.json  full MatchAnalysis document
  renders/<render_id>.mp4|.jpg      videos and thumbnails (the only files served over HTTP)
  cache/radars/*.png                radar images downloaded on first 2D render
  tmp/                              upload spool and per-render scratch dirs
```

## Data model

| Table | What | Notes |
|---|---|---|
| `matches` | one uploaded demo | unique by SHA-256 of the decompressed demo; summary columns for listing |
| `highlights` | editable clip spec | `segments` JSON, `origin` (auto / manual / agent), `version` bumped on every edit |
| `renders` | one video | `highlight_ids` (1 = highlight, many = reel), renderer, options, `highlight_versions` snapshot → `stale` flag |
| `jobs` | queue | `type`, `capability`, `status`, `progress`, heartbeat, attempts |
| `webhooks` | subscriptions | events filter, optional HMAC secret |

The full analysis is a JSON file rather than tables: it is written once, read whole, and versioned by
`schema_version` (see `analysis/schema.py`). Re-analysis replaces `auto` highlights and keeps `manual` /
`agent` ones.

## The highlight spec

```json
{
  "segments": [
    {"start_tick": 74201, "end_tick": 74560, "pov_steamid": "7656…", "speed": 1.0, "caption": null},
    {"start_tick": 74560, "end_tick": 74782, "pov_steamid": "7656…", "speed": 0.5, "caption": "1v2"}
  ]
}
```

Edits are small validated operations (`trim`, `split_segment`, `set_speed`, `set_pov`, …) applied atomically
in `service.edit_highlight`. The same function backs `POST /highlights/{id}/edit` and the MCP tool
`edit_highlight`, so UI, scripts and agents cannot drift apart. This spec is the extension point for the
editing roadmap (templates, bumpers, music, transitions): new fields, same operations model.

## Job queue

A table plus one atomic statement:

```sql
UPDATE jobs SET status='running', worker_id=?, … WHERE id=? AND status='queued'   -- rowcount = 1 wins
```

Chosen over Celery/arq/RQ because those need Redis or RabbitMQ and are awkward on Windows. Workers heartbeat
through progress updates; jobs whose worker vanished are re-queued (max 2 attempts). Cancellation is
cooperative (`cancel_requested`, checked by renderers).

Clients never have to poll tightly: `GET /jobs/{id}?wait=N` and the MCP `get_job(wait_seconds=N)` block until
the job finishes; webhooks push `match.ready`, `render.ready`, etc.

## Renderers

`render/base.py` defines the contract: `availability()` and `render(RenderJob) -> RenderResult`. A
`RenderJob` carries the demo path, the analysis, the clips (segments), output paths, options, and callbacks
for progress and cancellation.

- **radar** (`render/radar/`): loads per-tick player state for each segment directly from the demo
  (`demoparser2.parse_ticks`), plus smokes, fires, flashes, shots and bomb events; draws frames with skia
  and pipes raw frames into ffmpeg (libx264). No temp image files.
- **ingame** (`render/ingame/`): launches CS2 on the demo with a playback plugin that executes commands at
  ticks (seek, spectate, `startmovie`), then encodes the captured frames. See `docs/ingame-recording.md`.

Adding a renderer = implementing that protocol and registering it in `render/__init__.py` with the worker
capability it needs.

## Security model (v0.1)

- Optional single API key for `/api` and `/mcp`. Without it the service is meant for localhost.
- `/files` serves only `data/renders/`; with an API key, links are HMAC-signed and expire after 24 h.
- Uploads are validated by magic bytes (CS2 `PBDEMS2`), size-limited, stored under generated names.
- Server-side import (`import_demo`) is off unless `DEMOREEL_IMPORT_DIRS` whitelists folders.
- MCP HTTP endpoint keeps DNS-rebinding protection on for loopback binds.

Not there yet: users/organisations, rate limiting, migrations — see the roadmap.
