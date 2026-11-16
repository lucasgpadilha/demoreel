# demoreel web UI: design plan

Working notes for the redesign of `src/demoreel/web/`. Kept up to date with what was tried and rejected,
so the next pass does not repeat it.

## Subject, audience, job

- **Subject**: the highlights site of a Friday-night CS2 mix (first instance: Mix da Vila, about 20 friends).
- **Audience**: the players themselves, on a phone on Saturday morning, looking for *their* play and a link to
  paste in the group chat. Second audience: the organizer, once a week, importing the night's demos.
- **Job of the page**: put a play on screen within a second of landing, say who did what in the words players
  use (1v3, 4K, varou a smoke), and make the link shareable. Everything else is secondary.

## Concept: "a fita" (the tape)

Counter-Strike is already a notation system: a match is a strip of rounds, a round is a strip of kills, a clip
is a strip of segments. The UI draws all three with **one device, a tape with marks**, at three zoom levels:

1. **Play tape**: the clip's segments end to end with a mark at every kill. It is the video scrubber on the
   play page, the editor's timeline, and a thin fingerprint under every poster (four kills in 3 s looks like a
   burst; a 1v3 over 48 s looks like a long, lonely strip).
2. **Round tape**: the classic two-row round history (top row = rounds won by the left team, bottom row = the
   right team), coloured by the side that won, with the plays pinned to their rounds and an economy lane.
3. **Night**: matches are grouped by the night they were played (a match at 03:30 on Saturday belongs to
   Friday), each one a scorebug row carrying its round tape.

The tape is the single signature. Everything around it is quiet: no cards, no panels, no decoration.

## Tokens

Brand colours arrive from `GET /api/v1/brand` at runtime and are written to CSS custom properties. The CSS only
knows roles. The values of a sample kit are shown; a neutral built-in theme (graphite + sand) applies when there is no brand.

| Role | Name | Sample kit | Neutral default | Used for |
| --- | --- | --- | --- | --- |
| `--bg` | Preto quente | `#0b0a09` | `#101316` | the page; footage sits directly on it |
| `--surface` | Carvão | `#1a1714` | `#181c20` | things that float over the page: dialogs, sticky bars, table heads |
| `--text` | Creme | `#f3eee7` | `#e8ebee` | text, kill marks, the winner's score |
| `--muted` | Fumaça | `#9a9186` | `#8e979f` | secondary text, the loser's score |
| `--accent` | Laranja | `#e08a3c` | `#d9c7a0` | "you can act here": primary button, focus ring, now-playing |
| `--ct` / `--t` | Azul CT / Laranja TR | `#4f95d9` / `#e08a3c` | `#5b9bd8` / `#e0a13c` | side encoding on tapes and kill feeds, nothing else |

Colour rule: **on a tape or in a kill feed, colour means side. Outside them, orange means action.** Blue is
never a link or a button. Positive/negative are reserved for job states and +/- columns.

Type (both self-hosted, OFL, given by the brand kit):

- **Barlow Condensed 700**, tabular: notation and scores (`13:11`, `1v3`, `4K`). These are the only large type
  on the site. The notation is content, not a label, so it carries the display role instead of headlines.
- **Barlow Condensed 600**: player names in lower thirds, section headings (sentence case, never tracked caps).
- **Barlow 400/500/600**: everything else, 16/1.5, lines under 72 characters.
- Scale: 13, 15, 16, 19, 24, 32, 48, 72, 112 px (roughly 4:5 steps, widening at display sizes).
- Player names are never transformed (no uppercase): they are the players' own typography (`♿`, `Ca$tigo`).

Shape: footage, tapes and rows are square. Controls get a 2 px radius. No shadows, no gradients except the
legibility scrim over video.

Alignment: everything is left aligned on a 12-column grid, max 1320 px. The only centred object is a scoreline,
because a score is symmetrical by nature.

Motion: one moment only. On the home stage the best play runs (muted) and the playhead crosses the kill marks;
each kill-feed line lights as its mark is crossed. No entrance animations anywhere. Hovering a poster previews
its video (answering the pointer). `prefers-reduced-motion` turns off autoplay and previews.

## Layout

Home, desktop:

