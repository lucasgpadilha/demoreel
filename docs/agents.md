# Driving demoreel from LLM agents

demoreel exposes the same capabilities three ways; pick whichever your agent stack prefers.

| Interface | When | Where |
|---|---|---|
| **MCP, Streamable HTTP** | Agent runs elsewhere (hosted assistants, connectors, other machines) | `http://<host>:<port>/mcp` |
| **MCP, stdio** | Local desktop clients (Claude Desktop, Claude Code, IDEs) | `demoreel mcp` |
| **REST + OpenAPI** | Your own tool definitions / function calling | `/openapi.json`, `/llms.txt` |

## Connecting

```bash
# Claude Code — HTTP
claude mcp add --transport http demoreel http://127.0.0.1:8000/mcp

# Claude Code — stdio (same data directory as your server: run from the repo, or set DEMOREEL_DATA_DIR)
claude mcp add demoreel -- uv run --directory /path/to/demoreel demoreel mcp
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "demoreel": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/demoreel", "demoreel", "mcp"],
      "env": { "DEMOREEL_IMPORT_DIRS": "[\"/path/to/your/demos\"]" }
    }
  }
}
```

With `DEMOREEL_API_KEY` set, HTTP clients must send `Authorization: Bearer <key>`.

The stdio server talks to the same database as `demoreel serve`, but it does not run jobs itself: keep a
server or `demoreel worker` running so analyses and renders get processed.

## Tools

| Tool | Purpose |
|---|---|
| `list_matches`, `get_match` | Find a match; overview with scoreboard and top highlights |
| `get_player_stats`, `list_rounds`, `get_round`, `search_kills` | Drill into the data |
| `list_highlights`, `get_highlight` | Browse clips; read segments before editing |
| `create_highlight` | New clip from segments, a tick range, or round + player |
| `edit_highlight` | Atomic list of edit operations |
| `render_highlight`, `render_reel` | Produce video (async, optional blocking wait) |
| `get_job`, `get_render`, `cancel_job` | Follow long-running work |
| `import_demo` | Analyze a demo already on the server's disk (whitelisted folders only) |
| `get_capabilities` | Which renderers this server has |
| `list_nights`, `get_rivalries`, `get_player_profile`, `get_trophies`, `get_chat_rankings` | Across matches: nights of play, who kills whom (nemesis / favourite victim), one player over the season, trophies of a match or a night, chat rankings for banter. All take `night=YYYY-MM-DD` or `match_id`; no filter = every match |

Also: prompt `make_highlight_reel(match_id, player?)` and resource `demoreel://matches/{id}/analysis`.

## Design choices that matter to agents

- **Players by name.** Every tool that takes a player accepts a SteamID or a (partial) name; ambiguity
  returns the candidate list instead of guessing.
- **Compact outputs.** List tools return one-line summaries; the full analysis is a resource, not a tool
  result, so it does not flood the context by accident.
- **Blocking waits, not polling.** `render_highlight(..., wait_seconds=90)` or `get_job(job_id, wait_seconds=30)`.
- **Errors you can act on.** e.g. `Segment 1: pov_steamid '123' is not a player of this match. Players: […]`.
- **Edits are atomic and non-destructive.** A failed operation changes nothing; existing videos are kept
  and flagged `stale`.
- **Tool annotations** mark read-only vs. destructive tools so clients can auto-approve the safe ones.

## Edit operations

```jsonc
{"op": "trim", "segment": 0, "start_delta_s": 1.0, "end_delta_s": -0.5}   // seconds; negative start = earlier
{"op": "set_range", "segment": 0, "start_tick": 74201, "end_tick": 74782}
{"op": "set_pov", "segment": null, "steamid": "7656…"}                    // null segment = all; null steamid = overview
{"op": "set_speed", "segment": 1, "speed": 0.5}                           // 0.5 = slow motion
{"op": "set_caption", "segment": 1, "caption": "1v2"}
{"op": "add_segment", "start_tick": 8000, "end_tick": 8200, "pov_steamid": "7656…", "index": 0}
{"op": "remove_segment", "segment": 2}
{"op": "move_segment", "segment": 2, "to": 0}
{"op": "split_segment", "segment": 0, "at_tick": 74560}
{"op": "set_meta", "title": "…", "description": "…", "tags": ["…"]}
```

Limits: ≤ 40 segments, ≤ 300 s per highlight, each segment ≥ 0.25 s and inside the demo.

## Example session

> **User:** In the last match, make Juca's pistol-round clutch punchier and send me the clip.
>
> 1. `list_matches(limit=1)` → `m_cdaf…`
> 2. `list_highlights(match_id, player="juca")` → `h_fa6d…` "1v2 clutch + 2K (Glock-18)", 10.1 s
> 3. `get_highlight("h_fa6d…")` → one segment 74137–74782, kills at 74393 and 74622
> 4. `edit_highlight("h_fa6d…", ops=[trim start +1s, split at 74560, set_speed segment 1 → 0.5, caption])`
> 5. `render_highlight("h_fa6d…", wait_seconds=90)` → `video_url`

## What is coming for agents

Templates (intro/outro bumpers per client/map/situation), music, transitions and speed ramps will arrive as
new fields on the same highlight spec and new operations on the same `edit_highlight` tool — plus cheap
preview renders and frame grabs so an agent can review its own cut. See the [roadmap](../ROADMAP.md).
