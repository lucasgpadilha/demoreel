"""Automatic highlight detection and curation.

One candidate is produced per (round, player) that did something worth watching: multi-kills, won clutches,
flashy single kills (noscope, through smoke, wallbang, blind, airborne, knife, zeus) and ninja defuses.

**Scoring** answers four questions a viewer would ask, and keeps the answers (``breakdown``) so a person or an
agent can see *why* a play ranks where it does:

* ``feat``   - what was done: how many kills, a clutch and how many opponents it was won against;
* ``impact`` - how much it changed the round: for every kill, how much the team's chance of winning the round
  rose (from the players alive on each side and the bomb). Finishing off the last enemy 5 against 1 is worth
  almost nothing; the same kill 1 against 2 is worth a lot;
* ``style``  - how it was done: headshots, tempo, and the kills people clip (noscope, through smoke, …);
* three multipliers for the circumstances: ``economy`` (rifles farming an eco round count less, a pistol
  beating rifles counts more), ``stakes`` (match point, overtime, a close score late in the game, pistol
  round) and ``outcome`` (a great play in a round that was lost is remembered less).

The result is a 0-100 score and a ``tier`` (S/A/B/C).

**Curation** then picks the plays that deserve the expensive treatment (first-person footage): the best few
of *each team*, never more than two of one player in that quota, plus every stand-out play whatever the
quota. Everything else is still a highlight, shown as a 2D replay.

Each candidate also gets *segments* (tick ranges + POV) that a renderer can turn into video. Kills far apart
in the same round become separate segments so clips stay tight.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import weapons
from .schema import ClutchEvent, HighlightCandidate, HighlightSegment, KillEvent, PlayerRef, RoundSummary

SCORING_VERSION = 2


@dataclass
class HighlightConfig:
    # --- cuts: where a clip starts and ends -----------------------------------------------------------
    # A clip starts shortly before the *exchange* that led to the first kill (the player's first shot or hit),
    # not a fixed time before the kill: measured on 1047 segments the exchange starts 0.3 s before the kill
    # (median), so a fixed 4 s pre-roll was mostly waiting.
    pre_pad_s: float = 1.5  # shown before the exchange starts, first segment
    pre_min_s: float = 2.5  # ... but never less than this before the kill
    pre_max_s: float = 5.5  # ... nor more
    cut_pre_pad_s: float = 1.0  # the same for segments entered by a cut
    cut_pre_min_s: float = 1.5
    cut_pre_max_s: float = 4.0
    post_roll_s: float = 2.0  # after the last kill of the clip
    cut_post_s: float = 1.2  # after the last kill of a segment that is followed by a cut
    death_tail_s: float = 0.4  # if the player dies during the post-roll, the clip ends this long after
    # Kills further apart than this start a new segment. Up to here the wait stays in the clip and composition
    # plays its quiet stretches fast (compose/overlay.py, speed ramp) instead of jumping over them.
    max_gap_s: float = 9.0
    defuse_lead_s: float = 2.5  # of a clutch won by defusing: shown before the bomb is defused
    defuse_tail_s: float = 1.2
    # --- replay from the victim's eyes (the only edit that needs extra footage) -------------------------
    # Only for plays of the match (tier S), one kill each, a few per match: each costs ~3.5 s of recording.
    replays_per_match: int = 3
    replay_lead_s: float = 3.0
    replay_tail_s: float = 0.5
    replay_speed: float = 0.5
    min_score: float = 20.0  # candidates below this are dropped
    max_highlights: int = 40
    # --- scoring ---------------------------------------------------------------------------------------
    kill_feat: dict[int, float] = field(default_factory=lambda: {1: 3, 2: 9, 3: 17, 4: 26, 5: 34})
    clutch_feat: dict[int, float] = field(default_factory=lambda: {1: 3, 2: 9, 3: 15, 4: 20, 5: 25})
    impact_weight: float = 40.0  # points for single-handedly taking a round from lost to won
    style_cap: float = 16.0
    # --- tiers and curation ----------------------------------------------------------------------------
    tier_s: float = 62.0  # a play of the match: featured whatever the quota
    tier_a: float = 45.0
    tier_b: float = 30.0  # also the floor of the per-team quota
    featured_per_team: int = 3
    featured_max: int = 10
    max_featured_per_player: int = 2  # within the quota; stand-out plays ignore it


_KIND_BY_KILLS = {5: "ace", 4: "quad_kill", 3: "triple_kill", 2: "double_kill"}
_LABEL = {"ace": "ACE", "quad_kill": "4K", "triple_kill": "3K", "double_kill": "2K"}
_FLASHY = {"noscope", "through_smoke", "wallbang", "blind_kill", "airborne", "knife", "zeus", "collateral"}
_BUY_RANK = {"eco": 0, "semi": 1, "force": 2, "full": 3}
# A single kill people clip whatever the round: it never scores below this (a knife kill is always a story).
_SINGLE_KILL_FLOOR = {"knife": 30.0, "zeus": 26.0, "noscope": 24.0, "airborne": 22.0, "blind_kill": 22.0}
_ALWAYS_KEPT = {"ace", "clutch", "clutch_attempt", "ninja_defuse"}  # never dropped by max_highlights


# ------------------------------------------------------------------------------------------ round model

def win_probability(own: int, enemy: int, bomb: int = 0) -> float:
    """Rough chance of winning the round with ``own`` players alive against ``enemy``.

    ``bomb`` is +1 when the bomb is planted and this is the terrorists' view, -1 for the CTs' view, 0 before
    the plant. A logistic on the man advantage, steeper when few players are left (5v4 = 0.70, 4v3 = 0.70,
    3v2 = 0.72, 2v1 = 0.85, 1v2 = 0.15, 1v3 = 0.03). It is an approximation meant to rank plays, not a
    prediction: map, side, economy and positions are ignored."""
    if enemy <= 0:
        return 1.0
    if own <= 0:
        return 0.0
    steep = {1: 2.0, 2: 1.1}.get(min(own, enemy), 1.0)
    logit = 0.85 * steep * (own - enemy) + 0.6 * bomb
    return 1.0 / (1.0 + math.exp(-logit))


def _kill_swings(r: RoundSummary) -> dict[int, tuple[float, float]]:
    """For every kill of the round (by index): ``(swing, chance before)`` - how much it raised the attacker's
    team's chance of winning the round, and what that chance was just before it."""
    alive = {"CT": set(r.ct_players), "T": set(r.t_players)}
    out: dict[int, tuple[float, float]] = {}
    order = sorted(range(len(r.kills)), key=lambda i: r.kills[i].tick)
    for i in order:
        k = r.kills[i]
        victim_side = "CT" if k.victim.steamid in alive["CT"] else "T" if k.victim.steamid in alive["T"] else None
        attacker_side = k.attacker.side if k.attacker is not None else None
        if (victim_side is not None and attacker_side in ("CT", "T") and attacker_side != victim_side
                and not k.is_suicide and not k.is_teamkill and not k.after_round_end):
            planted = r.plant_tick is not None and k.tick >= r.plant_tick
            bomb = 0 if not planted else (1 if attacker_side == "T" else -1)
            own, enemy = len(alive[attacker_side]), len(alive[victim_side])
            if own == 0:  # roster unknown for this round: assume even numbers
                own = enemy
            p = win_probability(own, enemy, bomb)
            out[i] = (max(0.0, win_probability(own, enemy - 1, bomb) - p), p)
        if victim_side is not None:
            alive[victim_side].discard(k.victim.steamid)
    return out


