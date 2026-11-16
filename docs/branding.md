# Brand kits, bumpers and the video HUD

demoreel is white-label by design: the videos and the web UI carry the identity of the community that runs
the instance, not ours. That identity is a **brand kit** — a folder with a small JSON file and a logo.

## A brand kit

`<data_dir>/brands/<slug>/brand.json` (kits are user data; they are never committed to this repository):

```json
{
  "name": "Mix da Vila",
  "tagline": "CS2 com os amigos, toda semana",
  "url": "https://mixdavila.example",
  "logo": "logo.png",
  "logo_background": "#0b0a09",
  "colors": {
    "background": "#0b0a09", "surface": "#1a1714", "surface_raised": "#231f1b", "border": "#3a342d",
    "text": "#f3eee7", "muted": "#9a9186", "accent": "#e08a3c", "accent_strong": "#d9772a",
    "positive": "#6fcf97", "negative": "#e5484d", "ct": "#4f95d9", "t": "#e08a3c"
  },
  "fonts": { "display": "Barlow Condensed", "body": "Barlow" },
  "locale": "pt-BR",
  "bumper": { "style": "assemble", "intro_seconds": 2.0, "outro_seconds": 2.8, "sound": true, "cta": "mixdavila.example" },
  "music": null,
  "watermark": true,
  "chat": true
}
```

- `logo`: a square-ish PNG. Transparency is used when present; otherwise the colour in `logo_background`
  (or the corner colour) is keyed out. Flat logos with a few colours work best.
- `music`: optional audio file in the kit folder. It is looped under the whole video, ducked under game
  sound, and faded out. No music ships with demoreel — bring a track you have the rights to.
