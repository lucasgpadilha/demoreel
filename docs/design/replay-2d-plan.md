# Replay 2D: design plan

Working notes for the redesign of the `radar` renderer (`src/demoreel/render/radar/`) and the shared kill feed
(`src/demoreel/render/killfeed.py`). Kept up to date with what was tried and rejected.

## Subject, audience, job

- **Subject**: one play of a Friday-night CS2 mix, seen from above.
- **Audience**: the players themselves, on a phone, usually inside a social app, sound off. They know the map,
  the nicknames and every icon of the game's HUD by heart.
- **Job of the frame**: say who did what, where on the map, in what order, in the notation the game already
  taught them. First-person footage shows how it felt; the 2D replay shows how it was *built*: the route, the
  angles, who was where.

What was rejected by the owner (and confirmed by rendering it): a square radar next to a dark dashboard
panel. Caps labels, text weapon names, plain dots, uniform circles for smoke and fire, a footer paragraph.
It is an admin panel recorded as video.

## Concept: "o traçado" (the trace)

The site draws a play in **time** as a tape with a mark at every kill. The 2D replay draws the same play in
**space**: the player's route stays on the map as a thin cream line, and every kill is pinned where it
happened (a line from the muzzle to the body, a numbered X). By the last second the camera pulls back and
the map holds the whole play as one tactical drawing. That drawing is the signature: it is the one thing a
top-down view can show that footage cannot, it is the closing frame, and it is the poster.

Everything else is quiet and borrowed from what players already read without thinking, the game's spectator
HUD, redrawn with the brand's slabs and type: scorebug top centre, the two rosters at the edges, the game's
kill feed top right, the watched player's plate.

## Tokens

All colours come from the brand kit (`brand_for(...)`); nothing is hard-coded to one client.

| Role | Sample kit | Used for |
| --- | --- | --- |
| background | `#0b0a09` | the void around the map, slabs (at 80 to 88 % alpha) |
| map ramp | `surface` to a warm grey | the radar image, re-toned to one hue so every map belongs to the brand |
| text | `#f3eee7` | names, clock, the trace, weapon icons |
| muted | `#9a9186` | dead players, secondary numbers |
| ct / t | `#4f95d9` / `#e08a3c` | side, and nothing else: markers, HP bars, names in the feed, score cells |
| accent | `#e08a3c` | the play itself: kill numbers, the kill counter, the label slab |
| negative | `#e5484d` | bomb timer, low HP, the red outline of the watched player's kills |

Type: Barlow Condensed ExtraBold for numbers (score, clock, HP, kill numbers), Barlow Condensed SemiBold for
names, Barlow SemiBold for the one sentence a clip may carry (the caption). Names are never transformed.
Fallback fonts per glyph for emoji and symbols.

Shape: slabs lean at `SLANT = 0.36` like the bumpers and the first-person HUD. Rows are square, the inner
end of a roster row is cut on the same lean. No rounded cards, no drop shadows except a text shadow where
type sits directly on the map.

## The frame

The map is full bleed. The camera decides what part of it is on screen; the HUD floats on top and defines a
*focus rectangle* (the area it does not cover). The camera frames the action inside that rectangle, so one
mechanism serves every aspect ratio and both camera modes (`full` = fit the whole radar in the focus
rectangle).

16:9 (unit = h / 720)

```
[logo]            Time B  5 | 1:23 | 1  Time A             kill feed
                     ▰▰▰▰▱   round 7   ▰▰▱▱▱                 rows
 roster                                                    roster
 (left team,              M A P                           (right team,
  5 rows)                                                    5 rows)
 [nameplate 3 s]        ▰ ▰ ▱ ▱   caption
```

9:16 (unit = w / 540; text stays out of the top 220 px and bottom 340 px of 1080x1920)

```
 ~~~~~~~~ map bleeds under the app's own UI ~~~~~~~~
      Time B 5 | 1:23 | 1 Time A
 [logo]         round 7        kill feed (3 rows)
                 M A P
          (focus: the middle band)
 [nameplate 3 s, then  ▰ ▰ ▱ ▱  and the label]
 roster left           roster right
 ~~~~~~~~ map bleeds under the caption area ~~~~~~~~
```

1:1 and 4:5 (unit = short side / 720): scorebug top, feed under it on the right, rosters as two columns at
the bottom, the nameplate and then the kill counter in the band just above them.

The watched player's team is always the left roster and the left score, as in the first-person HUD.

## Motion

One orchestrated moment per clip, the rest answers the game:

- **Open**: the camera starts on the whole map and pushes in to the player in about a second (where on the
  map are we), while the nameplate (name, "Clutch 1v3", weapon) slides in exactly like the first-person one.
- **Follow**: the camera frames the watched player *and* who he is fighting. It knows the future (the demo is
  all there), so it starts widening before the enemy appears and is smoothed without lag.
- **Kill**: time dips for a few frames and catches up (the clip keeps its length, so the site's tape still
  lines up), a small zoom punch, a ring and the X stamped at the body, the kill line, the feed row, the pip.
- **Slow motion** (`segment.speed < 1`): letterbox bars close in (same as the first-person HUD), the map
  dims outside a pool of light around the action, tracers and ghosts linger.
- **Close**: the camera pulls back to the whole drawing; the label punches in over the pips; if the round
  ended inside the clip the scorebug counts the round.

