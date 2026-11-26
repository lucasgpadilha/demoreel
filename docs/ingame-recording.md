# In-game recording (`ingame` renderer)

The `ingame` renderer records highlights with the real Counter-Strike 2 client, from the player's point of
view. It starts CS2 on the demo, lets the game write an image sequence + audio for each clip, encodes them with
ffmpeg and joins them into one mp4.

> **Status: validated on Windows, on one machine; more coverage needed.** The code is a port of
> [CS Demo Manager](https://github.com/akiver/cs-demo-manager)'s flow. Real recordings on 2026-11-14
> (Windows 11, CS2 1.41.9.7 / patch 14197, plugin `server.dll` from cs-demo-manager commit `96a2967`) produced
> correct first-person clips, both from the CLI and through the server's job queue. See
> [What has been verified](#what-has-been-verified) and [What is not verified](#what-is-not-verified).

## Requirements

| | |
|---|---|
| OS | Windows 10/11, or Linux with a desktop session (X11 or Wayland). macOS cannot run CS2. |
| Game | CS2 installed through Steam, **Steam running and logged in**, CS2 **not** running. |
| Plugin | The CS2 plugin binary of CS Demo Manager, provided by you (see [Setup](#setup)). demoreel does not ship or download it. |
| Hardware | A GPU that runs CS2. The game window must be able to open at the requested resolution. |
| Disk | Raw frames are JPEG by default: roughly **0.1–0.2 GB per minute at 1280x720/30 fps**, on the drive where CS2 is installed, deleted after encoding. With `ingame_frames: "tga"` (lossless) it is about 6 GB per minute, and slow disks will stall the recording. |
| ffmpeg | Same as the rest of demoreel (`DEMOREEL_FFMPEG`, `ffmpeg` on PATH, or the bundled `imageio-ffmpeg`). |

The game runs with `-insecure` (no VAC, cannot join VAC-secured servers) for the duration of the recording only.

## Setup

1. **Get the plugin binary** from the CS Demo Manager repository (MIT), folder
   [`static/cs2/`](https://github.com/akiver/cs-demo-manager/tree/main/static/cs2):

   | File | For |
   |---|---|
   | `server.dll` (Windows) / `libserver.so` (Linux) | the **latest** CS2 build - what you want in most cases |
   | `server_<N>.dll` / `libserver_<N>.so` | frozen builds that work with CS2 **up to patch N** (`N` = `PatchVersion` of `game/csgo/steam.inf` without dots: 1.41.6.8 -> 14168) |
   | `libcsdm_vulkan_layer.so` / `libcsdm_vulkan_layer_<N>.so` | Linux only, optional: the Vulkan layer built with the matching plugin (it also fixes the color order of recorded frames) |

2. **Put it in the plugin folder**, keeping the file name: `<data_dir>/cache/csdm-plugin/` (so
   `data/cache/csdm-plugin/server.dll` with the default data folder), or any folder named by
   `DEMOREEL_INGAME_PLUGIN_DIR`.

3. **Enable the renderer** on that machine: `DEMOREEL_INGAME_ENABLED=1` (for `demoreel serve` / `demoreel worker
   -c ingame`). The one-shot `demoreel render --renderer ingame` command does not need it.

4. **Check**: `demoreel doctor` prints whether the renderer is available and, if not, exactly what is missing
   (which file to copy where, Steam not running, ...).

### How the binary is chosen

CS Demo Manager lets the user pick a plugin version in its settings (default: latest). demoreel picks
automatically from the files present in the plugin folder: if a frozen build covers the installed CS2 patch
(smallest `N >= patch`) it is used, otherwise the latest build. Set `DEMOREEL_INGAME_PLUGIN_VERSION` (`latest`
or a number such as `14168`) to force one.

### Environment variables

| Variable | Meaning | Default |
|---|---|---|
| `DEMOREEL_INGAME_ENABLED` | let workers of this machine record with CS2 | off |
| `DEMOREEL_CS2_DIR` | the `Counter-Strike Global Offensive` folder (its `game` or `game/csgo` subfolder also works) | auto-detected |
| `DEMOREEL_STEAM_EXE` | path of `steam.exe` / `steam.sh`; its folder is used to find the Steam libraries | auto-detected |
| `DEMOREEL_INGAME_PLUGIN_DIR` | folder holding the plugin binaries | `<data_dir>/cache/csdm-plugin` |
| `DEMOREEL_INGAME_PLUGIN_VERSION` | force a plugin build (`latest`, `14168`, ...) | automatic |
| `DEMOREEL_INGAME_TIMEOUT_S` | hard limit for one game session, in seconds | `240 + 90 x takes + 12 x clip seconds` |
| `DEMOREEL_INGAME_RUNTIME_SCRIPT` | Linux: path of `SteamLinuxRuntime_sniper/_v2-entry-point` | searched in the Steam libraries |
| `DEMOREEL_INGAME_BACKEND` | recording system: `auto`, `startmovie`, `hlae` (see [Recording backends](#recording-backends)) | `auto` |
| `DEMOREEL_INGAME_HLAE_EXE` | path of `HLAE.exe` | the highest version under `<data_dir>/cache/hlae/` that has `x64/AfxHookSource2.dll` |
| `DEMOREEL_INGAME_HLAE_ENCODER` | encoder HLAE pipes frames into: `x264` or `nvenc` (NVIDIA cards) | `x264` |
| `DEMOREEL_INGAME_CAPTURE_FPS` | minimum recording frame rate; recording time grows with it | `30` |

Auto-detection: Windows reads `HKCU\Software\Valve\Steam\SteamPath` (then `HKLM`), Linux tries `~/.steam/steam`,
the snap location, `~/.local/share/Steam` and the flatpak location; `steamapps/libraryfolders.vdf` then gives
the library that owns app 730.

### Per-render options

Passed in the render request's `options.extra` (all optional):

| Key | Effect | Default |
|---|---|---|
| `ingame_hud` | `"deathnotices"` (kill feed only) or `"full"` | `"deathnotices"` |
| `ingame_xray` | Player outlines through walls and smoke. `"auto"`: only around the followed player's kills **through smoke or through a wall** (on 3 s before, off 1 s after), so those kills can be read; `true`: always; `false`: never | `"auto"` |
| `ingame_assists` | show assists in the kill feed | `true` |
| `ingame_player_voices` | include the players' voice chat recorded in the demo | `false` |
| `ingame_true_view` | `cl_demo_predict 1` ("TrueView") | `false` |
| `ingame_commands` | console commands run before each take, after demoreel's own (list, or one string separated by `;`) | none |
| `ingame_launch_args` | extra CS2 launch options (`-insecure` cannot be removed) | none |
| `ingame_keep_raw` | keep the TGA/WAV files in the game folder (debugging) | `false` |
| `ingame_swap_red_blue` | swap red and blue while encoding | Linux without the Vulkan layer |
| `ingame_lock_wait_s` | how long to wait for another recording to finish. A queued job that is still waiting after that goes back to the queue, in its place; it does not fail | 0 for queued jobs, 1800 for a direct render |
| `ingame_startup_timeout_s` / `ingame_stall_timeout_s` | give up when no frame / no new frame shows up for this long | 300 / 300 |

`width`, `height`, `fps` and `crf` come from the normal render options; the game window is opened at
`width x height`. `title_cards` and segment captions are not drawn by this renderer, and **`speed` is ignored**
(the reference has no playback-speed support in this recording path; everything is recorded at 1x).

## How it works

Everything below is a port of CS Demo Manager (file names refer to its repository).

1. **Slots.** `spec_player` does not take a SteamID but a player slot. CSDM's analyzer stores
   `(userinfo.userid & 0xff) + 1` for each player and the app emits `spec_player <slot>`
   (`cs-demo-analyzer/pkg/api/player.go`, `export_csdm.go`, `fetch-match-players-slots.ts`). demoreel reads the
   same value with `demoparser2` (its `user_id` prop is `userid & 0xff`) at each clip's start tick and adds 1.
   Bots have no SteamID and cannot be targeted; such a clip is recorded with whatever camera the game picks, and
   a warning is returned.
2. **Actions file.** `<demo>.dem.json` next to the demo, a list of sequences of `{tick, cmd}`
   (`create-cs2-video-json-file.ts`, `json-actions-file-generator.ts`). For each take:
   - tick 96: HUD/cvar setup (`sv_cheats 1`, `volume 1`, telemetry and build info off, `cl_demo_predict`,
     `cl_draw_only_deathnotices`, `tv_listen_voice_indices`), then `demo_gototick <setup tick - 1>`;
   - setup tick (= start - one second of ticks): `spec_show_xray`, `mp_display_kill_assists`,
     `host_framerate <fps>`, the plugin's own panels off (`csdm_buttons_overlay 0`, `csdm_voice_indicator 0`,
     not in the reference: see [Known pitfalls](#known-pitfalls)), then your `ingame_commands`;
   - start - 4: `pause_playback` (the plugin pauses 2 s so the loading tint is not recorded);
   - start: `spec_mode 1`, `spec_player <slot>`, `startmovie <n>-sequence`. A segment with a fixed `camera`
     (`{x, y, z, pitch, yaw}`, the numbers `spec_pos` prints in the console) gets `cl_demo_freecam 0` and
     `spec_goto x y z pitch yaw` there instead (`addFocusCamera`: since the 2026-10-14 CS2 update the demo free
     camera ignores `spec_goto` unless it is turned off first). Inside the pipeline that camera is a point of
     view of its own (`cam:x,y,z,pitch,yaw`), so stored footage is matched per camera as it is per player;
   - end: `endmovie`; end + 64: `go_to_next_sequence` (the plugin restarts the demo from tick 0 for the next
     take) or `quit` after the last one.

   Actions never run before tick 96 (the game skips the first ticks of a demo), and the seek and `spec_player`
   are never on the same tick (since an October 2025 CS2 update `spec_player` is ignored otherwise). For that
   reason a take never starts before tick 160: a play from the first second of a demo is recorded from there,
   so the camera is aimed on ticks of its own. Takes are
   recorded in demo order and assembled in the order of the request. A take that plays past the end of the
   match is the last one of its game launch, and the takes left are recorded in a new launch: the game keeps
   its end-of-match screen up for the rest of the session, over every take recorded after it. Back-to-back segments of one clip are
   recorded as a single take with a camera switch.
   The demo is hard-linked (or copied) into the job's scratch folder first, so the JSON never lands in your
   demo folder and the path given to the game is short and ASCII-only (CS2 fails on other characters).
3. **Plugin.** CS2 has no plugin API. The binary is copied to `game/csgo/csdm/bin/` (`bin/linuxsteamrt64/` on
   Linux) and `Game<TAB>csgo/csdm` is inserted ahead of `Game<TAB>csgo` in `game/csgo/gameinfo.gi`, which makes
   the game load CSDM's `server` library in place of its own (`cs2-server-plugin.ts`). The original
   `gameinfo.gi` is saved as `gameinfo.gi.demoreel-backup` first and restored when the game exits, whatever
   happened. If demoreel itself is killed in between, the next run restores it before doing anything else
   (or simply deletes the backup when the file is already clean, e.g. after a Steam update).
   The plugin keeps the state of its panels in `game/csgo/cfg/csdm_settings.json` and writes it back whenever a
   setting changes, which hiding the panels does. That file is CS Demo Manager's, so it is saved
   (`csdm_settings.json.demoreel-backup`) before the game starts and put back once it has quit: the same bytes,
   or no file when there was none. A run that was killed is undone by the next one, as for `gameinfo.gi`.
4. **Launch.** `cs2.exe -insecure -novid +playdemo <demo> -width W -height H -sw` (Linux: the same through
   `SteamLinuxRuntime_sniper/_v2-entry-point --verb=waitforexitandrun -- game/cs2.sh`), with `USRLOCALCSGO`
   pointing to `<data_dir>/ingame/cfg` so your own CS2 video settings and binds are not touched
   (`start-counter-strike.ts`). A watchdog kills the game on timeout, on cancellation, when no frame appears,
   or when frames stop coming.
5. **Encode.** The game writes `game/csgo/csdm/movie/<timestamp>/<n>-sequence%08d.tga` and
   `game/csgo/movie/<timestamp>/<n>-sequence.wav` (`get-sequence-raw-files.ts`). Each take is encoded to H.264 +
   AAC at the requested size (`generate-video-with-ffmpeg.ts`), the takes are concatenated, a thumbnail is
   written and the raw files are deleted.

Files demoreel touches inside the CS2 folder, all removed/restored afterwards: `game/csgo/gameinfo.gi`
(+ `.demoreel-backup`), `game/csgo/cfg/csdm_settings.json` (written by the plugin; + `.demoreel-backup`),
`game/csgo/csdm/`, `game/csgo/movie/<timestamp>/<n>-sequence.wav`,
`game/bin/<platform>/csdm.log` (the plugin's log, deleted before each run and quoted in error messages).

## Known pitfalls

- **CS2 updates break the plugin.** It hooks game internals; after a game update it may crash the game, show an
  error dialog, or silently not load (symptom: "CS2 wrote no frames" / "No csdm.log was written"). Wait for
  CS Demo Manager to publish new binaries and replace the file. Old demos often cannot be played by a newer
  CS2 at all; the render result carries a warning when the demo's patch differs from the installed one.
- **One instance at a time.** A lock file (`<data_dir>/ingame/recording.lock`) serializes recordings. Queued
  jobs do not wait for it on a worker: a worker takes the lock before it takes a recording job, so while the
  game is recording the other workers of that machine leave those jobs in the queue - they run in the order
  they arrived, however many there are - and edit what is already recorded meanwhile. A match is one turn at the game, even when it needs two sessions (normal
  speed, then slow motion). Only a direct render (`demoreel render --renderer ingame`) waits, 30 min by
  default. If CS2 is already open - your own game - the render is refused: demoreel never kills a game it did
  not start.
- **Machines that host game servers.** A dedicated server (`cs2 -dedicated`) is a process with the same name
  as the game. demoreel tells them apart by the command line: servers do not count as "CS2 is already
  running", and when a recording is cancelled or times out only the game it started is closed - never a
  server. Two things to know:
  - a `cs2` process started by another user or as administrator cannot be inspected; it is taken for the game
    (recording is refused) and is never killed. Run demoreel as the user that runs the servers;
  - a server running **from the same CS2 folder** the game is recorded from blocks recording: for a moment
    that folder's `gameinfo.gi` is changed and a plugin is added, which a server starting meanwhile would load,
    and the game would load the server's add-ons (Metamod, CounterStrikeSharp). Record from a second copy of
    CS2 (another Steam library, `DEMOREEL_CS2_DIR`) or stop the server while recording.
  Recording next to running servers still shares the machine's CPU and GPU with them: measure before doing it
  during a match.
- **Do not touch the game while it records.** It runs windowed (`-sw`); keep the window un-minimized and fully
  on screen, and do not click or type in it (input may change the spectated player). A resolution
  larger than the desktop cannot be recorded correctly: frames are scaled/padded to the requested size.
- **Plugin panels are kept out of the video.** Recent plugin builds draw CSDM's in-game panels into recorded
  frames when they are visible, with both recording backends. Two of them show without anybody asking: the
  voice indicator ("who is talking": microphones and nicknames on the left), which is on by default and
  follows the voice data of the demo **whether or not `ingame_player_voices` is on**, and the buttons overlay
  (keys pressed by the spectated player) when the machine's owner turned it on in CS Demo Manager (the plugin
  remembers both in `game/csgo/cfg/csdm_settings.json`). Every take therefore runs `csdm_buttons_overlay 0` and
  `csdm_voice_indicator 0`. To have one in the picture, ask for it: `ingame_commands` runs afterwards, so
  `["csdm_voice_indicator 1"]` brings the indicator back. The playback panel is hidden by default and
  nothing is sent for it. Footage captured before this was the default keeps the panel
  it was recorded with. Hiding the voice indicator this way was checked in a real recording; the buttons
  overlay command and the built-in default were not (see [What is not verified](#what-is-not-verified)).
- **Disk usage** is temporary but large (see Requirements); the job fails early when the CS2 drive lacks space.
- **Steam must be running** and logged in; in offline mode the plugin starts the demo itself after ~8 s.
- **CS Demo Manager on the same machine**: do not use its playback/recording while demoreel records. If its
  search path is already in `gameinfo.gi`, demoreel leaves the file exactly as it found it. The plugin is pointed
  at an unused WebSocket port so an open CSDM app cannot drive the recording. Your panel settings
  (`game/csgo/cfg/csdm_settings.json`) are put back after each recording, so the voice indicator and the
  buttons overlay are as you left them the next time you watch a demo from CS Demo Manager.
- **Camera**: `pov_steamid = null` segments are not given a camera; the game shows its default spectator view.
  If the POV player is dead at the clip start the game may pick another camera.
- **Antivirus / permissions**: the CS2 folder must be writable by the user running demoreel.
- **Crash leftovers**: `gameinfo.gi` is restored automatically on the next run. To do it by hand, copy
  `gameinfo.gi.demoreel-backup` over `gameinfo.gi` and delete `game/csgo/csdm/`, or verify the game files in
  Steam. The plugin's settings come back the same way: by hand, copy
  `game/csgo/cfg/csdm_settings.json.demoreel-backup` over `csdm_settings.json` (an empty backup means there was
  no file: delete both). Settings changed in CS Demo Manager between the crash and the next recording are
  replaced by that older copy.

## Linux notes

- CS2 must be started through the Steam Linux Runtime 3.0 "sniper" (installed by Steam with CS2). It is looked
  up in every Steam library; set `DEMOREEL_INGAME_RUNTIME_SCRIPT` if it lives elsewhere.
- A display is required (`DISPLAY` or `WAYLAND_DISPLAY`); a headless server needs a virtual display with GPU
  access (not covered here).
- Without CSDM's Vulkan layer the frames written by CS2 have red and blue swapped; demoreel then swaps them back
  while encoding. With `libcsdm_vulkan_layer.so` in the plugin folder, the layer is installed for the session
  (`game/csgo/csdm/vulkan/`, enabled through `VK_ADD_IMPLICIT_LAYER_PATH` / `XDG_DATA_DIRS` /
  `ENABLE_CSDM_OVERLAY=1`) and no swap is applied. If colors look wrong, flip `ingame_swap_red_blue`.
- Fullscreen is broken on Linux since a September 2025 CS2 update; windowed mode is what is used anyway.

## What has been verified

One real run, on Windows 11 with a GTX 1660 SUPER, CS2 1.41.9.7 (patch 14197, same patch as the demo), plugin
`server.dll` from cs-demo-manager commit `96a2967` (2026-11-13), 1280x720 at 30 fps, one 8.6 s single-take
highlight from a SourceTV demo:

- The plugin loads on that CS2 build and executes the actions file (`csdm.log` shows every command).
- `spec_player <user_id + 1>` selected the intended player, from the very first frame.
- Frames and audio are found, encoded and muxed: H.264 + AAC stereo, exact expected frame count (257/257).
- Timing (wall clock, from the command start): ~85 s until the first recorded frame (demo analysis ~10 s, then
  game start + demo load), then under 50 s to record the 257 frames and encode them. About 2 min 15 s in total
  for an 8.6 s clip; game start-up dominates, which is why batching clips into one launch matters.
- `gameinfo.gi` is byte-identical to the original afterwards, and the backup file is gone.
- The user's own CS2 configuration is untouched: all 30 files under `userdata/*/730/local/cfg` hashed
  identical before and after (the game wrote its config to the private `USRLOCALCSGO` folder instead).
- The game closes by itself.

A second run went through the server (`DEMOREEL_INGAME_ENABLED=1`, `POST /highlights/{id}/renders` with
`renderer: "ingame"`, picked up by the worker with the `ingame` capability): a 30 s highlight made of three
takes (907/907 frames) finished in 1 min 44 s, with all three takes showing the right player and moment, the
video served by the API, and the CS2 folder and user configuration again left byte-identical.

Later runs (2026-11-15, same machine) validated the two-stage pipeline through the server:

- captures with **handles** (1.5 s before / 1 s after each segment) at **60 and 75 frames per second** for
  clips with slow-motion segments, stored as masters; frame counts exact (1148/1148, 1285/1285);
- `+engine_no_focus_sleep 0` and the early camera commands: accepted by the game, peak capture rate 52 frames/s;
- **JPEG frames** (`startmovie <name> jpg wav`, now the default): 31 frames/s sustained against 7.6 with TGA,
  70 MB against 4.2 GB for the same take, audio present, no visible quality difference — see
  [performance.md](performance.md);
- HUD, slow motion, captions and bumpers composed over the capture; a re-cut rendered from the master in
  26 s with in-game recording disabled and the game never started;
- after five recording sessions in total, `gameinfo.gi` and the user's configuration are still byte-identical
  and nothing is left in the game folder.

A recording on 2026-11-18 (CS2 patch 14198) of a demo from another platform, which carries the players' voice
chat, had the plugin's voice indicator in the picture for half of the clip although `ingame_player_voices` was
off; the same recording with `csdm_voice_indicator 0` passed through `ingame_commands` did not. That is the
command, on the tick, that every take now runs by itself.

The first run also found one defect, now fixed and covered by a test: CS2 writes `ServerConfig.vdf` and
`_iconcache/` into `game/csgo/csdm` while it is the first search path, which left that folder and
`game/bin/win64/csdm.log` behind. Both are now removed after a successful recording.

## What is not verified

- Reels (several highlights in one render) and takes that require seeking backwards in the demo.
- Cancelling in the middle of a recording, and the watchdog timeouts, against the real game.
- A standalone `demoreel worker -c ingame` on a second machine (only the worker started by `serve` was used).
- Anything on Linux: launch through the runtime, the Vulkan layer environment, the red/blue swap default, the
  flatpak Steam location.
- `spec_player` for players who reconnect during the match, and for bots (no POV is selected for bots).
- Behaviour when the demo's CS2 version differs from the installed one, and after the next CS2 update.
- Other resolutions / frame rates than 1280x720 at 30 fps; player-POV (non-SourceTV) demos.
- The recording uses the game's default settings (private config folder), not the machine owner's: default
  crosshair, viewmodel and video quality. Choosing those per render is not implemented yet.
- The plugin panels, beyond the one run above: `csdm_voice_indicator 0` sent by the recorder itself rather than
  through `ingame_commands` (same command and tick, covered by tests with a fake game only), on each recording
  backend, and `csdm_buttons_overlay 0` at all; a frozen plugin build from before the panels existed, where both commands
  are unknown to the game; and `csdm_settings.json` being put back after a real session (the plugin's source
  says it writes the file within 128 frames of a change and on shutdown; the restore runs once the game is
  gone).

## Manual test procedure

1. Unit tests (no game needed): `uv run pytest tests -k ingame -q`.
2. `uv run demoreel doctor` - the `ingame` line must be `ok`. Fix what it reports (plugin file, Steam, ...).
3. Close CS2, keep Steam open, make sure about 10 GB are free on the CS2 drive, then record one highlight:

   ```sh
   uv run demoreel render path/to/match.dem --renderer ingame --top 1 -o first-ingame.mp4
   ```

   CS2 opens in a window, loads the demo, jumps to the highlight, records and quits by itself. Do not interact
   with it.
4. Check: the mp4 plays with sound, shows the right player's first-person view from its first frame and has no
   loading tint at the start, and no plugin panel (with a demo that has voice chat: no microphones and
   nicknames on the left); `game/csgo/gameinfo.gi` contains no `csgo/csdm` line and
   `gameinfo.gi.demoreel-backup` and `game/csgo/csdm/` are gone; `game/csgo/cfg/csdm_settings.json` is the same
   as before (or still absent) with no `.demoreel-backup` next to it; CS2 starts normally from Steam afterwards.
5. Then try `--top 3` (several takes: the demo restarts between them) and a cancel in the middle (Ctrl+C or
   cancelling the job): the game must close and `gameinfo.gi` must be clean.
6. If it fails, the error message quotes the end of `game/bin/win64/csdm.log` (`linuxsteamrt64` on Linux).
   `[<tick>] Executing: startmovie 1-sequence` proves the actions file was loaded; no log at all means the
   plugin did not load (wrong build for this CS2 version, or `gameinfo.gi` not patched). Re-run with
   `options.extra.ingame_keep_raw = true` to inspect the raw files in `game/csgo/csdm/movie/`.

## Recording backends

`DEMOREEL_INGAME_BACKEND` (or `options.extra.ingame_backend` per render) selects how frames leave the game.
Planning, player slots, the plugin session, the lock, the watchdog, x-ray and masters are shared.

| | `startmovie` | `hlae` |
|---|---|---|
| What it is | the game's own recorder: one image file per frame, encoded afterwards | [HLAE](https://github.com/advancedfx/advancedfx) (`AfxHookSource2`) pipes frames straight into ffmpeg |
| Platforms | Windows, Linux | Windows only |
| Extra install | none | HLAE (not shipped; see below) |
| Speed, 64 s of 1080p30 in 6 takes | ~150 s recording + ~30 s encoding (the game runs at 12–15 frames/s, so playback looks like slow motion) | **66 s recording + 4 s joining** with `nvenc`, 75 s with `x264` — real time |
| 1080p60 | twice the 30 fps time | 88 s for the same plays |

`auto` (the default) uses HLAE where it is installed and `startmovie` everywhere else.

**Installing HLAE**: download the release zip from https://github.com/advancedfx/advancedfx/releases and
extract it to `<data_dir>/cache/hlae/<version>/` (so that `HLAE.exe` and `x64/AfxHookSource2.dll` are there),
or point `DEMOREEL_INGAME_HLAE_EXE` at an existing install. Several releases can sit side by side: the folder
with the highest version number wins (`2.193.10` over `2.193.9`; a folder named `backup` or `latest` only when
nothing else is there), and a folder without `x64/AfxHookSource2.dll` is never used. `demoreel doctor` prints
the one chosen and the others found. demoreel writes `ffmpeg/ffmpeg.ini` next to
`HLAE.exe` so HLAE finds the bundled ffmpeg. HLAE injects a DLL into the game; this is done with `-insecure`
(VAC is off for the session), exactly as CS Demo Manager does it.

How it runs (port of CS Demo Manager's HLAE path): `HLAE.exe -noGui -autoStart -noConfig
-afxDisableSteamStorage -customLoader -hookDllPath …/x64/AfxHookSource2.dll -programPath …/cs2.exe -cmdLine
"<game arguments>"` starts the game and exits; the session lasts until `cs2.exe` is gone. Per take the actions
file runs `mirv_streams record name|fps|start|end` with an ffmpeg preset writing `<take>/video.mp4`, and
`startMovieWav` for the sound (`<take>/take0000/audio.wav`). Afterwards each take's picture is copied
as recorded and muxed with its sound. The frames HLAE really wrote are counted per take and stored in the
master, so tick ↔ video time stays exact.

HLAE follows CS2 updates with a delay: after a game update, recording may fail until a new HLAE release is
installed. The error says so, and `DEMOREEL_INGAME_BACKEND=startmovie` keeps things working meanwhile.

Verified with HLAE 2.193.0: three plays / six takes at 1080p30 (x264 and NVENC) and 1080p60 (NVENC), frame
counts 1928–1930 of 1930 expected, sound present, kill feed only, automatic x-ray. Not verified: cancellation
mid-recording, a stalled HLAE session, CS2 builds newer than that HLAE release.

## Attribution

The recording technique - the plugin loaded through a temporary `gameinfo.gi` search path, the JSON actions
file and its command sequences, the launch parameters and the location of the raw files - is ported from
**[CS Demo Manager](https://github.com/akiver/cs-demo-manager)** by AkiVer, MIT License, Copyright (c)
2014-present AkiVer. The plugin binary itself is part of that project and is not distributed with demoreel.
Each module under `src/demoreel/render/ingame/` cites the reference files it was ported from.
