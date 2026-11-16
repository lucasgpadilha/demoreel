# Contributing

Thanks for wanting to help. The project is young; issues, demos that break the parser, and small focused
pull requests are all very welcome.

## Setup

```bash
uv sync                      # creates .venv with runtime + dev dependencies
uv run demoreel serve        # API + UI + worker on http://127.0.0.1:8000
uv run pytest                # fast; no demo needed
uv run ruff check src tests
```

Put a few `.dem` files in `demos/` (git-ignored) to also run the tests marked `demo`, which exercise the
whole pipeline on real data. **Never commit demos** — they are large and contain player names and SteamIDs.

## Layout

```text
src/demoreel/
  analysis/     parser wrapper, stats, highlight detection, public schema
  render/       Renderer protocol, ffmpeg helpers, map calibration
    radar/      2D tactical replay renderer
    ingame/     CS2 client recorder
  service.py    business logic used by every front-end
  api.py        REST API          mcp_server.py   MCP tools
  jobs.py       queue + workers   tasks.py        job handlers
  web/          static UI (no build step)
tests/          synthetic-demo unit tests + real-demo tests (auto-skipped)
docs/           architecture, metrics, agents, in-game recording
```

## Guidelines

- **Put behaviour in `service.py`**, then expose it from the API and the MCP server. The front-ends stay thin
  and identical in behaviour.
- **Stat definitions are a contract.** If you change one, update `docs/metrics.md` and add a test with a
  synthetic demo (`tests/conftest.py` has helpers: `make_demo`, `kill`).
- **Schema changes**: `analysis/schema.py` is the public data contract — additive changes only, or bump
  `SCHEMA_VERSION`. Database model changes need a note in the PR until migrations land.
- **Error messages are UI** — for humans and for LLM agents. Say what was wrong and what would be valid.
- **Renderers** must honour `job.cancelled()`, report `job.progress()`, clean up after themselves, and raise
  `RenderError` with a message a user can act on.
- **Licensing**: only add code you wrote or that is under a permissive licence (MIT/BSD/Apache-2.0), with
  attribution. No game assets in the repository (radar images are downloaded at runtime).
- Keep it runnable on **both Windows and Linux**; avoid dependencies that need a broker or a compiler.

## Reporting a demo that fails

Open an issue with: where the demo comes from (Premier, FACEIT, a community server with MatchZy…), the CS2
build if you know it, the error or the wrong number, and — if you can share it — a link to the demo.
