# Making video generation fast and cheap

What is measured, what has been done, and the ranked plan. Numbers are from one machine (Windows 11,
Ryzen 5 5600, GTX 1660 SUPER, SATA SSD, CS2 patch 14197, 1280x720) — treat them as a baseline, not a spec.

## Measured: recording backends (same three plays, 64 s of 1080p footage in six takes)

| Backend | Recording | After recording | Whole session |
|---|---|---|---|
| `startmovie`, JPEG frames, 60 fps | 271 s (game alive) | encode ~40 s | ~5 min |
| `startmovie`, JPEG frames, 30 fps | ~150 s (12–15 frames/s) | encode ~30 s | ~3.5 min (estimated from the measured rate) |
| **`hlae` + x264, 30 fps** | 75 s | join 4 s | **109 s** |
| **`hlae` + NVENC, 30 fps** | 66 s | join 4 s | **95 s** |
| `hlae` + NVENC, 60 fps | 88 s | join 5 s | 125 s |

The game's own recorder stops after every frame to compress and write an image; that, not the GPU, limits it
to 12–15 frames/s at 1080p, which is why playback looks like slow motion while it records. HLAE hands the
frames to an encoder process instead and records in real time. Start-up (~25 s warm) and the seek to each take
(~3 s) are now the fixed costs.

Two more things mattered as much as the backend:

- **One slow-motion play must not set the frame rate of a batch.** The first full match was recorded at
  75 fps because one play had a 0.4× segment: 19,668 frames, 21 minutes. Plays are now grouped by the rate they
  need.
- **Do not record what nobody will watch.** With the curation (see `docs/metrics.md`) a match gets
  first-person videos for its 5–7 featured plays instead of all 20–40: one session of about two minutes.

## Measured (in-game, `startmovie`)

Same play each time: a 17.4 s clip with a slow-motion segment, so the capture runs at 60 frames per game
second (1148 frames, handles included). Game files warm in the OS cache.

| | TGA frames, focus sleep on (before) | TGA frames, focus sleep off | **JPEG frames, focus sleep off (default now)** |
|---|---|---|---|
| Peak capture rate | ~21 frames/s | 52 frames/s | 40+ frames/s |
| Sustained capture rate | 6–9 frames/s | 7.6 frames/s (stalls up to 34 s) | **31 frames/s, no stalls** |
| Raw data on disk | 3.7 MB/frame | 4.2 GB for the take | **70 MB for the take (60× less)** |
| Game process alive | — | 211 s | **64 s** |
| Whole job (capture + HUD + bumpers) | — | 4 min 12 s | **1 min 39 s** |
| Re-render of a new cut from the stored master | — | — | **26 s, game not started** |

What this showed:

- **The unfocused-window sleep was real**: with `engine_no_focus_sleep 0` the peak rate went from ~21 to 52
  frames/s. It did not help the average, because…
- **…disk was the sustained limiter**: 1148 uncompressed TGA frames are 4.2 GB; on a DRAM-less SATA SSD the
  write cache runs out after ~1.5 GB and the game stalls for tens of seconds at a time. It also kept the game
  alive ~20 s after the last frame.
- **JPEG frames remove the limiter.** `startmovie <name> jpg wav` writes ~61 KB per 720p frame. Side by side
  at 2× zoom the result is indistinguishable from the TGA capture after the final H.264 encode. JPEG is now
  the default (`options.extra.ingame_frames: "tga"` keeps the lossless path).
- **Start-up is now the largest fixed cost**: ~26 s from launch to the first recorded frame when warm
  (~47–60 s cold), against 37 s of actual recording for 17 s of 60 fps footage.

Earlier baseline, for reference (30 fps, TGA, focus sleep on): an 8.6 s single-take clip took 2 min 15 s
cold; a 30 s three-take clip took 1 min 44 s warm.

## Done

| Change | Effect |
|---|---|
| Capture and composition are separate stages; captures are kept as **masters** with handles | Trims, speed changes within the recorded frame rate, captions, HUD and re-branding never relaunch the game (see [branding.md](branding.md)). Measured: a new cut re-rendered in 26 s with in-game recording disabled. |
| **JPEG frames by default** | Sustained capture 7.6 → 31 frames/s; raw data 60× smaller; whole job 2.5× faster. |
| `+engine_no_focus_sleep 0` at launch and at the first tick | Peak capture ~21 → 52 frames/s with the window in the background. |
| Camera aimed 8 ticks before a take starts (and again on its start tick) | Accepted by the game; no wrong point of view seen in the recordings checked. Not isolated as a measurement. |
| Slow motion recorded at `fps / speed` (up to 240) | Real frames for slow motion. Verified at 60 and 75 fps captures. |
| Bumpers rendered once per brand and cached | Zero marginal cost per video. |
| Composed frames are copied out of the Skia surface with `readPixels` into one reused buffer (was `toarray().tobytes()`, ~100 ms per 1080x1920 frame) | Retime + HUD of a 12 s 1080x1920 body: 56–61 s → 17 s on the same busy machine (Python CPU 38 s → 12 s), with the speed ramp, freeze-frame and push-in added in the same change. The push-in resamples with Pillow's Lanczos (~40 ms per zoomed frame); Skia's cubic resamplers took ~0.8 s per frame on the CPU. |
| Reels share one game launch | One job, many takes. |