def _stakes(r: RoundSummary, before: tuple[int, int], last_round: int, won_round: bool) -> tuple[float, list[str]]:
    """How much the round mattered: 0.92 (garbage time) to 1.3."""
    a, b = before
    m, tags = 1.0, []
    if r.is_pistol:
        m += 0.08
        tags.append("pistol_round")
    if r.is_overtime:
        m += 0.12
        tags.append("overtime")
    elif max(a, b) >= 12:
        m += 0.12
        tags.append("match_point")
    elif abs(a - b) <= 2 and r.number >= 13:
        m += 0.08
        tags.append("close_game")
    elif abs(a - b) >= 8:
        m -= 0.08
    if r.number == last_round and won_round:
        m += 0.06
        tags.append("final_round")
    return min(1.3, m), tags


def _economy(own_buy: str | None, enemy_buy: str | None) -> float:
    """Rifles against an eco round count less; winning fights with the worse buy counts more."""
    if own_buy not in _BUY_RANK or enemy_buy not in _BUY_RANK:
        return 1.0
    diff = _BUY_RANK[enemy_buy] - _BUY_RANK[own_buy]
    return 1.2 if diff >= 2 else 1.08 if diff == 1 else 1.0 if diff == 0 else 0.93 if diff == -1 else 0.78