- `chat`: show what the lobby typed about a play at the end of its first-person video (see
  [Editing](#editing-what-composition-does-with-the-footage)). On by default; `false` for a community that
  would rather not.
- The active kit is `DEMOREEL_BRAND=<slug>`, or simply the only kit present. Matches imported from a
  [source](#match-sources) get that source's `brand`.
- `GET /api/v1/brand` exposes the kit (with `logo_url`); the web UI themes itself from it.

## Looking like the community's own site: `ui`

The web UI is meant to be embedded in the community's site, so a kit can carry a `ui` block with the
conventions of that site. It is read only by the web UI (`GET /api/v1/brand` → `ui`) and never changes a
video, so editing it does not invalidate bumpers or masters.

```json
"ui": {
  "colors": { "on_accent": "#0b0a09", "surface": "#131110", "border": "#2b2621", "muted": "#8d857b" },
  "display": { "transform": "uppercase", "weight": 800, "tracking": "0.025em" },
  "radius": "0",
  "surface": { "style": "panel", "padding": "24px" },
  "header": { "border": true, "name_accent": "MIX" },
  "button": { "font": "display", "weight": 800, "hover": "lift" },
  "row": { "divider": true, "hover": "bar" },
  "score": { "separator": " : ", "winner": "accent" },
  "meta_separator": " · ",
  "strings": { "pt-BR": { "count.kills": { "one": "{n} abate", "other": "{n} abates" } } },
  "embed": { "share_base": "https://example.com/highlights/" }
}
```

Every key is optional and unknown keys are ignored; `web/js/brand.js` is the reference for what is read
and `web/styles/kit.css` for what each option does. Without a `ui` block the built-in theme applies (see `docs/design/frontend-design-plan.md`). Embedding, the
visitor / organiser split and share links are described in [embedding.md](embedding.md).

## What a kit changes

| | Without a kit | With a kit |
|---|---|---|
| Web UI | neutral built-in theme | the kit's colours, logo, name, language |
| Intro / outro | none | generated bumpers, see below |
| In-game videos | HUD in neutral colours | HUD in the kit's colours with its mark |
| 2D replay | "demoreel" corner text | the community's name |

Per render you can switch pieces off:
`options: {"intro": false, "outro": false, "music": false, "hud": false, "ramp": false, "chat": false}`.

## Generated bumpers

Nobody has to open a motion-design tool. `compose/bumper.py` renders the intro and the outro from the kit:

1. **The logo is split into pieces automatically** (`compose/logo.py`): background keyed out, pixels
   clustered into the logo's flat colours, each colour split into connected shapes. A typical esports mark
   yields its letters / emblem as "main" pieces and its secondary-colour shapes as "accents".
2. **Intro ("assemble")**: the main pieces swing in from their own side with perspective and motion blur;
   the most central piece drops from above and lands on an **impact** — flash, shockwave ring, sparks,
   camera shake, chromatic aberration; the accent pieces ignite white-hot and cool to their colour; a glint
   crosses the mark; the name is revealed over an accent rule; the camera punches through the logo into the
   footage.
3. **Outro**: arrives on a flash from the footage, the assembled mark lands, the call to action (`cta`, or
   the site URL) and tagline appear, and it fades to black.
4. **Sound is synthesised** on the same timeline (`compose/sfx.py`): riser, a whoosh per piece, a sub-drop
   impact with a metallic ring, a tick per accent, a falling sweep on the cut. No samples, nothing to license,
   and picture and sound cannot drift.

Bumpers are rendered once per kit / resolution / frame rate and cached in `<data_dir>/cache/bumpers/`.
Changing anything in the kit (or the logo file) invalidates the cache. The first render after that pays
for it (tens of seconds; more on a busy machine).

**Vertical videos open cold.** In 9:16 the brand is on screen the whole time (header and footer), and two
seconds of bumper before the play is where short-video viewers leave, so the intro is skipped there and the
first kill arrives about 2.5 s in. `"bumper": {"intro_vertical": true}` in the kit, or `"intro": true` on a
render, brings it back.

The intro is deliberately short (2 s by default). Commercial clip services front-load 5 s or more of
sponsor and bumper before the play; viewers leave. Put weight on the outro and the HUD instead.

## The HUD on first-person footage

The game's own HUD is hidden while recording (only the kill feed stays), and `compose/hud.py` draws:

- **corner mark**: the logo, both team names and the score as it stood during the round, and the round;
  once the bomb is planted, the **bomb clock** counts down next to it and turns into "defused" (the game's own
  HUD is hidden, and without it nobody knows why a clutch is in a hurry);
- **nameplate**: the player and, under the name, **what is at stake**: `1v3 · 4 : 3` (alone against three, and
  the score while the round was played, his team first), `Match point · 12 : 11`, `Prorrogação · 15 : 15`,
  `Pistol · 0 : 0`. A round with nothing special falls back to what the clip is and the weapon
  (`3 kills  AK-47  2 HS`). It slides in at the start and is gone after two seconds, before the first kill;
- **kill counter**: one mark per kill of the clip; each fills and pops on its kill with `2/4` above it;
- **the play's label** (`ACE`, `Clutch 1v3`, `3 kills`): lands on the decisive kill — the last one — together
  with the freeze-frame, and stays through it. A clip with a single kill has no counter but still gets its
  label when it has one (`Clutch 1v1`);
- **chat**: up to three lines the lobby typed in reaction to the play, one slab per line (`name: text`, the
  name coloured by the kind of line — crying and swearing in the negative colour, laughing in the accent,
  praise in the positive), entering one by one on the left during the last two seconds of the play;
- **captions** (`caption` on a segment): above the kill counter; in a clip that has a counter or a label they
  sit one lane higher, so the two never overlap, and a caption that would sit on the nameplate waits for it
  to leave;
- **replay**: a segment with `role: "replay"` (the kill again from the victim's eyes, see
  [metrics.md](metrics.md)) shows only a `REPLAY` badge in the corner, its caption (`Visão de <name>`) and the
  letterbox — no nameplate, score, bomb clock, kill counter or kill feed, which all belong to the play and
  leave before the replay starts;
- **letterbox bars** while a segment is in slow motion, and a flash + whoosh on every jump in time.

**Kills through smoke or through a wall** would otherwise look like someone shooting at nothing, so the
recording switches the spectator x-ray (player outlines) on 3 s before each such kill of the player being
followed and off 1 s after it; the rest of the footage stays clean. `extra: {"ingame_xray": true}` keeps the
outlines on for the whole clip, `false` never shows them. This is part of the capture, so changing it
re-records the play.

Texts follow the kit's `locale` (`pt-BR` and `en` today). The shapes lean at the same angle as the bumper
bars, so intro, HUD and outro read as one system.

## Editing: what composition does with the footage

All of this happens in `compose/overlay.py`, from footage that is already recorded — none of it needs the
game, and all of it can be redone with different settings from the same master.

- **Speed ramp.** Half of the time between two kills of a play is the player repositioning. Those stretches
  run at **3×** (4× from 4 s on) instead of being cut out, with the speed gliding up and down over 0.2 s and
  the game sound following (same pitch). Never touched: the lead-in to the first kill, 1 s before and 0.6 s
  after every kill, 0.4 s before and 0.6 s after everything the player did (his shots, his hits, damage he
  took — `action_ticks` of the highlight), everything after the last kill, and any two kills less than 3 s
  apart. A stretch has to be 1.5 s long to count. Only segments at normal speed are ramped, and only when
  the highlight has `action_ticks` (plays analysed before that field existed play in real time until the
  match is re-analysed). There is no flash: nothing is skipped. `options: {"ramp": false}` switches it off.
- **Freeze-frame.** On the decisive kill — the last of the clip, or the only one — the picture holds for a
  third of a second and pushes in slightly while the label lands; the game sound makes room for an impact
  and comes back with the picture, so nothing drifts. It is done by repeating a frame, so 30 fps footage is
  enough.
- **Push-in (9:16 only).** The window of a vertical video is narrow and enemies are small in it, so the
  picture pushes in 1.12× on the crosshair from 0.15 s before every kill and eases back within 0.5 s after it.
- **Replay.** See the HUD above; the cut to it is a jump in time, so it gets the flash and the whoosh.
- **Chat.** `analysis/chat.py` picks the lines typed within 20 s of the clip's kills (the victim's complaints
  first, slurs masked). Shown are lines that read as a reaction — crying, swearing, laughing, praise — and
  anything typed by the player or by somebody he killed; a bystander's small talk is not. `"chat": false` in
  the kit or on a render leaves them out.

Because of the ramp and the freeze, the time of a kill in the video is no longer `(tick - start) / tickrate`.
`compose.overlay.timeline(clips, analysis, fps=…)` is the one description of time that the picture, the
sound and the HUD are made from; `compose_body` returns the kills of the clip as
`[{"tick": 74393, "t": 3.2}, …]` (seconds into the body: add the render's `body_offset_s`), and
`kill_marks(clips, analysis, fps=…, ramp=…, freeze=…)` computes the same list without any footage.

## Shapes: 16:9, 9:16 and 1:1 from the same footage

First-person footage is always recorded at one size (1920x1080 by default, `DEMOREEL_INGAME_CAPTURE_WIDTH` /
`_HEIGHT`) and every shape is cut from it in composition, so asking for the vertical version of a play that
already has a horizontal one never needs the game.

| `options.format` | Size | Layout |
|---|---|---|
| `16:9` (default) | `DEMOREEL_RENDER_WIDTH` x `_HEIGHT` | the whole picture, HUD on top, the game's own kill feed |
| `9:16` | 1080x1920 | made for phones: brand header, the game in a **4:5 window cut from the centre** of the picture (the action is at the crosshair), HUD inside the window, our own kill feed (the game's is outside the cut), call to action below |
| `1:1` | 1080x1080 | centre square, HUD and kill feed on top |

The bumpers are generated per shape as well. In `9:16` the header and footer sit mostly under the areas where
social apps draw their own interface; everything that matters is inside the window.

`POST /api/v1/highlights/{id}/renders {"renderer": "ingame", "options": {"format": "9:16"}}`

## Producing a whole match

`POST /api/v1/matches/{id}/produce {"renderers": ["ingame", "radar"], "formats": ["16:9", "9:16"]}` queues
every play in every requested renderer and shape, skipping videos that already exist. Plays whose footage is
not recorded yet are captured **together in one game session** (a `capture_match` job) and their videos are
queued when it finishes — game start-up is the largest fixed cost, so this is how a match should be made.

## Why edits rarely need the game: masters

Recording with CS2 is the expensive step. Everything after it is cheap, so the two are separate:

```text
capture (CS2, minutes, GPU)  ──►  master  ──►  retime + HUD  ──►  intro + body + outro  ──►  video
      render/ingame               compose/master.py   compose/overlay.py     compose/assemble.py
```

Each capture is kept as a **master** in `<data_dir>/masters/<match_id>/`, recorded with *handles* (1.5 s
before and 1 s after every segment) and, when a segment is slowed down, at a higher frame rate (0.5× at
30 fps is recorded at 60 so slow motion has real frames). A render reuses any master that covers what it
needs — same point of view, tick range inside the footage, enough frames per second:

- trimming, starting up to the handle earlier, splitting, captions, speed changes within the recorded frame
  rate, switching the HUD or bumpers off, or re-branding → **no game launch**; such a render runs on any
  worker, even where in-game recording is disabled;
- a different point of view, ticks outside the footage, or slower motion than was recorded → a new capture
  (or, where the game is not available, the existing footage is used and the job result carries a warning
  that slow motion will be less smooth).

Masters are large (tens of MB per clip). They are deleted with their match; retention policies are on the
roadmap.

## Match sources

A source lets the instance pull matches from the community's own site instead of uploading demos by hand.
It is a JSON file `<data_dir>/sources/<id>.json` — no code needed for a site that lists matches as JSON and
serves one URL per demo:

```json
{
  "name": "Mix da Vila",
  "url": "https://mixdavila.example/partidas",
  "brand": "mixdavila",
  "list_url": "https://mixdavila.example/api/partidas",
  "demo_url": "https://mixdavila.example/api/partidas/{id}/demo",
  "page_url": "https://mixdavila.example/partidas/{id}",
  "fields": { "id": "id", "map": "mapa", "played_at": "data", "score_a": "placarA", "score_b": "placarB", "has_demo": "demo" }
}
```

- `GET /api/v1/sources/{id}/matches` lists what the site has, with the import status here.
- `POST /api/v1/sources/{id}/import {"remote_ids": ["102", "100"]}` queues download + analysis per match.
- Imported matches remember where they came from (`source_id`, `remote_id`, `played_at`, `page_url`).
- The same is available to agents through the MCP tools `list_source_matches` and `import_matches`.

### Be a polite guest

Demos are hundreds of megabytes each and community sites often run on small hosting plans. **A bulk import
through such a site can exhaust its bandwidth quota and take the whole site down** — this happened to the
first site demoreel was used with: after 17 demos (~4.4 GB) pulled back to back, its host started answering
`503 usage_exceeded` for every page. Since then:

- downloads from one source are spaced out;
- the moment a source answers 429 / 503 / "usage exceeded", it is **paused** (6 h by default): queued imports
  fail immediately without touching the site, the import endpoint answers 503 with the reason, and the match
  list is served from the last good copy;
- `GET /api/v1/sources` shows `paused_until` / `paused_reason`; delete `<data_dir>/sources/<id>.paused` to
  resume earlier;
- importing more than five matches at once returns a warning about bandwidth.

If you run the site: do not serve demos through metered web hosting. Put them on object storage with free or
cheap egress and point `demo_url` there, or let demoreel read them from a folder the game server writes to.

Not handled yet: series with several maps per match id, authenticated sources, scheduled sync, and an
up-front size estimate before a bulk import.
