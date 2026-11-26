# demoreel

**Upload a CS2 demo. Get the match metrics and the highlight videos back — automatically.**

demoreel is a self-hosted service that ingests Counter-Strike 2 demos (`.dem`), computes scoreboard-grade
statistics, detects the moments worth watching, and renders them to video. It is built to be driven by
software as much as by people: a REST API, webhooks, and an **MCP server** so LLM agents can query a match
and *edit* its highlights.

```text
            ┌────────────┐   analyze    ┌──────────────────────┐
 .dem ────▶ │  REST API  │ ───────────▶ │ stats · rounds · kills│ ──▶ JSON (API / MCP / web UI)
 upload     │  MCP · UI  │   job queue  │ highlight candidates  │
            └────────────┘              └──────────┬───────────┘
                  ▲                                │ highlight = editable spec
                  │ edit ops (trim, POV, speed…)   ▼ (segments: ticks, POV, speed, caption)
             humans / agents            ┌──────────────────────┐
                                        │ renderers            │ ──▶ mp4 + thumbnail
                                        │  ingame (CS2 POV)    │
                                        │  radar  (2D replay)  │
                                        └──────────────────────┘
```

> **Status: early (v0.1).** Analysis, highlight detection, the API, MCP server, web UI and the 2D renderer
> work end to end on real demos. The in-game (first-person) renderer has produced correct clips on Windows
> with the current CS2 build (single and multi-take, from the CLI and through the server), on one machine so far — see what is and is not
> verified in [docs/ingame-recording.md](docs/ingame-recording.md), and the [roadmap](ROADMAP.md).

## What you get

- **Match metrics** — score by half and side, per-round economy and outcome, and per player: K/D/A, ADR, KAST,
  HS%, rating (HLTV 2.0 approximation), opening duels, trades, multi-kills, clutches (1vX), utility damage,
  flashes, accuracy, weapon breakdown, CT/T split.
- **Automatic highlights** — aces and multi-kills, won clutches, flashy single kills (noscope, through smoke,
  wallbang, blind, airborne, knife, zeus), ninja defuses; each scored and described, with tight clip
  boundaries (kills far apart become separate segments).
- **Highlight videos** from two renderers:
  - `ingame` — the real game, from the player's point of view (needs a PC with CS2);
  - `radar` — a 2D tactical replay with rosters, kill feed, utility and bomb, which runs anywhere
    (no game, GPU or display — fine in a container).
- **Finished, branded videos** — give the instance a brand kit (logo + colours) and every video gets an
  animated intro and outro *generated from the logo* (with synthesised sound), a HUD in the community's
  identity (score, nameplate, kill counter), captions and real slow motion. White-label: no third-party
  watermark. See [docs/branding.md](docs/branding.md).
- **Ready to post** — every play in 16:9 and in **9:16 laid out for phones** (and 1:1), cut from the same
  recorded footage; one call produces a whole match.
- **Import from your community's site** — point a small JSON file at a site that lists matches and demo
  links, and import a night's games in one click (or one MCP call). Imports are throttled and stop at the
  first sign that the site is overloaded.
- **Edits without the game** — first-person captures are kept as masters with handles, so trimming, slowing
  down, captioning or re-branding a clip re-renders in seconds without launching CS2 again.
- **Editable highlights** — a highlight is a small declarative spec. Trim it, change POV, slow it down, add a
  caption, split or reorder segments through validated operations, then re-render. Reels stitch several
  highlights into one video.
- **Agent-friendly by design** — MCP tools with compact outputs, job handles you can block on instead of
  polling, actionable error messages, `/llms.txt`, a complete OpenAPI contract.
- **Async everything** — uploads and renders return a job; long-poll it or subscribe with webhooks.
- **One install, no infrastructure** — SQLite and a DB-backed queue by default (no Redis). Windows and Linux.

## Quick start

