# Metric and highlight definitions

Source of truth: [`analysis/stats.py`](../src/demoreel/analysis/stats.py) and
[`analysis/highlights.py`](../src/demoreel/analysis/highlights.py). All values are computed from the demo;
warm-up, knife rounds and rounds before a scoreboard reset are excluded.

## Rounds and teams

- A **round** is delimited by `round_end` events that have a winner. Its window runs from the round start to
  the "officially ended" tick, so kills after the round was decided still belong to it (flagged
  `after_round_end`).
- **Teams** are persistent across side swaps: `team_a` is the team that started as CT, `team_b` started as T.
  Team names come from the clan tags in the demo when both exist and differ, else `Team <best player>`.
- **Pistol rounds**: round 1 and the first round after the first side swap (round 13). **Overtime**: rounds after 24.
- **Incomplete demos**: a demo can miss whole stretches of a match (recording started late or stopped, or the
  file is a fragment). The scoreboard stored in the demo is then the source of truth: rounds keep their real
  numbers and scores, `rounds_in_match` says how many were played, and a warning lists the missing rounds.
  Player stats and highlights cover only the rounds that are in the file.
- **Buy type** per side, from average equipment value at freeze-time end: `eco` < $1000, `semi` < $2500,
  `force` < $4000, `full` ≥ $4000 (`pistol` on pistol rounds).

## Player stats

| Stat | Definition |
|---|---|
| Kills / deaths / assists | Enemy kills only (team kills and suicides are tracked separately). Flash assists count as assists. |
| Damage / ADR | Damage to enemies **capped at the health actually removed** (a 121-damage headshot on a 100 HP player counts 100), per round played. |
| HS% | Headshot kills / kills. |
| KAST | % of rounds with a **K**ill, **A**ssist, **S**urvival, or a death that was **T**raded. |
| Trade | A kill on an enemy who killed one of your teammates in the previous 5 seconds. |
| Opening kill / death | First enemy kill of the round. |
| Multi-kills | Rounds with exactly 2, 3, 4 or 5+ enemy kills. |
| Clutch | The player became the last one alive on a team of ≥ 2 while `N` ≥ 1 enemies were alive (1vN). Won if their team won the round. |
| Accuracy | Enemy-damaging bullet hits / bullets fired (grenades and knives excluded). An approximation: one pellet-spread shot can register several hits. |
| Utility | HE and fire damage to enemies; grenades thrown; enemies/teammates flashed for ≥ 1 s; total enemy blind time. |
| Rating | Approximation of HLTV Rating 2.0 using the public reverse-engineered formula: `0.0073·KAST + 0.3591·KPR − 0.5329·DPR + 0.2372·Impact + 0.0032·ADR + 0.1587`, with `Impact = 2.13·KPR + 0.42·APR − 0.41`. It tracks HLTV closely but is not their number. |

When a demo lacks `player_blind` events, flash data is rebuilt from the `flash_duration` player property
around each flashbang detonation (validated to match the event when both exist).

## Highlight detection, scoring and curation

One candidate per (round, player) that got two or more kills, won a clutch, or made a kill people clip
(noscope, through smoke, wallbang, while blind, airborne, knife, zeus, collateral). A **ninja defuse** (bomb
defused with terrorists alive) is its own highlight.

### Score (0–100)

```text
score = (feat + impact + style) × economy × stakes × outcome − penalty
```

Every term is stored with the highlight (`breakdown`), so a person or an agent can see why a play ranks
where it does.

- **feat — what was done.** Kills: 1 → 3, 2 → 9, 3 → 17, 4 → 26, 5 → 34. Clutch won: 1v1 +3, 1v2 +9, 1v3 +15,
  1v4 +20, 1v5 +25.
- **impact — how much it changed the round (0–40).** For every kill, how much the team's chance of winning
  the round rose, summed and scaled by 40. The chance comes from the players alive on each side and whether
  the bomb is planted: a logistic on the man advantage, steeper when few players are left (5v4 ≈ 0.70,
  3v2 ≈ 0.72, 2v1 ≈ 0.85, 1v2 ≈ 0.15, 1v3 ≈ 0.03; the plant shifts it towards the terrorists). So finishing
  off the last enemy five against one is worth almost nothing, the same kill one against two is worth a lot,
  and winning a 1v3 is the full 40. It is an approximation meant to rank plays: map, side, economy and
  positions are ignored.