## Plan, ranked by expected gain per unit of effort

0. **One game session per match — implemented, not yet measured.** `POST /matches/{id}/produce` records every
   play that has no footage in a single `capture_match` job (one take per play and point of view, 1080p at
   the output frame rate, handles included; plays in slow motion are recorded apart at the rate they need, so
   one of them does not multiply the frames of the whole batch — the first full match was recorded at 75 fps
   for a single 0.4x segment and took 21 min instead of ~9) and queues the videos afterwards. Expected cost per extra play: a seek and its recording.
1. **Coalesce queued in-game jobs of the same match into one game session** (M). Still useful for renders
   requested one by one. With disk out of the way,
   the ~26–60 s start-up is the dominant cost per job; each additional clip should cost a seek and its
   recording instead. Needs a short coalescing window in the queue and per-job results from a shared session.
2. **Stream frames into the encoder while recording** (M). A watcher thread encodes each frame as soon as
   the next one exists and deletes it, so encoding overlaps recording and disk use stays bounded on long
   reels. Less urgent now that frames are JPEG (reka-ai's renderer does this on Linux with TGA).
3. **Instrumentation** (S): `-condebug` plus timestamps for launch, first plugin line, first frame, last
   frame, encode start — in the job result. Everything below depends on knowing the split.
4. **Trim the tail** (S): the 64-tick post-roll and the wait for a graceful quit cost ~5–7 s per job.
5. **One forward pass instead of a reset per take** (M). The plugin's `go_to_next_sequence` seeks to tick 0
   before every take; chaining forward-only jumps in one playback avoids it. Risk: replay loops when a seek
   lands behind an action — keep the current path as fallback.
6. **Keep the game alive between jobs** (L): the plugin accepts a `playdemo` message over its WebSocket and
   reloads the actions file. Est. 20–40 s saved per job; stability over long sessions unknown.
7. **Deterministic, lighter video settings** (S): write our own `cs2_video.txt` into the private config
   (e.g. MSAA 2x, medium shadows). Gain unknown; matters at 1080p60 and makes output identical across machines.
8. **GPU encoding** (S): `h264_nvenc` / `hevc_vaapi` in the stream encoder. x264 is not the bottleneck at
   720p30; this mostly frees CPU for the game.
9. **HLAE backend on Windows — done.** Real-time recording; see the table at the top. Still open there:
   its motion-blur sampler, and recording several takes without returning to tick 0 between them.

Rough throughput: **measured** ~36 jobs/hour today at one clip per launch (1 min 39 s each, 60 fps capture);
**estimated** 200–350 ten-second clips/hour per GPU once clips are batched per match.

## At scale

There is no "demo-only" engine: the full client is the renderer, so scale means more game instances.

- One CS2 + one Steam account per VM or container; nobody we could find runs two instances on one machine.
- The queue already supports it: workers advertise capabilities, so `demoreel worker -c ingame` on GPU
  machines pulls recording jobs while a cheap box does analysis, 2D renders and all composition.
- reka-ai rendered a 10k-demo dataset on Linux with this same technique (actions file + plugin +
  `startmovie`), with Steam as an offline service on Xvfb and watchdogs for hangs, loops and crashes.
- What the largest commercial service (Allstar) publishes about its fleet is essentially nothing. Their
  public traces point to queue-lag autoscaling (NATS + KEDA), MP4 on cheap object storage behind a cheap
  CDN, and they state ~30 min turnaround for CS2 clips. Two things worth copying from them: capture a clean
  master and do branding afterwards (done here), and run a smoke render after every CS2 update with a public
  status line (not done).

## 2D renderer and bumpers

- 2D replay: roughly real time on a desktop CPU; it has not needed attention.
- Bumper generation: ~1 s per frame during the animated part on a busy machine (motion blur renders five
  sub-frames). It runs once per brand; if it ever matters, the backdrop gradient and the per-frame Python
  post-processing are the costs to attack.