## Review against the brief (what a first draft would have got wrong)

- **Radar in a box + panel** is the generic "replay viewer". Replaced by a full-bleed map with a floating HUD.
- **A rounded-card kill feed with text** is the web default. The feed is the game's: dark translucent row,
  side-coloured names, the weapon icon, marks in the game's order, red outline for the watched player.
- **Middle dots, caps labels, a title paragraph**: gone. The only words on screen are names, the label the
  HUD family already uses ("Clutch 1v3", "3 kills"), the round, and a caption if the editor wrote one.
- **Colourful Valve radar as is**: each map has its own palette (Nuke is teal and sand, Inferno is slate),
  which fights the brand and the side colours. Re-toned to a single warm ramp; side colours are then the
  only saturated things on screen.
- **Uniform circles for utility**: smoke is a layered, soft, slowly turning cloud with a countdown ring;
  fire is a cluster of flickering additive flames; HE and flash are short bursts.
- **A progress bar** (the site's tape, copied): rejected. In a video it is a social-media cliché and it would
  be a second signature. The kill counter pips of the first-person HUD already tell "2 of 4".

## Log

What was tried and changed while building, judged on frames of three real demos (Inferno, Nuke with
its two floors, Anubis) at 1280x720, 1920x1080, 1080x1920 and 1080x1080.

Kept as planned: full-bleed map, the trace as closing frame and poster, spectator scorebug and rosters, the
game's kill feed, the first-person HUD's nameplate / pips / caption / letterbox.

Changed after looking at it:

- **Zoom by distance, not by map.** A fixed zoom relative to the radar image made Nuke (7 units per pixel)
  look twice as far away as Inferno. The follow camera now shows about 2100 world units across the shorter
  side of the focus rectangle, widens only when the fight needs it, and leaves room in the direction the
  player looks. `extra.zoom` still forces a zoom relative to the whole map.
- **Map ramp lifted.** The first ramp kept the map within 20 % of the background: the picture was a dark
  blob. Floor now sits near a third of the way from surface to muted, with a quarter of the original hue.
- **Smoke** went from a bright ball to a greyer, mottled cloud; two layers drift against each other instead
  of rotating (rotation is skia's slow path and gave nothing).
- **View cone** as three flat wedges instead of a gradient: reads the same at video speed and is crisper.
- **Tall layout**: the nameplate covered the watched player in the first seconds. It moved into a band
  directly above the rosters, where the kill counter appears once the plate has left; the focus rectangle
  ends above that band and starts under the first rows of the kill feed (3 rows there instead of 5).
- **4:5** uses the square arrangement (a feed post has no app UI on top of it), only narrower than 0.7 is tall.
- **Round end**: the tab under the clock turns into the result in the winner's colour ("Bomba desarmada"),
  the winner's score pulses, the bomb clock turns green when defused. The kill counter's label no longer
  fades before the end: the last frame is the result.
- **Bomb timer and defuse as a race**: red fuse along the bottom of the clock, blue defuse progress along
  its top. The game hides the timer; in a replay it is the tension.
- **Title cards** lost the opening "match" card (intro bumper + match card + play card was five seconds
  before any action). One card per play: which play of the reel (pips), the notation, the name, "Round 7, Nuke".

Rejected along the way:

- **Numbers or initials inside the markers** (suggested in the brief): at 720p the inside of a marker is 12 px.
  The marker's rim is the health gauge instead, and identity is the name above it; a name that would collide
  with another is left out.
- **Armour and money in the rosters**: noise in the middle of a round. Rows carry health, weapon, bomb or kit.
- **Blind mark at the end of the feed row** (as listed in the brief): the game puts it before the attacker
  and the airborne mark before the weapon. Followed the game.
- **Hit-stop that lengthens the clip**: would break every video-time to tick mapping (the site's tape).
  Game time freezes 0.10 s and is paid back over the next 0.55 s; the HUD keeps running meanwhile.
- **A grid shader, mipmapped or RGBA textures, gradient shaders over the frame**: each cost 50 to 100 ms per
  frame in skia's raster backend. The ground is one opaque texture in the surface's pixel format (map,
  plotting-table crosses and background baked in), light and smoke are small sprites.
- **Sound bed** (ticks for shots, an impact per kill): not built. A 2D clip stays silent between the bumpers
  rather than ship something untested on phone speakers.

Performance (shared desktop, 30 fps output, 30 s clip with three segments): 1280x720 went from 16 to 20
frames per second to about 47; 1920x1080 from 7.5 to 27; 1080x1920 runs at 28. `RenderResult.details`
carries `seconds` = {load, draw, encode} for the next person who measures.

Structure of `src/demoreel/render/radar/`:

```
renderer.py   the job: plan clips and frames, sequence cards and segments, pipe to ffmpeg
layout.py     where things go per aspect ratio (pure)       timeline.py  frame -> tick, hit-stop (pure)
camera.py     the planned, smoothed follow camera (pure)    data.py      what is read from the demo
world.py      the map layer: utility, bomb, shots, trace, players
hud.py        scorebug, rosters, nameplate, kill counter, caption, title card
art.py        re-toned radar texture and sprites            theme.py     brand -> colours, type, words
canvas.py     slabs and easing
../killfeed.py  the game's kill feed (shared with the first-person layouts) and the text helper `Type`
```

Known gaps (data the analysis does not carry) are listed in the hand-off report, not here.