- **style — how it was done (capped at 16).** Headshot +1.2 · wallbang +3 · through smoke +3 · while blind +4
  · airborne +4 · sniper noscope +5 · zeus +6 · knife +9 · HE +2 · long range (≥ 30 m, not sniper) +1.5 ·
  deagle headshot +1.5 · pistol outside pistol rounds +1 · opening kill +1 · pace
  `0.8 × kills × max(0, 4 − average seconds between kills)` · two kills on one tick (collateral) +6.
- **economy.** The two teams' buys that round (eco < semi < force < full): rifles against an eco ×0.78, one
  step better ×0.93, even ×1, one step worse ×1.08, an eco beating rifles ×1.2.
- **stakes.** Pistol round +0.08 · overtime +0.12 · match point (a team on 12 or more) +0.12 · close score
  (≤ 2 apart) from round 13 on +0.08 · the round that ended the match, for the side that won it, +0.06 · a
  blowout (8+ apart) −0.08. Capped at ×1.3.
- **outcome.** Round won ×1 · round lost ×0.72 (×0.82 for a clutch attempt that got two or more kills).
- **penalty.** Clips longer than 40 s −4; −1.5 per extra segment (kills far apart). At most −6.
- An **ace** never scores below the tier-S line.
- A **single kill people clip** never scores below its floor, whatever the round: knife 30 · zeus 26 · sniper
  noscope 24 (not at point-blank range, under 5 m) · airborne 22 · while blind 22. Before this rule 22 of 46
  such kills in twenty matches were dropped.
- A 1v1 clutch counts +8 instead of +3 at match point or in overtime.
- **Almost a clutch** (`clutch_attempt`): left alone against three or more and one kill short of winning.
  It is a play of its own, with outcome ×0.92 instead of ×0.82.
- A **ninja defuse** by a player who already has a play in that round (usually the clutch itself) is added to
  that play (tag, title, +8 per terrorist alive) instead of being lost.

Candidates under 20 are dropped; at most 40 per match are kept, but a clutch, a clutch attempt, an ace or a
ninja defuse is never dropped by that cap (a 46-round overtime game used to lose its overtime clutches).

### Tier

`S` ≥ 62 (a play of the match) · `A` ≥ 45 · `B` ≥ 30 · `C` below.

### Curation: which plays get first-person videos

Recording with the game is the expensive step, and a list of forty clips is not a highlight reel. So the
analysis marks a few plays per match as **featured**; `produce` makes first-person videos for those and 2D
replays for everything.