def _kill_style(k: KillEvent, is_pistol_round: bool) -> tuple[float, list[str]]:
    bonus, tags = 0.0, []
    cls = weapons.weapon_class(k.weapon)
    if k.headshot:
        bonus += 1.2
        tags.append("headshot")
    if k.wallbang:
        bonus += 3
        tags.append("wallbang")
    if k.noscope and cls == "sniper":
        bonus += 5
        tags.append("noscope")
    if k.through_smoke:
        bonus += 3
        tags.append("through_smoke")
    if k.attacker_blind:
        bonus += 4
        tags.append("blind_kill")
    if k.attacker_airborne:
        bonus += 4
        tags.append("airborne")
    if cls == "knife":
        bonus += 9
        tags.append("knife")
    if k.weapon == "taser":
        bonus += 6
        tags.append("zeus")
    if k.weapon == "hegrenade":
        bonus += 2
        tags.append("he_kill")
    if cls == "pistol" and not is_pistol_round:
        bonus += 1
    if k.weapon == "deagle" and k.headshot:
        bonus += 1.5
        tags.append("deagle_headshot")
    if k.distance_m is not None and k.distance_m >= 30 and cls not in {"sniper", "grenade"}:
        bonus += 1.5
        tags.append("long_range")
    if k.is_opening:
        bonus += 1
        tags.append("opening_kill")
    return bonus, tags


def tier_of(score: float, cfg: HighlightConfig | None = None) -> str:
    cfg = cfg or HighlightConfig()
    return "S" if score >= cfg.tier_s else "A" if score >= cfg.tier_a else "B" if score >= cfg.tier_b else "C"


# ---------------------------------------------------------------------------------------------- segments

def _segments(ticks: list[int], pov: str, r: RoundSummary, tickrate: int, cfg: HighlightConfig,
              max_tick: int, onsets: dict[int, int] | None = None, death_tick: int | None = None,
              windows: tuple[tuple[int, int], ...] = ()) -> list[HighlightSegment]:
    """Tick ranges to show for these kill ticks.

    ``onsets`` maps a kill tick to where its exchange began (``KillEvent.fight_start_tick``); without it the
    minimum pre-roll applies. ``death_tick`` is when the player died in the round, if he did. ``windows`` are
    extra ranges to show as they are (a defuse)."""
    gap = int(cfg.max_gap_s * tickrate)
    lo, hi = r.start_tick, min(max(r.official_end_tick, r.end_tick), max_tick)
    groups: list[list[int]] = []
    for t in sorted(ticks):
        if groups and t - groups[-1][-1] <= gap:
            groups[-1].append(t)
        else:
            groups.append([t])
    out: list[HighlightSegment] = []

    def add(start: int, end: int) -> None:
        if out and start <= out[-1].end_tick:
            out[-1].end_tick = max(out[-1].end_tick, end)
        elif end > start:
            out.append(HighlightSegment(start_tick=start, end_tick=end, pov_steamid=pov))

    for gi, g in enumerate(groups):
        first, last = gi == 0, gi == len(groups) - 1 and not windows
        pad, least, most = ((cfg.pre_pad_s, cfg.pre_min_s, cfg.pre_max_s) if first
                            else (cfg.cut_pre_pad_s, cfg.cut_pre_min_s, cfg.cut_pre_max_s))
        lead_s = max(0.0, (g[0] - (onsets or {}).get(g[0], g[0])) / tickrate)
        pre = min(most, max(least, lead_s + pad))
        start = max(lo, g[0] - int(pre * tickrate))
        post = cfg.post_roll_s if last else cfg.cut_post_s
        end = min(max(hi, g[-1] + tickrate), g[-1] + int(post * tickrate))
        if death_tick is not None and g[-1] < death_tick < end:
            end = death_tick + int(cfg.death_tail_s * tickrate)
        add(start, end)
    for a, b in sorted(windows):
        add(max(lo, a), min(max(hi, a + tickrate), b))
    return out


