# Roadmap

Status legend: ✅ done · 🚧 in progress · 🔜 next · 💡 idea

## Where we are (v0.1)

- ✅ Demo ingestion (upload / server-side import, `.dem` + gz/bz2/zst/zip, SHA-256 dedupe)
- ✅ Match analysis: rounds, economy, K/D/A, ADR, KAST, rating, opening duels, trades, multi-kills, clutches, utility
- ✅ Automatic highlight detection with scoring (multi-kills, clutches, flashy kills, ninja defuses)
- ✅ Highlights as editable, declarative specs (segments with ticks, POV, speed, caption) + validated edit operations
- ✅ Async jobs (DB-backed queue, worker capabilities, long-poll, webhooks)
- ✅ 2D tactical replay renderer (runs anywhere, no game needed)
- 🚧 In-game renderer (real CS2 footage from the player's POV) — ported from cs-demo-manager's flow; first
  real recordings work on Windows (single and multi-take, CLI and server queue); reels, cancel and Linux
  still unverified
- ✅ REST API + OpenAPI, MCP server (HTTP + stdio), CLI
- ✅ Web UI redesigned around video ("the tape"), themed by the brand kit, pt-BR + en
- ✅ Brand kits, generated intro/outro bumpers with sound, HUD, masters — see section 2
- ✅ Match sources: import a community's matches from its own site — throttled, with a circuit breaker
  after a bulk import exhausted the first site's hosting quota

## 1. In-game POV highlights — the main product 🔜

Player-POV footage recorded from the real game is what a highlight *is*; the 2D replay is the fallback for
machines without the game. Making this path boring and reliable is the top priority.

- 🚧 Validate the `startmovie` backend on Windows: single take ✅, multi-take ✅, server queue ✅ — reels,
  cancel mid-recording and a remote worker still to do; publish a known-good CS2 build ↔ plugin matrix
- 🔜 Record with chosen client settings (crosshair, viewmodel, video quality) instead of the game defaults
- 🔜 Version check: detect a CS2 update that outdates the playback plugin and fail fast with a clear message
- ✅ HLAE backend on Windows: frames piped straight into ffmpeg (x264 or NVENC), recording in real time —
  about 2× faster end to end than `startmovie`, which stays as the portable fallback
- ✅ Highlight scoring v2 (feat, round impact, style, economy, stakes, outcome) with a stored breakdown, tiers
  and curation: first-person videos for the featured plays of each team, 2D replays for the rest
- ✅ Cuts around the exchange instead of fixed pre/post-roll (about a third shorter), clutches that end on the
  defuse with a bomb clock, cold open for vertical videos, poster on the last kill
- ✅ Visitors and organisers (`DEMOREEL_ADMIN_KEY`), embed mode (`?embed=1`), share pages with previews
  (`/p/<id>`), brand kits that carry the community site's own UI conventions
- ✅ Editing: speed ramp through quiet stretches, freeze frame with the label on the deciding kill, context on
  the nameplate, punch-in on vertical videos, chat reactions at the end of the clip, exact kill marks per video
- ✅ Replay from the victim's eyes for plays of the match (rationed: tier S, one kill each, 3 per match)
- ✅ Fails ("zoeira"): whiffs, team / self flashes that get somebody killed, team kills, own grenade, fall, bomb,
  knife out - as their own category, the funniest two of a match in first person
- ✅ Chat: classified lines linked to kills, shown on plays and in videos; cry-baby / foul mouth / giggler rankings
- ✅ Across matches: rivalries, player profiles, match and night trophies (REST + MCP + screens)
- 🔜 Record a whole night in one game session (chain demos instead of restarting the game: ~10-15 % less)
- 🔜 Radar picture-in-picture on clutches; slow motion around a kill with 60 fps footage
- 🔜 Team plays (wipes, comebacks, retakes) and utility plays as highlights; trophies as image cards
- 🔜 Sound: kill accents, procedural music bed, optional narration through an HTTP TTS provider
- 🔜 HLAE extras: motion-blur sampling, kill-feed control (`mirv_deathmsg`), depth
- 🔜 Alternative control channel without the plugin (`-netconport` + scheduled commands), fewer moving parts
- ✅ Edits reuse stored captures (masters): only a new point of view or new ticks relaunch the game
- ✅ One game session per match (`capture_match`): 16 plays of a match recorded at 1080p in one session
- ✅ Automatic x-ray around kills through smoke / walls (`ingame_xray: auto`)
- 🚧 Speed: measurements and ranked plan in [docs/performance.md](docs/performance.md) — JPEG frames and no
  background-window sleep took a 60 fps capture job from 4 min 12 s to 1 min 39 s; next is one game session
  per match, since start-up is now the largest fixed cost
- 🔜 **Recorder container**: the Linux CS2 client in a GPU container/VM (Steam Linux Runtime, virtual display,
  NVENC/VAAPI) running `demoreel worker -c ingame`, so first-person rendering needs no desktop PC at all.
  There is no "demo-only" engine — the full client is the renderer — but
  [reka-ai/cs2-dem-renderer](https://github.com/reka-ai/cs2-dem-renderer) (MIT) rendered 10k demos on Linux
  with this same actions-file + plugin + `startmovie` technique, which shows it scales
- 🔜 Record in the background on a desktop PC: hidden / minimised game window that keeps rendering without
  focus, so the machine stays usable while clips are captured
- 🚧 Dedicated "recorder node" mode: a PC or GPU VM that only pulls jobs from a remote server. The server
  side is done ([docs/workers.md](docs/workers.md): per-worker tokens, claim / heartbeat / upload / finish
  over HTTPS, footage kept on the worker and edits addressed to it); next is the worker program itself
  (`demoreel worker --server <url>`), with rules for when the machine may record
- 💡 Hardware encoding (NVENC / VAAPI / AMF) and 1080p60 / 1440p presets
- 💡 Camera options beyond POV: victim POV replay, free-cam / cinematic paths for the last kill

## 2. Automatic editing — highlights that come out finished 🚧

Every highlight should be publishable as rendered. Capture (renderer → master) and composition (everything
below) are separate stages, so none of this needs the game again. See [docs/branding.md](docs/branding.md).

- ✅ **Composition stage** between capture and the final file; captures kept as masters with handles
- ✅ **Intro / outro bumpers generated from the brand kit** — logo split into pieces and assembled on screen,
  with synthesised sound; cached per brand
- ✅ **Brand kits**: logo, colours, fonts, tagline, locale; one per instance, or per match source
- ✅ **HUD on first-person footage**: corner mark with score and round, nameplate, kill counter, captions,
  letterbox in slow motion, flash + whoosh on time jumps
- ✅ **Slow motion with real frames** (recorded at `fps / speed`), pitch-dropped audio
- ✅ **Music bed** from the kit, ducked under game audio, loudness-normalised (no music is bundled)
- 🔜 **Templates beyond one kit per instance**: selection rules per **map** and **situation** (ace, clutch,
  pistol round, overtime, match point…), sponsor sting slot, several bumper styles ("assemble" is the first)
- 🔜 **Edit recipes** an agent or the auto-editor applies in one call — grounded in what commercial clips do:
  - 3K (12–15 s): start 3 s before kill 1, real time, hold 0.5 s after the last kill
  - ace (20–25 s): compress gaps over 4 s with a ramp, counter 1/5…5/5, 40 % speed for ~0.7 s on kill 5,
    short replay of the last kill from the victim's side
  - 1vN clutch (25–35 s): open on a situation card (radar snapshot, HP, time), alive-count overlay, slow motion
    on the last kill or the defuse, end on the round-win frame
- 🔜 **Speed ramps** (ease into and out of slow motion; today speed is constant per segment), freeze frames
- 🔜 **Visibility-aware cuts**: drop or re-angle footage where the POV shows nothing (inside a smoke, flashed)
- 🔜 **Titles, tags and thumbnails generated from demo facts**, in the kit's language (titles are English today)
- 🔜 Transitions between clips in a reel; reels composed from masters with the HUD (today reels of in-game
  clips use the same path as single clips, 2D reels have their own title cards)
- ✅ Output formats: 9:16 for phones (4:5 centre window, own kill feed, header/CTA), 1:1, all cut from one
  1080p master; `produce` makes a whole match in one call
- 🔜 Smarter vertical reframing (follow the crosshair target instead of the centre), 1080p60, platform presets
- 🔜 Master retention policy; re-capture at a higher frame rate in the background when slow motion asks for it
- 💡 Victim POV / free-cam replay of the last kill; AI voice-over and captions; A/B variants
- 💡 Timeline editor in the web UI on top of the same edit operations

### Getting clips to people (lessons from studying Allstar)

- 🔜 **Chat-command clipping**: `!clip` typed in all-chat is in the demo — clip that player's previous round,
  no client integration needed
- 🔜 **Personal threshold**: clip when a round beats that player's own average, with a per-match cap
- 🔜 Discord delivery (bot / webhook) the morning after the mix; claimable player pages
- 🔜 Smoke render after every CS2 update with a public status line
- ✋ Deliberately not copying: a consumer social feed, clip quotas and four-tier feature matrices, 5 s of
  sponsor before the play, a 700-item effects marketplace, holding a partner's clips on our own platform

## 3. Agent-friendliness 🔜

- ✅ MCP tools for querying, editing and rendering; compact outputs; job handles with blocking wait
- 🔜 MCP tools for templates, music and edit presets as section 2 lands
- 🔜 "Critic" loop: give an agent frames/low-res previews of a render so it can review its own edit
- 🔜 Fast low-res preview renders so agents (and humans) can iterate cheaply before the final render
- 🔜 Upload from an MCP client without shared disk (resumable upload URL handed out by a tool)
- 💡 Natural-language search over a library ("all AWP noscopes on Mirage last month")

## 4. Analysis depth 🔜

- 🔜 Positions-based stats: time to kill, crosshair placement, spray accuracy, flash effectiveness, entry paths
- 🔜 Per-round win probability and "round swing" to rank highlights by how much they mattered
- 🔜 Player POV demos (not only GOTV/SourceTV), FACEIT / ESEA / Premier metadata, bot handling
- 🔜 Multi-demo series (BO3) and cross-match player profiles
- 🔜 Visibility-aware clips: detect when the POV shows nothing useful (inside a smoke, fully flashed) and
  trim or switch camera — e.g. a clutch won by defusing inside one's own smoke is a blank screen in first person
- 🔜 Golden-file regression tests against a public demo set; compare against another parser to catch drift
- 💡 Highlight scoring tuned from feedback (which clips people keep, share, or delete)

## 5. Platform 🔜

- 🔜 Database migrations (Alembic) — required before the schema changes again
- 🔜 Object storage (S3-compatible) for demos and videos; retention policies; delete demo after analysis option
- 🔜 Postgres as a first-class deployment target; multi-worker scaling notes
- 🔜 Auth beyond a single API key: users / organisations (needed for per-client templates), scoped tokens
- 🔜 Server-sent events for job progress (today `?wait=` only returns on completion, so the UI polls for
  the progress bar) and a cheap "anything running?" signal; rate limits; upload resumability
- 🔜 Host-independent file links (relative `video_path` next to the absolute `video_url`)
- 🔜 Renders carry their highlight's player/round so clients do not have to join
- 🔜 Docker images published per release; Helm chart 💡
- 💡 Integrations: Discord bot, auto-ingest from a match server (MatchZy / Get5 webhook), YouTube/TikTok upload