```
[MV] Mix da Vila                         Jogadas  Partidas  Jogadores      Importar partidas
+--------------------------------------------------------+  +------------------------------+
|                                                        |  | [thumb] 1v3 tonho        0:26|
|                 the best play, running                 |  |         Anubis, round 8      |
|  Jogo real                                             |  | [thumb] 4K  tem pao de queijo|
|                                                        |  | [thumb] 1v2 biruta           |
+--|-----------||---|------------------------------------+  | [thumb] 4K  biruta           |
 1v3  twitch.tv/trovoadinha              Nuke 13:3 round 7  | [thumb] 3K  Juca Faisca      |
      Clutch de AWP: 4 kills, 1 headshot                    | ...                          |
      trovoadinha  AWP  ZED      0:04                       | Ver todas as jogadas         |
      trovoadinha  AWP  Ca$tigo  0:16     Abrir jogada      +------------------------------+

Partidas
sexta, 13 de novembro
Anubis    Time A  13  ||||||||||||  ||||||||||||  11  Time B        22 jogadas
Nuke      Time B  13  ||||||||||||  ||||          3   Time A        19 jogadas
Inferno   Time B  13  ||||||||||||  |||           2   Time A        22 jogadas

Jogadores
Juca Faisca 14   Krvz 12   twitch.tv/trovoadinha 9   biruta 8   tonho 6 ...
```

Home, 375 px: the stage is full bleed, the lower third stacks under it, the rundown becomes a list, each match
row wraps to two lines (scoreline, then tape).

Match:

```
Anubis, sexta, 13 de novembro
Time A   13 : 11   Time B          Intervalo em 5:7. Time A virou no segundo tempo.
 o     o  o        o   o o  o      <- plays pinned to rounds
 ||| | ||  ||    | || |||| | |     <- rounds won by Time A (blue = as CT, orange = as TR)
    | |  ||  ||||  |    |   | |    <- rounds won by Time B
 ..:.::.:::.:  .:.:::.:::::        <- economy lane (equipment value per side)
 Round 8, 3:4. CT eliminou os TRs em 0:49 ...   kill feed, clutch, plays of the round
Melhores jogadas (posters)   Todas as jogadas (rows; "Gerar vídeo" where there is none)
Placar (two sortable tables, a row expands to the player's detail)
Vídeos e reel
```

Play:

```
Anubis  Time A 13:11 Time B   round 8
+----------------------------------------------+   1v3
|                    video                     |   tonho
+----------------------------------------------+   Clutch 1v3 com 4 kills, de CT
 > 0:07 [=====|=======][==|===|==========|===]     tonho  Desert Eagle  hs  ♿       0:03
 Jogo real | Replay 2D                             tonho  AK-47             biruta   0:12
 Copiar link   Baixar vídeo   Editar corte         headshot, de longe, round pistol
```

The editor opens under the player on the same page (`#/play/<id>/edit`): one row per segment (trim nudges at
both ends, speed, camera, caption, split, remove). Every action is applied at once; the current video is then
marked as out of date with one button to render again.

## Principles

1. Footage first: every screen that has a video shows it above the fold, without a frame around it.
2. Say it the way players say it: `1v3`, `4K`, "varou a smoke". Sentences instead of label/value pairs.
3. One device, the tape. If something is a sequence in time it is drawn as a tape; if it is not, it gets no
   decoration at all.
4. Always say what kind of video it is: "Jogo real" (first person, filled tag) or "Replay 2D" (outlined tag).
5. Honest states: a renderer that is switched off is shown as switched off, with the server's reason; a video
   older than its cut says so.

## Review against the brief (what the first draft got wrong)

The first draft was the page I would produce for any "video highlights" prompt, so these parts were replaced:

- **Headline hero + grid of rounded video cards** (a streaming-site clone, tell no. 4). Replaced by the stage
  and rundown: the hero *is* a play, and the list next to it is a broadcast running order, not a card grid.
- **Stat tiles** (big number, small label) for K/D, ADR, rating. Dropped. The scoreline is the only big number
  and it is part of the scorebug; player numbers live in the scoreboard table.
- **Round timeline as a row of dots inside a card**. Replaced by the two-row round history CS players already
  read fluently, extended with play pins and an economy lane so it carries the story.
- **Tracked caps eyebrows and kind badges** ("CLUTCH", "QUAD KILL"). Replaced by the notation itself set large
  (`1v3`, `4K`); section headings are plain sentence case.
- **Meta joined by middle dots** ("de_anubis · R8 · 25.8s"). Replaced by short sentences ("Anubis, round 8").
- **Orange used as decoration** (gradient washes, glowing borders). Orange is the client's colour and also the
  T side, so it is rationed: side on tapes, action elsewhere.