def _weapon_phrase(kills: list[KillEvent]) -> str:
    names: list[str] = []
    for k in kills:
        if k.weapon_name not in names:
            names.append(k.weapon_name)
    return names[0] if len(names) == 1 else " + ".join(names[:2]) + ("…" if len(names) > 2 else "")


# --------------------------------------------------------------------------------------------- detection

def detect(rounds: list[RoundSummary], tickrate: int, total_ticks: int, map_name: str,
           cfg: HighlightConfig | None = None) -> list[HighlightCandidate]:
    cfg = cfg or HighlightConfig()
    out: list[HighlightCandidate] = []
    last_round = rounds[-1].number if rounds else 0  # real round number when the scoreboard is available
    before = (0, 0)

    for r in rounds:
        by_player: dict[str, list[tuple[int, KillEvent]]] = {}
        refs: dict[str, PlayerRef] = {}
        for i, k in enumerate(r.kills):
            if k.attacker is None or k.is_suicide or k.is_teamkill:
                continue
            by_player.setdefault(k.attacker.steamid, []).append((i, k))
            refs[k.attacker.steamid] = k.attacker
        clutch_by_player: dict[str, ClutchEvent] = {c.player.steamid: c for c in r.clutches}
        for c in r.clutches:
            refs.setdefault(c.player.steamid, c.player)
        swings = _kill_swings(r)

        for pid in set(by_player) | {p for p, c in clutch_by_player.items() if c.won}:
            indexed = sorted(by_player.get(pid, []), key=lambda ik: ik[1].tick)
            kills = [k for _, k in indexed]
            n = len(kills)
            clutch = clutch_by_player.get(pid)
            won_clutch = clutch is not None and clutch.won
            player = refs[pid]
            won_round = player.side == r.winner_side
            team = (r.ct_team if player.side == "CT" else r.t_team) if player.side in ("CT", "T") else None

            stakes, ctx_tags = _stakes(r, before, last_round, won_round)
            feat = cfg.kill_feat.get(min(n, 5), 0.0) if n else 0.0
            tags: list[str] = []
            if won_clutch:
                bonus = cfg.clutch_feat.get(min(clutch.versus, 5), 0.0)
                if clutch.versus == 1 and {"match_point", "overtime"} & set(ctx_tags):
                    bonus = max(bonus, 8.0)  # a 1v1 is routine; a 1v1 with the match on the line is not
                feat += bonus
                tags.append(f"clutch_1v{clutch.versus}")
            # Left alone against three or more and one kill short of winning it: a play in its own right.
            near_clutch = (clutch is not None and not clutch.won and clutch.versus >= 3
                           and clutch.kills >= clutch.versus - 1)
            if near_clutch:
                tags.append(f"almost_1v{clutch.versus}")

            impact = cfg.impact_weight * min(1.0, sum(swings[i][0] for i, _ in indexed if i in swings))
            win_before = next((swings[i][1] for i, _ in indexed if i in swings), None)
            if won_clutch:  # the story of a clutch starts when the player was left alone
                win_before = min(win_before if win_before is not None else 1.0, win_probability(1, clutch.versus))
            if won_clutch and n == 0:  # won on the clock or by a defuse nobody saw coming
                impact = cfg.impact_weight * (1.0 - win_probability(1, clutch.versus))

            style = 0.0
            for k in kills:
                b, t = _kill_style(k, r.is_pistol)
                style += b
                tags.extend(t)
            span_s = (kills[-1].tick - kills[0].tick) / tickrate if n >= 2 else 0.0
            if n >= 2:
                avg_gap = span_s / (n - 1)
                style += 0.8 * n * max(0.0, 4.0 - avg_gap)  # fast sprays / flicks are more exciting
                if any(a.tick == b.tick for a, b in zip(kills, kills[1:], strict=False)):
                    style += 6
                    tags.append("collateral")
            style = min(cfg.style_cap, style)

            own_buy, enemy_buy = (r.ct_buy, r.t_buy) if player.side == "CT" else (r.t_buy, r.ct_buy)
            economy = _economy(own_buy, enemy_buy)
            if economy <= 0.8:
                tags.append("anti_eco")
            elif economy >= 1.2:
                tags.append("eco_frag")
            lost_clutch_attempt = clutch is not None and not clutch.won and clutch.kills >= 2
            outcome = 1.0 if won_round else 0.92 if near_clutch else 0.82 if lost_clutch_attempt else 0.72

            if n >= 5:
                kind = "ace"
            elif near_clutch:
                kind = "clutch_attempt"
            elif won_clutch and clutch.versus >= 2:
                kind = "clutch"
            elif n >= 3:
                kind = _KIND_BY_KILLS[n]
            elif won_clutch:
                kind = "clutch"
            elif n == 2:
                kind = "double_kill"
            else:
                kind = "special_kill"

            # A clutch won by defusing ends on the defuse, not on a player staring at the bomb for ten seconds:
            # the wait between the last kill and the defuse is cut. Won by time or by the explosion: the clip
            # ends with the last kill.
            defused_it = (won_clutch and r.defuse_tick is not None and r.defuser is not None
                          and r.defuser.steamid == pid)
            windows: tuple[tuple[int, int], ...] = ()
            if defused_it:
                lead = cfg.defuse_lead_s if kills else 6.0
                windows = ((r.defuse_tick - int(lead * tickrate),
                            min(r.defuse_tick + int(cfg.defuse_tail_s * tickrate), total_ticks)),)
            if kills:
                ticks = [k.tick for k in kills]
            elif defused_it:
                ticks = []
            else:  # clutch won without a kill, on the clock: show the end of the round
                ticks = [max(r.freeze_end_tick, r.end_tick - int(6 * tickrate)), r.end_tick]
            death_tick = next((k.tick for k in r.kills if k.victim.steamid == pid), None)
            segments = _segments(ticks, pid, r, tickrate, cfg, total_ticks,
                                 onsets={k.tick: k.fight_start_tick for k in kills if k.fight_start_tick is not None},
                                 death_tick=death_tick, windows=windows)
            if not segments:
                continue
            duration_s = sum(s.end_tick - s.start_tick for s in segments) / tickrate
            # Long, scattered clips are harder to watch than one tight burst.
            penalty = min(6.0, (4.0 if duration_s > 40 else 0.0) + 1.5 * (len(segments) - 1))

            score = max(0.0, min(100.0, (feat + impact + style) * economy * stakes * outcome - penalty))
            if n >= 5:  # an ace is always a play of the match, whoever it was against
                score = max(score, cfg.tier_s)
            if n == 1:
                floors = [v for t, v in _SINGLE_KILL_FLOOR.items() if t in tags
                          and not (t == "noscope" and (kills[0].distance_m or 99) < 5)]  # point blank is no feat
                if floors:
                    score = max(score, max(floors))
            worth_a_look = (n >= 2 or won_clutch or bool(_FLASHY & set(tags)))
            if not worth_a_look or score < cfg.min_score:
                continue

            label = _LABEL.get(_KIND_BY_KILLS.get(min(n, 5), ""), "")
            parts = []
            if near_clutch:
                parts.append(f"almost a 1v{clutch.versus}")
            if won_clutch:
                parts.append(f"1v{clutch.versus} clutch")
            if label and n >= 2:
                parts.append(label)
            if not parts:
                nice = [t.replace("_", " ") for t in dict.fromkeys(tags) if t in _FLASHY]
                parts.append((" ".join(nice) + " kill").strip() if nice else "kill")
            title = f"{player.name} — {' + '.join(parts)}"
            if kills:
                title += f" ({_weapon_phrase(kills)})"
            hs = sum(1 for k in kills if k.headshot)
            desc_bits = [f"Round {r.number} on {map_name} as {player.side}"]
            if n:
                desc_bits.append(f"{n} kill{'s' if n != 1 else ''}" + (f" in {span_s:.1f}s" if n >= 2 else "")
                                 + (f", {hs} headshot{'s' if hs != 1 else ''}" if hs else ""))
            extras = [t.replace("_", " ") for t in dict.fromkeys(tags)
                      if t in _FLASHY or t in {"long_range", "deagle_headshot"}]
            if extras:
                desc_bits.append(", ".join(extras))
            desc_bits.append(f"round {'won' if won_round else 'lost'} ({r.score_team_a}-{r.score_team_b})")
            victims = ", ".join(k.victim.name for k in kills)
            description = ". ".join(desc_bits) + (f". Victims: {victims}." if victims else ".")

            out.append(HighlightCandidate(
                kind=kind, title=title, description=description, round=r.number, player=player,
                score=round(score, 1), tags=list(dict.fromkeys(tags + ctx_tags)), segments=segments,
                kill_ticks=[k.tick for k in kills], start_tick=segments[0].start_tick,
                end_tick=segments[-1].end_tick, duration_s=round(duration_s, 1), team=team,
                action_ticks=[t for t in r.activity.get(pid, [])
                              if any(s.start_tick <= t <= s.end_tick for s in segments)],
                tier=tier_of(score, cfg),
                breakdown={"feat": round(feat, 1), "impact": round(impact, 1), "style": round(style, 1),
                           "economy": economy, "stakes": round(stakes, 2), "outcome": outcome,
                           "penalty": round(penalty, 1), "impact_max": cfg.impact_weight,
                           **({"win_before": round(win_before, 3)} if win_before is not None else {})},
            ))

        # Ninja defuse: bomb defused while at least one terrorist is still alive.
        if r.defuse_tick is not None:
            t_players = set(r.t_players)
            dead_t = {k.victim.steamid for k in r.kills if k.victim.side == "T" and k.tick <= r.defuse_tick}
            defuser = r.defuser
            alive_t = len(t_players - dead_t)
            mine = next((h for h in out if h.round == r.number and defuser is not None
                         and h.player.steamid == defuser.steamid), None)
            if alive_t >= 1 and mine is not None:
                # the defuser already has a play this round (usually the clutch itself): it is a ninja defuse too
                mine.tags.append("ninja_defuse")
                mine.title += " + ninja defuse"
                mine.score = round(min(100.0, mine.score + 8.0 * alive_t), 1)
                mine.tier = tier_of(mine.score, cfg)
                mine.breakdown["feat"] = round(mine.breakdown.get("feat", 0.0) + 8.0 * alive_t, 1)
            elif alive_t >= 1 and defuser is not None:
                seg = HighlightSegment(start_tick=max(r.freeze_end_tick, r.defuse_tick - int(8 * tickrate)),
                                       end_tick=min(r.defuse_tick + int(1.5 * tickrate), total_ticks),
                                       pov_steamid=defuser.steamid)
                stakes, ctx_tags = _stakes(r, before, last_round, True)
                feat = 34.0 + 8.0 * alive_t
                score = min(100.0, feat * stakes)
                out.append(HighlightCandidate(
                    kind="ninja_defuse", title=f"{defuser.name} — ninja defuse",
                    description=f"Round {r.number} on {map_name}: bomb defused with {alive_t} terrorist(s) alive.",
                    round=r.number, player=defuser, score=round(score, 1),
                    tags=["ninja_defuse", *ctx_tags], segments=[seg], kill_ticks=[], start_tick=seg.start_tick,
                    end_tick=seg.end_tick, duration_s=round((seg.end_tick - seg.start_tick) / tickrate, 1),
                    team=r.ct_team, tier=tier_of(score, cfg),
                    breakdown={"feat": feat, "impact": 0.0, "style": 0.0, "economy": 1.0,
                               "stakes": round(stakes, 2), "outcome": 1.0, "penalty": 0.0,
                               "impact_max": cfg.impact_weight},
                ))
        before = (r.score_team_a, r.score_team_b)

    out.sort(key=lambda h: -h.score)
    # The cap trims the long tail of double kills, never a clutch, an ace or a ninja defuse (a 46-round
    # overtime game used to lose its overtime clutches to it).
    kept = [h for h in out if h.kind in _ALWAYS_KEPT]
    rest = [h for h in out if h.kind not in _ALWAYS_KEPT]
    out = sorted(kept + rest[: max(0, cfg.max_highlights - len(kept))], key=lambda h: -h.score)
    for rank, i in enumerate(select_featured(out, cfg), start=1):
        out[i].featured = True
        out[i].featured_rank = rank
    _add_replays(out, rounds, tickrate, total_ticks, cfg)
    return out


