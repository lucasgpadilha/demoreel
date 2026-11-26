# demoreel over REST

For agents without the MCP tools. Base URL `http://127.0.0.1:8000` by default; the server's own summary is
`/llms.txt` and the full contract `/openapi.json` (interactive at `/docs`). With a key set on the server, send
`Authorization: Bearer <key>` (or `X-API-Key: <key>`). Errors are 4xx with `{"detail": "..."}`.

| Flow | Call |
|---|---|
| Upload a demo | `POST /api/v1/matches`, multipart field `file` (`.dem`, or `.gz` / `.bz2` / `.zst` / `.zip`) → 202 `{match, job, created}`; the same demo twice gives the same match (`created: false`) |
| Wait for a job | `GET /api/v1/jobs/{job_id}?wait=60` (blocks up to 120 s; repeat until `status` is `succeeded`, `failed` or `canceled`) |
| What this server can do | `GET /api/v1/capabilities` |
| Matches | `GET /api/v1/matches`, `GET /api/v1/matches/{id}` |
| Scoreboard | `GET /api/v1/matches/{id}/scoreboard` |
| Players, rounds, kills | `GET /api/v1/matches/{id}/players`, `/players/{steamid}`, `/rounds`, `/rounds/{n}`, `/kills?attacker=&weapon=&headshot=` |
| Everything at once (large) | `GET /api/v1/matches/{id}/analysis` |
| Highlights | `GET /api/v1/matches/{id}/highlights?category=play` (`fail`, `all`; `featured=true`; `player=<steamid>`) |
| One highlight | `GET /api/v1/highlights/{id}` |
| Hand-made clip | `POST /api/v1/matches/{id}/highlights` with `{"title", "segments": [...], "category": "fail"}`, or `start_tick` + `end_tick` + `pov_steamid`, or `round` + `player_steamid` |
| Move a hand-made clip to the fails / plays | `PATCH /api/v1/highlights/{id}` `{"category": "fail"}` |
| Promote to featured | `PATCH /api/v1/highlights/{id}` `{"featured": true}` |
| Edit | `POST /api/v1/highlights/{id}/edit` `{"ops": [{"op": "trim", "segment": 0, "start_delta_s": 1.0}, ...]}` |
| Render one clip | `POST /api/v1/highlights/{id}/renders` `{"renderer": "auto", "options": {"format": "9:16"}}` → 202 `{render, job}` |
| Reel | `POST /api/v1/matches/{id}/reels` `{"highlight_ids": [...]}` or `{"top": 5, "player_steamid": "..."}` |
| Every video of a match | `POST /api/v1/matches/{id}/produce` `{"formats": ["16:9", "9:16"]}` |
| A video | `GET /api/v1/renders/{id}` → `video_url`, `stale` |
| Cancel | `POST /api/v1/jobs/{job_id}/cancel` |
| Across matches | `GET /api/v1/insights/nights`, `/insights/rivalries`, `/insights/players/{steamid}`, `/insights/weapons?weapon=`, `/insights/trophies`, `/insights/chat` (filters `night=YYYY-MM-DD`, `match_id`, `since`, `until`) |

A segment is `{"start_tick", "end_tick", "pov_steamid", "speed", "caption"}`; for a fixed camera leave
`pov_steamid` out and add `"camera": {"x", "y", "z", "pitch", "yaw"}` (the numbers CS2's `spec_pos` prints).
In the REST API players are SteamIDs (strings); names are resolved only by the MCP tools.
