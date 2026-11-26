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
| `list_highlights`, `get_highlight` | Browse clips (`category="fail"` for the fails, `"all"` for both); read segments before editing |
| `set_featured` | Promote a play to the featured ones (they get first-person videos) or take it out; survives re-scoring |
| `rescore_match` | Re-run scoring and curation on an analyzed match (fast, the demo is not read again) |
| `create_highlight` | New clip from segments, a tick range, or round + player; `category="fail"` files it under the fails (banter); a segment with `camera` `{x, y, z, pitch, yaw}` (from `spec_pos`) is filmed from that fixed spot instead of a player |
| `set_category` | Move a hand-made clip between the plays and the fails of its match |
| `edit_highlight` | Atomic list of edit operations (below) |
| `delete_highlight` | Delete a clip definition (its videos are kept) |
| `render_highlight`, `render_reel` | Produce video (async, optional blocking wait); `format` `16:9` / `9:16` / `1:1` |
| `produce_match` | Every video of a match in one call: 2D of every play, first-person of the featured ones, in each shape |
| `get_job`, `get_render`, `cancel_job` | Follow long-running work |
| `get_feed` | The best plays that already have a video, across matches |
| `import_demo` | Analyze a demo already on the server's disk (whitelisted folders only) |
| `list_source_matches`, `import_matches` | Matches listed by the community's site (a configured source) and importing them |
| `get_capabilities`, `list_workers`, `get_brand` | Which renderers this server has; the remote workers (the PC that records) and whether they are online; the community's name, colours and logo |
| `list_nights`, `get_rivalries`, `get_player_profile`, `get_weapon_rankings`, `get_trophies`, `get_chat_rankings` | Across matches: nights of play, who kills whom (nemesis / favourite victim), one player over the season, who kills the most with each weapon, trophies of a match or a night, chat rankings for banter. All take `night=YYYY-MM-DD` or `match_id`; no filter = every match |

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
{"op": "set_pov", "segment": null, "steamid": "7656…"}                    // null segment = all; null steamid = overview; removes a fixed camera
{"op": "set_camera", "segment": 0, "camera": {"x": -324.4, "y": -8.2, "z": 506.0, "pitch": 38.2, "yaw": -121.3}}
                                                                          // film from that spot (clears the player); null camera = overview
{"op": "set_speed", "segment": 1, "speed": 0.5}                           // 0.5 = slow motion
{"op": "set_caption", "segment": 1, "caption": "1v2"}
{"op": "add_segment", "start_tick": 8000, "end_tick": 8200, "pov_steamid": "7656…", "index": 0}  // or "camera": {…}
{"op": "remove_segment", "segment": 2}
{"op": "move_segment", "segment": 2, "to": 0}
{"op": "split_segment", "segment": 0, "at_tick": 74560}
{"op": "set_meta", "title": "…", "description": "…", "tags": ["…"]}
```

Limits: ≤ 40 segments, ≤ 300 s per highlight, each segment ≥ 0.25 s and inside the demo. A segment is
watched from a player or from a fixed camera, never both (`spec_pos` in the CS2 console prints the camera's
five numbers). A camera's `x`, `y` and `z` are finite and within ±16384, its `pitch` within -90..90. Fixed
cameras are filmed by the in-game renderer only: a 2D replay shows the whole map during those segments and
says so in the job result's `warnings`.

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

## Agent skill and Claude Code plugin

[`skills/demoreel/SKILL.md`](../skills/demoreel/SKILL.md) is an [Agent Skill](https://agentskills.io): a
short guide an agent loads when a request is about CS2 demos, stats, clips or videos. It says what to install
(and what only in-game recording needs), how to connect, the flows of this page, and the recording pitfalls
(CS2 updates breaking the plugin or HLAE, recording refused while CS2 is open, jobs that need a worker).
[`references/rest-api.md`](../skills/demoreel/references/rest-api.md) maps the same flows to REST calls.

### Claude Code

The repository is a plugin marketplace whose only plugin is the repository itself:

```text
/plugin marketplace add lucasgpadilha/demoreel
/plugin install demoreel@demoreel
```

That installs the skill (Claude picks it up on its own, or call it with `/demoreel:demoreel`) and an MCP
server over HTTP, so start `uv run demoreel serve` in your clone first. On install the plugin asks for two
settings: the server's MCP address (default `http://127.0.0.1:8000/mcp`, a local `demoreel serve`) and its key
(the `DEMOREEL_ADMIN_KEY`, else the `DEMOREEL_API_KEY`; leave it empty for a server without one), which Claude
Code keeps in the system's credential store. The plugin does not run demoreel itself: Claude Code copies
plugins into its own cache, where a `demoreel mcp` would see a database of its own, with none of your matches
and no worker. If the plugin's connection is refused anyway, add your own (`claude mcp add --transport http
demoreel <url> --header "Authorization: Bearer <key>"`) and disable the plugin's in `/mcp`.

`plugin.json` carries a `version` (the project's, `0.1.0`): users stay on it until it changes, so bump it with
each release (`tests/test_skill.py` checks that it matches `pyproject.toml`). Users update with `claude plugin
update demoreel@demoreel` or **Update now** on the plugin in `/plugin`; `/plugin marketplace update demoreel`
only refreshes the catalog. Check the manifests with `claude plugin validate --strict .`. Because the
repository root is the plugin, Claude Code copies the whole repository into its cache on install; a
`plugins/demoreel/` folder with only the manifest and the skill would avoid that, at the cost of a second copy
of the skill.

### Other agents and skill marketplaces

Copy `skills/demoreel/` into the folder your agent loads skills from (Claude Code without the plugin:
`~/.claude/skills/demoreel/`) and connect the MCP server as in [Connecting](#connecting). Marketplaces that take
a zip get it from:

```bash
uv run python scripts/build_skill_zip.py           # -> dist/demoreel-skill.zip (git-ignored)
uv run python scripts/build_skill_zip.py --check   # only check the skill
```

The zip has `SKILL.md` at its root and `references/` next to it, LF line endings and fixed timestamps (same
sources, same bytes). The script refuses frontmatter without a valid `name` and `description`, files outside
`references/`, `scripts/` and `assets/`, relative links that would break once zipped (link to the repository
instead) and archives over 50 MB. `tests/test_skill.py` also checks that every tool the skill names exists in
the MCP server and takes the parameters the skill passes it, that every REST route in
`references/rest-api.md` exists in the API, and that the skill states a demoreel version no newer than the
code; keep the skill in step when a tool, a parameter or a route changes. The skill says it was tested with
demoreel 0.1.0 and later and tells the agent to compare that with the `version` of `GET /health` or
`get_capabilities()`.

Suggested listing text (free listing; links to this repository are fine, offers of paid versions elsewhere
are not):

> **demoreel: CS2 demos to stats, highlight clips and videos**
>
> Teaches your agent to drive demoreel, an open-source (MIT), self-hosted service for Counter-Strike 2
> demos: scoreboard-grade stats (K/D/A, ADR, KAST, rating, clutches, opening duels), automatic highlights and
> fails, rivalries and trophies across matches, hand-made clips (fixed camera spots included) and rendered
> videos, either first-person game footage or a 2D radar replay, in 16:9 and 9:16 for phones.
>
> **Requirements.** demoreel 0.1.0 or newer, installed and running on Windows or Linux (macOS is not
> supported), and an agent that can use MCP or call a REST API. Installing takes git and uv (`git clone
> https://github.com/lucasgpadilha/demoreel`, `uv sync`, `uv run demoreel serve`); uv downloads Python, so the
> first run needs internet, and so does the first 2D video (it fetches the map images). On Linux, also
> `sudo apt-get install -y libgl1 libegl1 libfontconfig1`. First-person video recording also needs
> Counter-Strike 2 through Steam and a GPU on the same machine, plus the CS Demo Manager playback plugin, a
> third-party binary (MIT) you download yourself; HLAE is optional. Stats, highlights and 2D videos need none
> of that. CS2 demos only (`.dem`, `.gz`, `.bz2`, `.zip`; `.zst` with Python 3.14+): not for CS:GO demos or
> live match data. On Windows PowerShell, call `curl.exe` rather than `curl`.
> The skill is instructions only (no binaries or scripts); it talks to your own demoreel server.
>
> Source, documentation and issues: https://github.com/lucasgpadilha/demoreel. Not affiliated with Valve.