_REPLAY_WORTHY = ("wallbang", "through_smoke", "noscope", "blind_kill", "airborne", "collateral", "knife", "zeus")


def replay_kill(h: HighlightCandidate, r: RoundSummary) -> KillEvent | None:
    """The one kill of a play worth seeing again from the victim's eyes: the first that went through a wall or
    smoke, a noscope, a kill while blind or in the air; otherwise the kill that closed a clutch or an ace."""
    kills = [k for k in r.kills if k.tick in set(h.kill_ticks) and k.attacker is not None
             and k.attacker.steamid == h.player.steamid and not k.victim.steamid.startswith("BOT:")]
    if not kills:
        return None

    def worth(k: KillEvent) -> int:
        flags = {"wallbang": k.wallbang, "through_smoke": k.through_smoke, "noscope": k.noscope,
                 "blind_kill": k.attacker_blind, "airborne": k.attacker_airborne,
                 "knife": weapons.weapon_class(k.weapon) == "knife", "zeus": k.weapon == "taser"}
        return next((len(_REPLAY_WORTHY) - i for i, t in enumerate(_REPLAY_WORTHY) if flags.get(t)), 0)

    best = max(kills, key=lambda k: (worth(k), k.tick))
    if worth(best) == 0 and h.kind not in ("clutch", "ace"):
        return None
    return best