- **Chamfered scorebug** echoing the logo's cuts. Cut before building: it would hard-code one client's logo
  shape into a themeable product, and it was a second signature competing with the tape.
- **Monospace for numbers**. Not used; Barlow's tabular figures do the job.

## Log: what changed while building

Checked with screenshots of the real instance (three analyzed matches, nine rendered plays, one of them game
footage) at 1280 px and 375 px, in pt-BR and en, with a brand kit and with the neutral theme.

Kept as planned: stage + running order, the three tapes, sentences instead of labels, square footage.

Changed after looking at it:

- **Kill feed of a play lost the attacker column.** Every line repeated the play's author and truncated the
  victim on phones. A play's feed is now "time, weapon, victim"; the full attacker/weapon/victim line stays in
  the round view, where attackers differ. The weapon forms a centre spine there, like the HUD.
- **The victim is struck through when the playhead crosses the kill**, instead of dimming the lines that have
  not happened yet (dimmed lines read as disabled before the video starts).
- **Rows without a video show the play's tape where the thumbnail would be.** The first build drew an empty
  outlined box, which looked unfinished. The tape is real information and keeps the rows aligned.
- **Lower-third token reduced** from 112 px to about 90 px: at 1280 it left no room for a long nickname
  (`twitch.tv/trovoadinha`) beside it.
- **Organizer strip on the home page removed.** The header already carries "Importar partidas"; a tinted box at
  the bottom was a card for its own sake. Same for a second import button on the matches page.
- **Headshot dropped from the tag sentence**: the kill feed already marks HS per kill, and a lone "Headshot."
  under a 1v3 was noise.
- **Import page**: list constrained to one readable column, older nights folded behind one button, rows
  without a demo lose their checkbox, and the import button sticks to the bottom of the viewport (the list of a
  community with 100 matches is long).
- **Play page order on phones**: video, then who/what/kill feed, then the buttons. The first build put the
  buttons between the video and the description.
- **Copy**: "ready in seconds" was wrong for the 2D renderer (about a minute for a 13 s clip here); it now
  says so. A hand-made cut says "Corte feito à mão" instead of "editado, versão 1".

- **Renders in progress got a "Cancelar" button** (play page and reel). A two-play reel took about three
  minutes on this machine; a progress line with no way out was not acceptable.
- **Reels are created with a title in the reader's language** ("Melhores jogadas, Nuke 13:3") instead of the
  server default "Match highlights".

Verified by doing it on the live instance: create a play from a round, render it (2D), trim / split / change
speed / caption, see the video flagged as out of date, delete video and play; build a two-play reel, watch it,
delete it; switch between game footage and 2D on the 1v3; jump to kills from the kill feed.
Not exercised: a real import from the community site and a real .dem upload (both would have added matches to
the instance), the API-key prompt (this server has no key), `prefers-reduced-motion` (no way to emulate it in
the pane; handled in code and CSS).

Rejected along the way:

- Weapon icons in the kill feed: the recognisable ones are Valve's artwork. Weapon names in text instead.
- A sticky header: the page is about footage, and the header is one tap away at the top of short pages.
- An overlay (modal) for the play: a play is the thing people link to, so it is a page with its own URL.
- A separate "reel" page: the reel plays inside the match page, next to the form that builds it.

Structure of `src/demoreel/web/`:

```
index.html            shell (header, main, footer are filled by app.js)
styles/base.css       tokens, type, shell, controls        styles/parts.css  tapes, player, posters, rows
styles/views.css      screen layouts
js/app.js             boot, brand, router, drop-anywhere    js/api.js   HTTP client, upload, job watcher
js/data.js            cached reads, feed/brand/sources fallbacks
js/plays.js           notation, sentences, timeline maths   js/parts.js tapes, posters, rows, kill feed
js/player.js          video player with the tape scrubber   js/render.js renderer choice, job progress
js/brand.js           brand kit -> CSS custom properties    js/i18n.js + js/locales/{pt-BR,en}.js
js/views/             home, lists (plays, player, roster, matches), match, play (+ editor), import
fonts/                Barlow + Barlow Condensed (woff2, latin + latin-ext) and OFL.txt
```

Known gaps (API side) are listed in the hand-off report, not here.

## Second pass: POV first, three videos per play

What the owner asked for after seeing the first build, and what was decided.

- **"Jogo real" is now "POV"** in both languages. The badge grammar is unchanged: POV is solid, the 2D replay
  is an outline. A video that is not 16:9 says so in its badge ("POV 9:16").
