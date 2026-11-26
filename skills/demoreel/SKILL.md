---
name: demoreel
description: Analyze Counter-Strike 2 (CS2) demos with demoreel and turn them into match stats, highlight clips and videos. Use when the user has a CS2 .dem file or a demoreel server and asks for a scoreboard, player or round stats (K/D, ADR, KAST, rating, clutches, opening duels), rivalries, trophies or weapon rankings across matches, the best plays or the fails of a match, a hand-made clip of one moment, or rendered highlight videos and reels (first-person in-game footage or a 2D radar replay).
license: MIT
compatibility: Needs a running demoreel server (github.com/lucasgpadilha/demoreel, installed with uv; Windows or Linux). First-person video recording also needs Counter-Strike 2 through Steam and a GPU on the same machine; HLAE is optional.
---

# demoreel

[demoreel](https://github.com/lucasgpadilha/demoreel) (MIT, self-hosted) parses CS2 demos into scoreboard-grade
stats, finds the plays worth watching and renders them to mp4. You drive it through its MCP tools (preferred)
or its REST API. Everything slow is a job you wait on.

## 1. Is it there?

- If tools such as `list_matches` and `get_match` are available, you are connected: call `get_capabilities()`
  to see which renderers this server has, then go to section 3.
- Otherwise check for a server: `curl -s http://127.0.0.1:8000/health`. If nothing answers, the user has to
  install and start it. Ask before installing anything on their machine.

| Needed for | What |
|---|---|
| Everything | [uv](https://docs.astral.sh/uv/getting-started/installation/) (brings Python 3.11+) and demoreel. ffmpeg is bundled. Windows or Linux. |
| 2D radar videos | Nothing more (CPU only, works in a container). |
| First-person (`ingame`) videos | A PC with Counter-Strike 2 installed through Steam, Steam running and logged in, a GPU that runs CS2, the CS Demo Manager playback plugin binary copied by the user, and `DEMOREEL_INGAME_ENABLED=1`. Setup: [docs/ingame-recording.md](https://github.com/lucasgpadilha/demoreel/blob/main/docs/ingame-recording.md). |
| Faster in-game recording (optional) | [HLAE](https://github.com/advancedfx/advancedfx/releases) on Windows, extracted to `<data_dir>/cache/hlae/<version>/`. |

```bash
git clone https://github.com/lucasgpadilha/demoreel && cd demoreel
uv sync
uv run demoreel doctor     # what this machine can do, and exactly what is missing
uv run demoreel serve      # API + web UI + MCP at /mcp + one job worker, on http://127.0.0.1:8000
```

Never start CS2, Steam or HLAE yourself and never write inside the CS2 folder: demoreel launches the game
for a recording and restores everything it touched afterwards.

## 2. Connect

- **MCP over HTTP** (server running): `claude mcp add --transport http demoreel http://127.0.0.1:8000/mcp`.
  Other clients take the same URL. When the server sets a key, send `Authorization: Bearer <key>`: the
  `DEMOREEL_ADMIN_KEY` when there is one, else the `DEMOREEL_API_KEY`.
- **MCP over stdio**: `claude mcp add demoreel -- uv run --directory /path/to/demoreel demoreel mcp`. It uses
  the same database as `demoreel serve` when started from the clone, but it runs no jobs: keep `demoreel serve`
  (or `demoreel worker`) running or analyses and renders stay queued forever.
- **REST**: base `http://127.0.0.1:8000/api/v1`. Read `/llms.txt` first; the contract is `/openapi.json`. The
  calls for each flow below are in [references/rest-api.md](references/rest-api.md).

## 3. Flows

Players are named by SteamID or by (part of) their in-game name; an ambiguous name returns the candidates.
Ticks are demo time: seconds = ticks / 64.

**Register a demo.** A file in one of the server's `DEMOREEL_IMPORT_DIRS`: `import_demo(path)` returns
`match_id` and `job_id`. Any other file: upload it over REST (`POST /api/v1/matches`, multipart field `file`).
From the community's match site: `list_source_matches()` then `import_matches(source_id, remote_ids)`.

**Wait for the analysis.** `get_job(job_id, wait_seconds=60)` blocks until the job finishes (about 10 s for a
40-minute match); repeat while `done` is false. A `failed` job's `error` says what to fix. Do not poll in a loop.

**Read the match.** `get_match(match_id)` gives the scoreboard (K/D/A, ADR, KAST, rating per player), the top
highlights and warnings. Then `get_player_stats(match_id, player)`, `list_rounds(match_id)`,
`get_round(match_id, round_number)` and `search_kills(match_id, attacker=..., weapon=..., headshot=...)`.
Across matches (filter with `night=YYYY-MM-DD` or `match_id`; no filter = every match): `list_nights()`,
`get_rivalries()`, `get_player_profile(player)`, `get_weapon_rankings(weapon="AWP")`, `get_trophies()`,
`get_chat_rankings()`. What each stat means: [docs/metrics.md](https://github.com/lucasgpadilha/demoreel/blob/main/docs/metrics.md).

**List highlights.** `list_highlights(match_id, player=..., featured=..., category="play")` returns plays best
first with `score` (0-100), `tier` (S/A/B/C) and `video_url` when rendered. `category="fail"` lists the fails
(whiffs, team flashes, team kills: banter material), `"all"` both. `featured` plays are the ones that get
first-person videos; `set_featured(highlight_id, featured=true)` promotes one. Read `get_highlight(highlight_id)`
(segments, kill ticks, renders) before changing a clip.

**Edit a clip.** `edit_highlight(highlight_id, ops)` applies a list of operations, all or nothing: `trim`,
`set_range`, `set_pov`, `set_camera`, `set_speed` (0.5 = slow motion), `set_caption`, `add_segment`,
`remove_segment`, `move_segment`, `split_segment`, `set_meta`. Slow motion on a final kill: split the segment
just before it and set the second part to 0.3-0.5. `set_pov` puts a segment on a player (or the free camera)
and `set_camera` on a fixed spot; each replaces the other, and `add_segment` takes either. Videos already made
are kept and flagged `stale`; render again.

**Make a clip by hand.** `create_highlight(match_id, ...)` takes explicit `segments`, or `start_tick` +
`end_tick` (+ `pov`), or `round_number` + `player` to clip that player's kills in that round.
- `category="fail"` files it with the match's fails (banter) instead of its plays;
  `set_category(highlight_id, "play")` moves a hand-made clip back (and `"fail"` the other way).
- A segment can be filmed from a fixed spot instead of a player: give it `camera` = `{x, y, z, pitch, yaw}`
  and no `pov_steamid`. Get the numbers in CS2: watch the demo, fly the free camera to the spot, type
  `spec_pos` in the console; it prints `spec_goto x y z pitch yaw`. Fixed cameras are filmed by the in-game
  renderer only; a 2D replay shows the whole map during those segments and says so in the job's `warnings`.

```json
{"match_id": "m_…", "title": "The plant, seen from the A site balcony", "category": "play",
 "segments": [
   {"start_tick": 51200, "end_tick": 51650, "camera": {"x": -1200.5, "y": 640.0, "z": 128.0, "pitch": 12.0, "yaw": 90.0}},
   {"start_tick": 51650, "end_tick": 52100, "pov_steamid": "76561190000000001"}]}
```

**Render.** `render_highlight(highlight_id, renderer="auto", wait_seconds=90)`; `renderer` is `auto`
(first-person when this server can, else 2D), `ingame` or `radar`. Several clips in one video:
`render_reel(match_id, highlight_ids=[...])` or `render_reel(match_id, top=5, player=...)`. Every video of a
match in 16:9 and 9:16 in one call: `produce_match(match_id)`. All return a `job_id`.

**Check jobs.** `get_job(job_id, wait_seconds=30)` until `done`, then read `video_url`; `get_render(render_id)`
tells whether a video is `stale`; `cancel_job(job_id)` stops one. When first-person videos stay queued,
`list_workers()` shows whether the recording machine is online or busy.

Limits: at most 40 segments and 300 s per highlight, each segment at least 0.25 s and inside the demo.

## 4. Gotchas

- **Nothing happens to jobs.** No worker is running (a stdio MCP server alone runs none). Ask the user to keep
  `uv run demoreel serve` open.
- **Recording refused because CS2 is open.** demoreel never closes a game it did not start. Ask the user to
  quit CS2 (Steam stays open and logged in), then render again.
- **A recording waits.** One game session at a time per machine; other recording jobs keep their place in the
  queue and start after it. Do not cancel and resubmit them.
- **A CS2 update broke recording.** Game updates break the playback plugin and HLAE until their authors ship
  new builds: "CS2 wrote no frames", "No csdm.log was written", an `AfxHookSource2` error box, or a session
  stuck at "Waiting for CS2 to load the demo". The fix is a newer plugin binary or HLAE release (ask before
  downloading); meanwhile `DEMOREEL_INGAME_BACKEND=startmovie` avoids HLAE, and `renderer="radar"` always works.
  Old demos may not play on a newer CS2 at all; the render result then carries a warning.
- **Edits do not need the game.** First-person footage is kept as a master: trims, slow motion, captions and
  re-branding re-render in seconds, even when the game is unavailable. Only new ticks, another point of view or
  a new fixed camera need a new recording.
- **Nobody touches the game window while it records** (it must stay on screen, not minimized).
- **Bots** have no SteamID (`BOT:<name>` ids) and cannot be followed in first person.
- **Write tools refused.** When the server sets `DEMOREEL_ADMIN_KEY`, visitors can only read; creating,
  editing, importing and rendering need that key.
- **Errors are written to be acted on** (what was wrong, what is valid): fix the argument and retry.
- Demos carry player names and SteamIDs: do not send them to a server the user does not control.

More: [docs/agents.md](https://github.com/lucasgpadilha/demoreel/blob/main/docs/agents.md) (tools, edit
operations, an example session) and the [README](https://github.com/lucasgpadilha/demoreel#readme).