def _add_replays(out: list[HighlightCandidate], rounds: list[RoundSummary], tickrate: int, total_ticks: int,
                 cfg: HighlightConfig) -> None:
    by_number = {r.number: r for r in rounds}
    added = 0
    for h in sorted(out, key=lambda h: -h.score):
        if added >= cfg.replays_per_match:
            break
        if not h.featured or h.tier != "S" or h.round not in by_number:
            continue
        k = replay_kill(h, by_number[h.round])
        if k is None:
            continue
        start = max(by_number[h.round].start_tick, k.tick - int(cfg.replay_lead_s * tickrate))
        end = min(total_ticks, k.tick + int(cfg.replay_tail_s * tickrate))
        h.segments.append(HighlightSegment(start_tick=start, end_tick=end, pov_steamid=k.victim.steamid,
                                           speed=cfg.replay_speed, caption=f"Replay · {k.victim.name}", role="replay"))
        h.duration_s = round(sum((s.end_tick - s.start_tick) / tickrate / s.speed for s in h.segments), 1)
        h.tags.append("victim_replay")
        added += 1


# ---------------------------------------------------------------------------------------------- curation

def select_featured(candidates: list[HighlightCandidate], cfg: HighlightConfig | None = None) -> list[int]:
    """Indexes of the plays that deserve first-person footage, best first.

    * up to ``featured_per_team`` of each team, among plays of tier B or better, at most
      ``max_featured_per_player`` of one player and one per round for a team;
    * a team with nothing that good still gets its single best play, so both sides appear;
    * every tier-S play, whatever the quota - a match full of great plays gets more videos;
    * never more than ``featured_max`` in total (the best are kept)."""
    cfg = cfg or HighlightConfig()
    order = sorted(range(len(candidates)), key=lambda i: -candidates[i].score)
    chosen: list[int] = []
    for team in sorted({c.team for c in candidates if c.team}):
        mine = [i for i in order if candidates[i].team == team]
        per_player: dict[str, int] = {}
        rounds: set[int] = set()
        picked = 0
        for i in mine:
            c = candidates[i]
            if picked >= cfg.featured_per_team or c.score < cfg.tier_b:
                break
            if per_player.get(c.player.steamid, 0) >= cfg.max_featured_per_player or c.round in rounds:
                continue
            chosen.append(i)
            per_player[c.player.steamid] = per_player.get(c.player.steamid, 0) + 1
            rounds.add(c.round)
            picked += 1
        if picked == 0 and mine:
            chosen.append(mine[0])
    chosen += [i for i in order if i not in chosen and candidates[i].score >= cfg.tier_s]
    chosen.sort(key=lambda i: -candidates[i].score)
    return chosen[: cfg.featured_max]