Requirements: Windows or Linux (macOS is not supported), [git](https://git-scm.com/downloads) and
[uv](https://docs.astral.sh/uv/). uv downloads its own Python and the dependencies, so the first run needs
internet. ffmpeg is bundled. On Linux the video drawing (skia) also needs three system libraries, which minimal
servers and WSL lack (without them `uv sync` works and the first video fails):

```bash
sudo apt-get install -y libgl1 libegl1 libfontconfig1   # Debian / Ubuntu
```

```bash
git clone https://github.com/lucasgpadilha/demoreel && cd demoreel
uv sync
uv run demoreel serve
```

Open <http://127.0.0.1:8000>, drop a demo, watch the highlights. API docs live at `/docs`.

- Demos: CS2 `.dem`, also compressed as `.gz`, `.bz2` or `.zip`; `.zst` only when the server runs Python 3.14+
  (the clone pins 3.13). CS:GO demos are refused.
- The first 2D video downloads the map images ([Renderers](#renderers)); afterwards nothing is downloaded.
- First-person recording also needs CS2 through Steam, a GPU and the CS Demo Manager playback plugin, a
  third-party binary (MIT) that demoreel does not ship and that you download yourself:
  [docs/ingame-recording.md](docs/ingame-recording.md).
- In Windows PowerShell, type `curl.exe` for the `curl` examples below (`curl` is another command there).
- `GET /health` answers with the running `version`.

No server, just the terminal:

```bash
uv run demoreel analyze path/to/match.dem                 # scoreboard + highlights
uv run demoreel render  path/to/match.dem -o reel.mp4     # top highlights as one video
uv run demoreel doctor                                    # what this machine can do
```

Docker (API + worker + 2D renderer): `docker compose up --build`.

## Using the API

```bash
# 1. upload → 202 with a match and a job
curl -F "file=@match.dem" http://127.0.0.1:8000/api/v1/matches

# 2. wait for the analysis (long-poll, no busy loop)
curl "http://127.0.0.1:8000/api/v1/jobs/<job_id>?wait=60"

# 3. read
curl http://127.0.0.1:8000/api/v1/matches/<match_id>/scoreboard
curl http://127.0.0.1:8000/api/v1/matches/<match_id>/highlights

# 4. edit a highlight: start 1s later, slow-mo on the final second, add a caption
curl -X POST http://127.0.0.1:8000/api/v1/highlights/<id>/edit -H "content-type: application/json" -d '{
  "ops": [
    {"op": "trim", "segment": 0, "start_delta_s": 1.0},
    {"op": "split_segment", "segment": 0, "at_tick": 74560},
    {"op": "set_speed", "segment": 1, "speed": 0.5},
    {"op": "set_caption", "segment": 1, "caption": "1v2 on the pistol round"}
  ]}'

# 5. render → 202 with a job; when done, the render has a video_url
curl -X POST http://127.0.0.1:8000/api/v1/highlights/<id>/renders -H "content-type: application/json" \
     -d '{"renderer": "radar"}'
```

Everything else (custom highlights from any tick range, reels, kill search, the plays feed, match sources,
the brand, webhooks, capabilities) is in
the interactive docs at `/docs` and summarised for machines at `/llms.txt`.

## Using it from an LLM agent (MCP)

The API process serves MCP over Streamable HTTP at **`/mcp`**; `demoreel mcp` speaks stdio for local clients.

```bash
# Claude Code, remote/HTTP
claude mcp add --transport http demoreel http://127.0.0.1:8000/mcp

# Claude Code / Claude Desktop, local stdio (run from the repo so it finds the same data dir)
claude mcp add demoreel -- uv run --directory /path/to/demoreel demoreel mcp
```

Tools: `list_matches`, `get_match`, `get_player_stats`, `list_rounds`, `get_round`, `search_kills`,
`list_highlights`, `get_highlight`, `set_featured`, `rescore_match`, `create_highlight`, `set_category`,
`edit_highlight`, `delete_highlight`, `render_highlight`, `render_reel`, `get_job`, `get_render`, `cancel_job`,
`get_feed`, `list_source_matches`, `import_matches`, `import_demo`, `produce_match`, `get_brand`,
`list_workers`, `get_capabilities`,
`list_nights`, `get_rivalries`, `get_player_profile`, `get_weapon_rankings`, `get_trophies`,
`get_chat_rankings` (insights across matches: who owns whom, who kills the most with each weapon, each night's
trophies, the cry-baby ranking of the chat — see
[docs/metrics.md](docs/metrics.md#across-matches-rivalries-weapon-rankings-trophies-chat-rankings))
— plus a `make_highlight_reel` prompt and the full analysis as a resource. Then just ask:

> *"Look at the last match, find biruta's best moment, cut it down to the three kills, slow down the last
> one and give me the video."*

More in [docs/agents.md](docs/agents.md).

### Agent skill

[`skills/demoreel/SKILL.md`](skills/demoreel/SKILL.md) teaches an agent when and how to use all of this:
what to install, how to connect, the main flows and the pitfalls of in-game recording. In Claude Code, the
repository is also a plugin marketplace that installs the skill and the MCP connection to a local
`demoreel serve`:

```text
/plugin marketplace add lucasgpadilha/demoreel
/plugin install demoreel@demoreel
```

Other agents: copy `skills/demoreel/` into their skills folder, or build the zip that skill marketplaces take
with `uv run python scripts/build_skill_zip.py` (→ `dist/demoreel-skill.zip`). Details in
[docs/agents.md](docs/agents.md#agent-skill-and-claude-code-plugin).

## Renderers

| | `ingame` | `radar` |
|---|---|---|
| Output | Real CS2 footage, player POV | 2D top-down tactical replay |
| Needs | Windows/Linux PC with CS2, GPU, desktop session | Nothing (CPU only) |
| Speed | Slow (launches the game) | ~1× real time on a desktop CPU |
| Enable | `DEMOREEL_INGAME_ENABLED=1` + setup in [docs/ingame-recording.md](docs/ingame-recording.md) | Always on |

`renderer: "auto"` (the default) uses `ingame` when the server can, otherwise `radar`. Workers advertise
capabilities, so a Linux server can analyze and render 2D while a gaming PC runs
`demoreel worker -c ingame` against the same database and records the first-person clips.

Radar images are Valve assets and are **not** in this repository; they are downloaded on first use from the
[awpy-data](https://github.com/pnxenopoulos/awpy-data) releases into the data directory
(`DEMOREEL_OFFLINE=1` disables that and renders on a plain grid).

## Configuration

Environment variables (or a `.env` file) — see [.env.example](.env.example). The ones you will touch first:
`DEMOREEL_DATA_DIR`, `DEMOREEL_PUBLIC_URL`, `DEMOREEL_API_KEY`, `DEMOREEL_AUTO_RENDER_TOP`,
`DEMOREEL_DEFAULT_RENDERER`, `DEMOREEL_INGAME_ENABLED`.

**Security note:** with no `DEMOREEL_API_KEY` the API is open — fine on `127.0.0.1`, not on a network.
Demos contain player names and SteamIDs; treat the data directory accordingly.

## How it works

- **Parsing** — [demoparser2](https://github.com/LaihoE/demoparser) (Rust) behind a defensive wrapper that
  handles warm-up, knife rounds and restarts, side swaps, missing events and bots
  ([`analysis/parser.py`](src/demoreel/analysis/parser.py)). A 40-minute match analyzes in ~10 s.
- **Stats** — [`analysis/stats.py`](src/demoreel/analysis/stats.py); definitions are documented in the code
  and in [docs/metrics.md](docs/metrics.md).
- **Highlights** — [`analysis/highlights.py`](src/demoreel/analysis/highlights.py): one candidate per
  (round, player), scored by kills, speed, flashiness, clutch size and round context.
- **Jobs** — a table and an atomic claim query ([`jobs.py`](src/demoreel/jobs.py)); worker processes with
  capabilities, heartbeats, re-queue on worker loss.
- **Rendering** — a tiny [`Renderer` protocol](src/demoreel/render/base.py); `radar` draws with skia and
  pipes frames to ffmpeg; `ingame` drives CS2 following cs-demo-manager's approach.
- **Front-ends** — FastAPI ([`api.py`](src/demoreel/api.py)), MCP ([`mcp_server.py`](src/demoreel/mcp_server.py)),
  Typer CLI, and a no-build static web UI — all thin layers over [`service.py`](src/demoreel/service.py).

- **Composition** — [`compose/`](src/demoreel/compose): logo splitting, procedural bumpers and sound, HUD,
  retiming, masters, final assembly. Capture answers "what did the game look like"; composition answers
  "what does the video look like".

Architecture notes: [docs/architecture.md](docs/architecture.md) · brand kits and composition:
[docs/branding.md](docs/branding.md) · speed and scale: [docs/performance.md](docs/performance.md) · UI design
rationale: [docs/design/frontend-design-plan.md](docs/design/frontend-design-plan.md).

## Development

```bash
uv sync
uv run pytest            # tests needing a real demo run only if you put .dem files in demos/
uv run ruff check src tests
```

See [CONTRIBUTING.md](CONTRIBUTING.md). The plan — in particular in-game recording hardening and **automatic
editing** (intro/outro bumpers, per-client/map/situation templates, music, transitions, speed ramps) — is in
[ROADMAP.md](ROADMAP.md).

## Acknowledgements

- [demoparser2](https://github.com/LaihoE/demoparser) (MIT) — the demo parser.
- [cs-demo-manager](https://github.com/akiver/cs-demo-manager) (MIT) by AkiVer — the in-game recording flow
  (playback plugin, actions file, `startmovie` pipeline) is ported from it.
- [awpy / awpy-data](https://github.com/pnxenopoulos/awpy-data) (MIT) — map calibration and radar extraction.
- [Barlow](https://github.com/jpt/barlow) by Jeremy Tribby (SIL OFL 1.1) — the typeface bundled for videos and UI.
- [reka-ai/cs2-dem-renderer](https://github.com/reka-ai/cs2-dem-renderer) (MIT) — evidence that this recording
  technique scales on Linux, and the streaming-encode design the performance plan follows.
- Ideas on highlight scoring and recording pitfalls were informed by reading
  [cs2-highlights-maker](https://github.com/Rovniy/cs2-highlights-maker),
  [CS2-insight-agent](https://github.com/DrEAmSs59/CS2-insight-agent) and
  [cs-demodesk](https://github.com/noih/cs-demodesk); no code from those projects is included.

demoreel is not affiliated with Valve. Counter-Strike is a trademark of Valve Corporation.

## License

[MIT](LICENSE). Notes on what it is used with:

- **demoparser2** is MIT-licensed in [its repository](https://github.com/LaihoE/demoparser); its PyPI package
  carries no licence metadata, so licence scanners report it as unknown.
- The optional `postgres` extra installs **psycopg**, which is LGPL-3.0. demoreel only imports it as a library
  (nothing of it is copied into this repository); SQLite, the default, needs nothing of the kind.
- **ffmpeg binaries are GPL.** The ffmpeg bundled by `imageio-ffmpeg` (BSD) is built with x264, and the Docker
  image installs Debian's ffmpeg; both are GPL. That does not change the licence of demoreel's code, but whoever
  *distributes* a Docker image (or another bundle) of demoreel distributes GPL binaries and must follow the GPL
  for them.
- None of these is shipped here: radar images (Valve assets) are downloaded on first use, the CS Demo Manager
  plugin binary and HLAE by the user.