1. For each team: its best plays of tier B or better, up to **3**, at most 2 by the same player and one per
   round (so the quota is not one player's whole evening).
2. A team with nothing that good still gets its single best play, so both sides appear.
3. **Every tier-S play**, whatever the quota — a match full of great plays gets more videos.
4. At most 10 in total (the best are kept). `featured_rank` 1 is the play of the match.

Anyone can overrule it: `PATCH /highlights/{id} {"featured": true|false}` (MCP: `set_featured`). A manual
choice survives re-scoring. `POST /matches/{id}/rescore` (MCP `rescore_match`, CLI `demoreel rescore`)
applies the current model to matches analyzed earlier without reading the demo again; highlights keep their
ids and videos.

On the twenty matches this was calibrated on, the model keeps 22–40 plays per match and features 5–7.

### Replay from the victim's eyes

The only edit that needs extra footage, so it is rationed: at most **3 per match**, only for featured plays
of **tier S**, **one kill each**. The kill is the first of the play that went through a wall or through smoke,
was a noscope, was made blind or in the air, or was a knife / zeus kill; if there is none, the last kill of a
clutch or an ace; other plays get no replay. It is stored as a last segment with `role: "replay"`: the
victim's point of view from 3 s before the kill to 0.5 s after it, at half speed, captioned with the victim's
name. That is about 3.5 s of extra recording per replay; because of the slow motion those plays are recorded
at 60 fps.

### Clip boundaries

Measured on the 1047 segments of twenty matches, 78 % of all clip time was fixed pre- and post-roll and the
exchange that leads to the first kill starts 0.3 s before it (median; 1.8 s at the 90th percentile). So clips
are cut around the *exchange*, not a fixed time around the kill:

- `KillEvent.fight_start_tick` is where the exchange began for the attacker: walking back from the kill
  through his shots and the hits he dealt or took, for as long as two of them are at most 2 s apart (10 s at
  most).
- **Start**: 1.5 s before the exchange, never less than 2.5 s nor more than 5.5 s before the first kill.
  Segments entered by a cut: 1.0 s before the exchange, between 1.5 and 4.0 s.
- **End**: 2.0 s after the last kill of the clip; 1.2 s after the last kill of a segment that is followed by a
  cut (long enough for the kill counter); 0.4 s after the player's own death if he dies sooner.
- Kills more than **9 s** apart start a new segment. Up to there the wait stays in the clip: half of it is the
  player repositioning, and the video plays those quiet stretches at 3–4× instead of jumping over them (the
  speed ramp of the composition stage, [branding.md](branding.md#editing-what-composition-does-with-the-footage);
  it goes by `action_ticks`, the ticks at which the player shot, hit or was hit). While that ramp did not
  exist the limit was 6 s. On the 118 featured plays of the twenty matches, going from 6 s without a ramp to
  9 s with it leaves the median body where it was (10.6 s) with fewer cuts (2.1 segments per play instead of
  2.4); 43 % of those plays have a stretch that runs fast, and the ramp takes 2.8 s out of each (median).
- **A clutch won by defusing** ends on the defuse: the wait between the last kill and the defuse is cut and
  the last 2.5 s of the defuse are shown (the video HUD shows the bomb clock, then "defused"). Won on the
  clock or by the explosion, the clip ends with the last kill.

On the same plays this makes the body of the videos about a third shorter (Anubis sample: 455 s → 293 s; the
1v3 of that match 25.8 s → 15.7 s), and every new cut fits inside the footage already recorded.

Re-analysing a match (`POST /matches/{id}/reanalyze`) keeps the ids and videos of its plays; a play whose cut
changed gets a new `version`, so its videos are reported `stale` and `produce` renders them again. A cut that
a person or an agent edited is never touched by the model.

## Fails

The moments friends roast each other for (`analysis/fails.py`). They are stored like highlights with
`category: "fail"` — never mixed into lists of plays unless asked (`?category=fail`) — and scored on the same
0–100 scale, comparable only among fails.

| Kind | What | Detection | Shown from |
|---|---|---|---|
| `fail_whiff` | emptied the magazine at somebody close, hit nothing, died | ≥ 8 bullets (≥ 2 with a sniper rifle) in the 5 s before dying to an enemy, no damage dealt, killer at ≤ 12 m, not through smoke | the player |
| `fail_team_flash` | blinded a team-mate who was killed while blind | a team-mate blind ≥ 1.5 s killed by an enemy within the blindness (4 s at most); the **thrower** is the one blamed | the blinded player |
| `fail_self_flash` | the same with his own flash | | the player |
| `fail_teamkill` | killed a team-mate during the round | kill flagged as team kill, before the round ended | the killer |
| `fail_own_grenade` | died to his own grenade or fire | suicide by HE / molotov / incendiary | the player |
| `fail_fall` | fell to his death | suicide by `trigger_hurt` | the player |
| `fail_bomb` | died to the bomb | | the player |
| `fail_knife_out` | killed holding the knife while everybody had guns | victim's active weapon at death, outside pistol rounds | the player |

Several players "dying" on the same tick is a restart and is ignored. At most 12 fails per match are kept;
the two funniest (score ≥ 40, different players) are **featured** and get first-person videos. Measured on
twenty matches: 2.0 close-range whiffs and 2.6 fatal team flashes per match.

## Chat

`analysis.chat` keeps every all-chat line with the round, a **kind** and, when it comes within 20 s of a
kill, the kill it reacts to and how the author relates to it (`victim`: he just died; `killer`; `other`).

Kinds come from a word list tuned on Brazilian mix chat (`analysis/chat.py`), first match wins: `command`
(starts with `!`, `.` or `/`) · `toxic` (swearing at people) · `whine` (cheat accusations, luck, the game
being unfair) · `laugh` · `praise` · `gg` · `talk`. `tags` lists every kind that applies, plus `shouting`.
It is banter material for a "biggest cry-baby of the night" ranking, not moderation: it misses sarcasm and
anything it has no word for. A short list of slurs is always masked when a line is shown.

Each highlight carries the lines that react to its kills (`chat`, at most three, crying first); videos show
them over the last seconds of the clip.

All of this is configurable in code (`HighlightConfig`); exposing it per request is on the roadmap.

## Across matches: rivalries, trophies, chat rankings

Source of truth: [`insights.py`](../src/demoreel/insights.py). Nothing here reads a demo: it is all added up
from the stored analyses, so it covers every match in the database. REST: `GET /api/v1/insights/nights`,
`/insights/rivalries`, `/insights/players`, `/insights/players/{steamid}`, `/insights/trophies`,
`/insights/chat`. MCP: `list_nights`, `get_rivalries`, `get_player_profile`, `get_trophies`,
`get_chat_rankings`.

### Scope and nights

- Every endpoint takes the same filters, combined with AND: `since` / `until` (when the match was played;
  its upload time when the source gave no date), `night=YYYY-MM-DD`, `match_id`. No filter = every analyzed
  match. An unknown match or a night without matches is an error that lists the valid nights; an empty
  `since`/`until` window is just empty.
- A **night** is every match played from noon to noon, local time: the night of a match is the local date
  twelve hours before it was played, so a game at 02:00 on Saturday belongs to Friday. Local time is
  `DEMOREEL_TIMEZONE` (an IANA name such as `America/Sao_Paulo`), else the server's time zone.
- **Bots** (`BOT:<name>`) are in no ranking, pair, podium or trophy. A player is his SteamID; the name shown
  is the one from his most recent match in the scope.

### Rivalries

- A **duel** is a kill of one human on an enemy human. Team kills and suicides are not duels; kills after
  the round was decided are (they are on the scoreboard too).
- A **pair** has the kills of each on the other, `duels` (their sum), `diff` (leader − trailer; the leader
  is listed first, ties by lower SteamID), `imbalance = diff / duels` (0 even, 1 one-way), `matches` (matches
  in which the two were on opposite teams, with or without kills between them), the **opening kills** of each
  on the other and the **humiliations** of each on the other (knife or Zeus kills).
- Pairs with fewer than `min_duels` kills between them are left out. Default: two per match in the scope,
  never under 3 nor over 10 (3 for one match, 10 for a season).
- Sort: `duels` (default: the most kills between them, then `diff`), `diff`, `imbalance`; then SteamIDs.
- Per player: the **nemesis** ("carrasco") is the opponent with the best balance against him (his kills on
  the player minus the player's kills on him, positive; ties: the one who killed him more, then lower
  SteamID) and the **favourite victim** ("freguês") the mirror of it - both only among opponents with at
  least `min_duels` kills between them.

### Player profile

Totals over the matches of the scope the player was in: matches, wins / losses / draws (his team's result),
rounds, K / D / A, HS% (headshot kills / kills), damage, opening kills and deaths, trade kills, multi-kills,
clutches won / attempted / by size, team kills, team-mates and enemies flashed, knife / Zeus kills and
deaths, top five weapons. **By weapon** (`weapon_stats`): every weapon he killed with, from the same kills as
the scoreboard (no suicides, no team kills), with kills, headshot kills, HS% of that weapon and its share of
his kills; most kills first, ties by name. **ADR** is total damage / total rounds; **KAST** and **rating** are the per-match
values averaged with rounds played as weights. Best plays (top 5 by score) and worst fails (top 3) come from
the stored highlights, so they carry ids; fails are also counted by kind. Nemesis and favourite victim as
above, chat numbers as below, and the list of his matches, newest first.

### Trophies

For **one match or one night** (with neither, the most recent night). Fourteen trophies; a trophy whose
minimum nobody reached is not given, a player can win several, and each comes with up to two runners-up.
Ties go to the tie-break in the table, then to the lower SteamID, so the same matches always give the same
winners.

| Key | Title (pt / en) | Winner | Minimum | Tie-break |
|---|---|---|---|---|
| `mvp` | MVP | Highest rating (weighted by rounds) | Played half the rounds of whoever played the most | More kills |
| `clutch_king` | Rei do clutch / Clutch king | Most clutches won | The clutches won add up to 2 enemies (one 1v2 or two 1v1) | More enemies beaten, fewer attempts |
| `entry` | Abre-alas / Entry fragger | Most opening kills | 3 | Fewer opening deaths |
| `kill_streak` | Imparável / Unstoppable | Longest run of enemy kills without dying, inside one match (it survives round changes, not the match) | 5 | The earlier match, the earlier round |
| `drought` | Seca / Dry spell | Most consecutive rounds played without an enemy kill, inside one match | 5 | The earlier match, the earlier round |
| `low_hp` | No talo / By a thread | Most kills made on 10 HP or less | 2 | The lowest HP |
| `team_flasher` | Cegador de amigos / Friendly flasher | Most team-mates blinded for ≥ 1 s | 5 | Fewer flashbangs thrown |
| `friendly_fire` | Fogo amigo / Friendly fire | Most team-mates killed | 1 | — |
| `sharp_aim` | Mira calibrada / Dialed in | Best accuracy (bullet hits / bullets fired) | 150 bullets | More bullets |
| `whiff` | Mira descalibrada / Stormtrooper | Most `fail_whiff` fails (a magazine at close range, nothing hit, dead) | 2 | Fewer rounds played |
| `knife_out` | Faca na mão / Knife out | Most deaths holding the knife (killed by anything but a knife), outside pistol rounds, while the round was live | 2 | Fewer rounds played |
| `baiter` | Baiter | Most lost rounds in which he was the last one alive of his team and had no kill in the whole round | 3 | Fewer rounds played |
| `economist` | Economista / Economist | Least money spent per kill | 8 kills | More kills |
| `headshot_machine` | Máquina de headshot / Headshot machine | Highest HS% | 10 kills | More headshot kills |

`low_hp` and `knife_out` need `attacker_health` / `victim_weapon` on the kills and `whiff` needs the fails:
matches analyzed before those existed simply give no such trophy until they are re-analyzed.

### Chat rankings

A ranking **for banter between friends, not moderation**. Lines are classified by the word list in
[`analysis/chat.py`](../src/demoreel/analysis/chat.py); it misses sarcasm and anything it has no word for,
and a word can land in the wrong category. Only all-chat is in the demos.

- A line counts for every category whose words it has (`kkk lixo` is both a laugh and swearing). Server
  commands (`!`, `.`, `/`) are counted apart and are not messages. Analyses stored before the chat model
  are classified on the fly.
- Per player: `messages` (every line typed, with `messages_per_round`), `commands`, and the count and the
  rate per round played of `whine` ("chorão": cheating accusations, luck, complaining), `toxic` ("boca
  suja": swearing), `laugh` ("risadinha"), `nice` ("gente boa": praise or gg), `shouting` (all caps) and
  `death_cries`.
- **`death_cries`** ("choro pós-morte") is the heart of the cry-baby ranking: lines of kind `whine` or
  `toxic` written within 20 s of the author's own death. `cried_about` is whose kills caused the most.
- **Podiums**, in this order: `toxic`, `whine`, `death_cry`, `talker`, `laugh`, `nice`, `shouting`. Each is
  the three players with the most lines, at least 2 (1 when the scope is a single match); ties by rate per
  round, then lower SteamID. Each entry quotes one or two lines - for crying and swearing the ones written
  right after dying first, then the longer ones - always through `chat.display_text`, which masks slurs.
- **`talker`** ("tagarela") is who types the most: it ranks by `messages`, every line whatever it says
  (server commands excluded), ties by `messages_per_round`. It quotes no lines.