- **A play has variants, not a list of renders.** `ingame-16x9` (POV), `ingame-9x16` (POV vertical, "pronto
  para postar") and `radar-16x9` (Replay 2D) are always offered; `radar-9x16` and 1:1 appear when they exist.
  Each variant shows its newest fresh video; older versions sit under "Mais opções deste vídeo".
- **The switch is a row of tabs above the player**, in the same place for every layout. Each tab carries a
  small frame with the proportions of the video (wide or tall; solid for POV, outline for 2D), so "vertical"
  is seen before it is read. A variant with no video is a dashed tab: picking it shows an empty frame of the
  right shape with one button, "Gerar POV vertical". That replaced the radio list of renderers.
- **A vertical video gets a vertical player.** Desktop: the player takes the height of the window
  (`(100svh - 200px) * 9/16` wide, about 340 x 600 at 1280 x 800) and the notation, kill feed and buttons move
  beside it. Phone: it takes the full column width up to the height of the screen (343 x 610 at 375 x 812),
  no letterbox. Rejected: boxing the 9:16 picture inside the 16:9 frame (a postage stamp), and a modal
  "phone preview".
- **Link and file follow the variant on screen.** The link carries `?view=<variant>` unless it is the one the
  play opens on. Watching the vertical, the primary button is "Baixar vertical para postar"; watching any
  other variant, the same download is still one press away when a vertical POV exists. Files are named
  `player-notation-map-variant.mp4`.
- **POV is never greyed out because `/capabilities` says recording is off.** The request is sent; a refusal
  is shown with the server's own `detail`, next to the variant it was for.
- **Posters, rows and the home stage use the POV 16:9, else the 2D 16:9, never a vertical.** The feed sends
  one video per play, so the client swaps it for the POV when the play has one.
- **The tape survives the brand intro.** New renders start with a bumper, so the play no longer begins at
  0:00. The API does not say where it begins; the extra length is split 2.0 : 2.8 (the server's default intro
  and outro) and the kill marks are shifted by that much. The bumpers show as gaps at both ends of the tape.
- **Match page: "Gerar os vídeos de todas as jogadas".** A dialog says what it costs (the whole match is
  recorded in CS2 on the server machine, several minutes, game open), lets the organizer untick POV / 2D and
  16:9 / 9:16, then two progress lines read from the match's jobs: the recording, then "Vídeos prontos: 5 de
  24". The button only exists when `/openapi.json` lists the endpoint. Reels can now be 9:16 too.

Checked on the live instance at 1280 x 800 and 375 x 812: a real POV 9:16 (1080 x 1920, rendered from the play
page with recording switched off on the server, so the "ask anyway" path is proven), a 2D 9:16, the empty
vertical frame, switching by tab and by link (`?view=`), the editor on a vertical variant, posters and the
home stage picking the POV 16:9. The production dialog and its progress were run once with 2D + 16:9 only
(no game): the server queued 7 renders and all 7 failed inside the radar renderer, which is how the "N
falharam. O servidor diz: ..." ending came to exist. A full run with POV (the game open for minutes) was not
started from here.

Still open on the API side: where the play starts inside a video with bumpers (the tape guesses), and cache
headers for the static UI (a tab can keep running yesterday's modules after an update).

## Third pass: curation (destaques, notas)

The API now grades every play (score 0-100, tier S/A/B/C, a breakdown) and marks the featured ones
("destaques": the best of each team plus anything tier S). Product rule: featured plays get POV videos, the
others stay as 2D replays on purpose.

- **Visual language, no badge.** The grade is type: tier letter and score in the condensed face ("S 79"),
  muted for B and C, text colour for A, the accent only for S. It sits where the eye already goes for
  secondary facts: beside the duration in rows, at the end of a poster caption, large in the play page.
  The curation word leads the where-line: "Jogada da partida" (rank 1, accent) or "Destaque". In a list
  where every play is featured (the home running order, the "Destaques" section of a match) "Destaque" is
  dropped, so the word is not repeated down the page; "Jogada da partida" always stays.
  Rejected: tier pills on posters, a star icon, a coloured frame around featured posters.
- **Home.** The stage and the head of the running order are the featured plays (`/feed?featured=true`); the
  other plays follow under one plain separator, "Outras jogadas".
- **Match.** "Destaques" as posters in rank order with the team named ("Time B, round 7"), then "Outras N
  jogadas" as the compact list, each row with one quiet action, "Promover a destaque". A match from before
  curation (no tiers) keeps its single list and says how to grade it ("Reavaliar as jogadas").
- **Play.** "Por que esta nota": the grade, then short sentences built from the breakdown and the tags,
  pluses first and minuses after ("Ganhou um 1v3 com 4 kills", "Decidiu o round, que estava praticamente
  perdido", "Contra eco", "Round perdido"). No table of factors. It ends with the play's status and the one
  action that changes it (promote / remove), with what that does to its videos.
- **Tabs follow the rule.** A play that is not featured is offered the 2D replay only; there is no dashed POV
  tab asking to be rendered as if something were missing. Videos that already exist are always shown.
- **Production dialog.** States the rule with the real counts ("Os 5 destaques ganham POV... Todas as 29
  jogadas ganham replay 2D"), the cost of recording, and keeps "POV de todas" plus the renderer / format
  ticks behind "Opções avançadas".

## Fourth pass: the client's own look comes from the kit; visitors; embedding

The product is embedded in the community's site, so on a client instance it has to look like that site.
The rules of this plan did not change: they describe the **built-in theme**, the one used with no kit or
with a kit that has no `ui` block. What changed is that a kit may now switch on things this plan forbids
for the built-in theme, because the client's site does them:

| Option in `brand.ui` | Built-in | What it turns on |
| --- | --- | --- |
| `display.transform: "uppercase"`, `tracking`, `weight`, `number_weight` | sentence case, 600/700 | caps display type with tracking, weight 800 |
| `label.transform`, `tracking`, `size` | 13 px, sentence case | small caps labels on table heads and form labels |
| `surface.style: "panel"`, `padding`, `accent_edge` | flat page | bordered panels, a 3 px team-coloured edge on the scoreboards, a sunken hero for the scoreline |
| `meta_separator: " · "` | ", " | facts joined by a middle dot |
| `score.separator: " : "`, `score.winner: "accent"` | "13:3", winner in the text colour | "13 : 3", the winner's number in the accent |
| `date_format: "dd/MM · HH:mm"` | nights by name | the site's own date pattern |
| `motion: "expressive"` | no entrance animation | staggered entrance of the page blocks, lift and shine on the primary button, zoom on poster hover |
| `button.*`, `radius`, `header.*`, `row.*`, `container.*`, `toast`, `colors.*` | this plan's values | the site's buttons, zero radius, header with border and a two-tone name, row dividers with an accent bar on hover, 1280 px container, toasts top right, web-only tones |
| `strings.<locale>.<key>` | this plan's copy | the site's vocabulary ("abates" for kills) |

All of it is off by default and belongs to the kit, never to `src/`. Every value is a measure, a weight, a
colour, or one of a closed set of words, validated in `js/brand.js`; a kit cannot inject CSS. The variables
and their built-in values are the first block of `styles/kit.css`.

Three things stay as this plan says even with a kit, on purpose:

- **The tape** (play tape, round tape) has no equivalent on the client's site and is what demoreel adds.
- **Footage has no frame.** Panels wrap lists and text; the player and the posters sit on them unframed.
- **Notation and nicks are never transformed.** "1v3" is not "1V3", and `Ca$tigo` stays as typed, also where
  the client's site would put a nick in caps.

Not taken from the client's site (yet): stroke icons instead of filled ones, avatars and map photos (the API
has neither), the centred footer, the toast with a title and a timer bar.

### Where each fact lives now

The UX audit found the same data on several screens. One home each:

- All matches: `#/matches`. Home shows the matches of the last night only, with one link to the rest.
- All players: `#/players` (with a search by nick). Home has no roster.
- All plays: `#/plays`, a page at a time ("Mostrar mais"). Home shows up to 10 featured plays of the last
  night ("Destaques de sexta"), or of every night when the last one has no videos yet.
- The play on the home stage is described under the video; its row in the list says only "Tocando agora".
- The kill feed of a play: its page. On the home stage it shows on wide screens only.
- On a play page the trail gives map, score and round; the line under the name adds only side and date.
- The score of a match: the headline. The scoreboard captions carry the team name only.
- A player's numbers per match: the scoreboard of that match. The player page links to it with one sentence
  per match instead of rebuilding the table.
- A player's plays: `#/player/<id>`. `#/plays?player=` redirects there.

### Visitor and organizer

`GET /session` says whether this browser may change things. Everything that changes something (import,
render, cancel, promote, edit, reels, re-score, re-analyze, delete) is drawn for the organizer only; a
visitor gets the watching, the link and the download. The entrance is one quiet link in the footer, "Sou
organizador". "Ver como visitante" lets the organizer check the public view. Videos that are queued or
rendering say so ("Na fila", "Gerando…") on posters, rows and tabs instead of "Sem vídeo".

### Embedded

`?embed=1`: no header or footer, the menu as a row of links in the content, visitor mode, height and route
reported to the host page, tall players sized against the host's window. See `docs/embedding.md`.

## Fifth pass: zoeira, chat, rivalries, trophies

The group plays together every week; half of what they want from the site is to laugh at each other. The
analysis now finds fails, reads the all-chat, counts who kills whom and hands out trophies. The front tells
all of it in the same voice as the plays: type first, a sentence a player would say, no icons, no emoji.

### New routes

- `#/zoeira?night=&kind=`: the fails that have a video, as posters, filtered by night ("Temporada" or one
  night) and by kind of fail. While the batch is still rendering and fewer than six have a video, the
  funniest fails of the night follow as lines that say where their video is ("Na fila", "Gerando..."),
  so the page is never thin or empty. One link under the lede, "Rankings do chat", goes to the chat page
  of the same night.
- `#/chat?night=`: what was typed in all-chat, for the season or one night. First the podiums (boca suja,
  chorão, choro pós-morte, tagarela, risadinha, gente boa, gritaria): three names, how many times, and a
  line or two they wrote ("tagarela" counts messages and quotes nothing). Then "Todo mundo": one table
  with every player and every count, sortable by any column, most messages first. On a phone the table
  scrolls sideways inside its own box with the names held in place. The rankings used to sit under the
  posters of Zoeira, where nobody found them; they are on this page only.
- `#/rivalidades?night=&sort=&player=`: each pair as a scoreline between two people ("trovoadinha 55 : 38
  biruta"), with matches together, opening duels and knife kills in one line under it. Three orders: most
  duels, closest, biggest beatings. `player=` narrows it to one person's duels.
- `#/night/<date>`: one night. Its trophies, its matches, and links to the zoeira, the chat and the
  rivalries of that night, with older / newer.

The header and the embedded tabs gain "Zoeira", "Chat" and "Rivalidades". On a phone six sections do not
fit, so the menu is one line that slides sideways and keeps the current section in sight.

### Changes to existing screens

- Play page, fail: same page, other telling. The token is the fail ("0/14", "FLASH", "TK", "FACA"), the
  line says what happened, one more line gives the detail that makes it funny (metres, seconds blind, whose
  eyes the video shows). No kill feed and no "Por que esta nota": a fail has no grade. The organizer still
  chooses which fails get a POV. The header marks "Zoeira", not "Jogadas".
- Play page, play: "No chat" under the kill feed, the lines typed right after the play. Crying and swearing
  carry a red edge, laughing and praise a green one; "vítima" / "quem matou" says which side of the kill
  the author was on.
- Match page: "Zoeira" after the plays (featured fails as posters, the rest folded) and "Troféus da
  partida". Fails stay out of Destaques and out of "Outras jogadas".
- Player page: one call to `/insights/players/<id>`. A sentence with the season, the plays, "Carrasco e
  freguês" as two scorelines plus the other main duels, the fails by kind, the chat habits in one sentence,
  one line per match.
- Home: unchanged except two links over the night's matches, "Troféus da noite" and "Zoeira da noite".
- `#/plays`: the count line says "24 de 473 jogadas" when the server sends the total.
- Tapes: a replay segment is hatched, on posters, rows, the scrubber and the editor, where it is named
  "Replay (visão de <vítima>)" and its kills are not counted twice. Kill marks on the scrubber come from
  `kill_marks` of the render when present (exact, whatever bumpers and speed changes the video has); the
  older estimate from the segments remains for renders without them.
- A visitor no longer reads "corte antigo" on a video: only who can render it again needs to know.

### Trophies and podiums as type

A trophy is a title, a line saying what it measures, who took it, the number in the display face, one line
of detail and the next two. No cup, no medal, no emoji. With the client's panel surface each one sits in a
small panel with the accent edge; in the neutral theme they are separated by a rule only.

### Notes for whoever continues

- What a fail was (shots fired, who was flashed, by whom he died, for how long) reaches the front only as
  English prose in `title` / `description`; `failFacts()` in `js/plays.js` reads it with regular
  expressions. Structured fields on the highlight would let that go.
- `/feed` has no `night` parameter; the front turns a night into `since` / `until` from `/insights/nights`.
- Titles of trophies and chat podiums come from the API in both languages; everything else is in
  `js/locales/`.
