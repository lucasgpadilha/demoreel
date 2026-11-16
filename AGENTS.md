# Notes for coding agents working on this repository

(Using demoreel *from* an agent — MCP tools, REST — is documented in [docs/agents.md](docs/agents.md).)

## Commands

```bash
uv sync                          # install
uv run pytest -q                 # all tests; real-demo tests skip unless demos/*.dem exist
uv run pytest -k ingame -q       # in-game renderer unit tests (never launch the game)
uv run ruff check src tests      # lint (line length 110)
uv run demoreel serve            # API + UI + MCP + worker on :8000
uv run demoreel analyze demos/x.dem
uv run demoreel publish --dry-run  # what would go to the read-only mirror (docs/hosting.md)
```

## Rules of the road

- Behaviour lives in `src/demoreel/service.py`; `api.py`, `mcp_server.py` and `cli.py` are thin wrappers.
  Add a capability to the service first, then expose it in both the REST API and the MCP server.
- `src/demoreel/analysis/schema.py` and `src/demoreel/schemas.py` are public contracts. Additive changes only.
- Stat definitions are documented in `docs/metrics.md`; change code, doc and a synthetic-demo test together.
  The numbers are validated against the scoreboard stored in real demos (kills, deaths, assists, damage,
  headshots must match exactly) — keep it that way.
- Never commit `.dem` files, `data/`, radar images, the CS2 playback plugin binary, or anything from a
  brand kit (client logos, music). Kits and match sources live in `data/` and are documented in
  `docs/branding.md`.
- Capture and composition are separate on purpose (`render/` vs `compose/`). Anything that changes how a
  video *looks* (HUD, captions, bumpers, speed, music) belongs in `compose/` and must work from a stored
  master; only what the game has to show differently (ticks, point of view) may require a new capture.
- UI copy is Brazilian Portuguese first with English as second locale (`web/js/locales/`). Follow
  `docs/design/frontend-design-plan.md`; do not reintroduce card grids, caps eyebrows or dot-joined metadata
  in the built-in theme. A brand kit may opt into its community's own conventions (uppercase display type,
  bordered panels, separators…) through its `ui` block: the UI is meant to be embedded in that community's
  site and must look like it. Nothing client-specific goes into `src/`.
- The UI has two audiences. Visitors only read; everything that changes data is an organiser action, hidden
  unless `GET /api/v1/session` says `admin`, and refused by the API without `DEMOREEL_ADMIN_KEY` when one is
  set. Keep new write endpoints behind the router's dependency and new buttons behind the session check.
- Do not launch CS2, Steam or HLAE, and do not write inside a Steam/CS2 install, from tests or scripts.
  The in-game renderer is tested through injected fakes (`tests/test_ingame_*.py`).
- Windows and Linux both matter. Use `pathlib`, avoid shelling out to platform tools without a guard, and
  remember Windows consoles are not UTF-8 by default (player names contain arbitrary Unicode).
- demoparser2 quirks worth knowing: `parse_event` returns a list (not a DataFrame) for missing events;
  synthesized events such as `round_end` do not appear in `list_game_events()`; SteamIDs must be handled
  as strings; bots have no SteamID (`BOT:<name>` ids are used); some demos lack `player_blind` events.
- Error messages are read by end users and by LLM agents: state what was wrong and what is valid.
- Only permissively licensed code (MIT/BSD/Apache-2.0), with attribution. cs2-highlights-maker (no licence),
  CS2-insight-agent (non-commercial) and cs-demodesk (AGPL) are references for ideas only — no code.
