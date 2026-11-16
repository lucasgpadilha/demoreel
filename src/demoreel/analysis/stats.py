"""Match statistics computed from a :class:`ParsedDemo`.

Definitions follow the conventions used by HLTV / Leetify-style scoreboards:

* **ADR** – damage dealt to enemies per round, capped to the health actually removed.
* **KAST** – % of rounds with a Kill, Assist, Survival or Traded death.
* **Trade** – a kill on a player who killed one of your teammates within ``TRADE_WINDOW_S``.
* **Rating** – approximation of HLTV Rating 2.0 using the public reverse-engineered formula.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from . import weapons
from .parser import ParsedDemo, RoundInfo
from .schema import (
    ClutchEvent,
    KillEvent,
    PlayerRef,
    PlayerStats,
    RoundSummary,
    SideStats,
    TeamSummary,
    UtilityStats,
)

TRADE_WINDOW_S = 5.0
REGULATION_ROUNDS = 24  # MR12


def _rating(kpr: float, dpr: float, apr: float, adr: float, kast_pct: float) -> tuple[float, float]:
    impact = 2.13 * kpr + 0.42 * apr - 0.41
    rating = 0.0073 * kast_pct + 0.3591 * kpr - 0.5329 * dpr + 0.2372 * impact + 0.0032 * adr + 0.1587
    return round(max(rating, 0.0), 2), round(impact, 2)


def _buy_type(total: int, players: int, is_pistol: bool) -> str:
    if is_pistol:
        return "pistol"
    per = total / max(players, 1)
    if per < 1000:
        return "eco"
    if per < 2500:
        return "semi"
    if per < 4000:
        return "force"
    return "full"


def assign_teams(rounds: list[RoundInfo]) -> tuple[dict[str, str], dict[int, str]]:
    """Give every player a persistent team (``team_a`` started CT) and say who is CT in each round."""
    members: dict[str, set[str]] = {"team_a": set(), "team_b": set()}
    ct_team_by_round: dict[int, str] = {}
    last = "team_a"
    for r in rounds:
        ct = {p for p, s in r.sides.items() if s == "CT"}
        t = {p for p, s in r.sides.items() if s == "T"}
        if not members["team_a"] and not members["team_b"]:
            members["team_a"], members["team_b"] = set(ct), set(t)
            ct_team = "team_a"
        elif not ct and not t:
            ct_team = last
        else:
            a_is_ct = len(ct & members["team_a"]) + len(t & members["team_b"])
            a_is_t = len(t & members["team_a"]) + len(ct & members["team_b"])
            ct_team = "team_a" if a_is_ct >= a_is_t else "team_b"
        t_team = "team_b" if ct_team == "team_a" else "team_a"
        members[ct_team] |= ct - members[t_team]
        members[t_team] |= t - members[ct_team]
        ct_team_by_round[r.number] = ct_team
        last = ct_team
    player_team = {p: key for key, ps in members.items() for p in ps}
    return player_team, ct_team_by_round


FIGHT_CHAIN_S = 2.0  # shots / hits closer together than this belong to the same exchange
FIGHT_LOOKBACK_S = 10.0


def round_activity(demo: ParsedDemo) -> dict[int, dict[str, list[int]]]:
    """``{round: {player: sorted ticks}}`` of bullets fired, damage dealt and damage taken from enemies."""
    out: dict[int, dict[str, set[int]]] = defaultdict(lambda: defaultdict(set))
    for shot in demo.shots:
        if weapons.is_bullet_weapon(shot.get("weapon")):
            out[shot["round"]][shot["player"]].add(int(shot["tick"]))
    for d in demo.damages:
        if d.get("is_friendly"):
            continue
        for who in (d.get("attacker"), d.get("victim")):
            if who:
                out[d["round"]][who].add(int(d["tick"]))
    return {rnd: {p: sorted(t) for p, t in players.items()} for rnd, players in out.items()}


def fight_starts(demo: ParsedDemo):
    """``f(player, kill_tick) -> tick`` where the exchange ending in that kill began for that player.

    A player's activity is every bullet he fired and every hit he dealt to or took from an enemy. Walking back
    from the kill, the exchange lasts as long as two consecutive events are at most ``FIGHT_CHAIN_S`` apart."""
    import bisect

    activity: dict[str, list[int]] = defaultdict(list)
    for shot in demo.shots:
        if weapons.is_bullet_weapon(shot.get("weapon")):
            activity[shot["player"]].append(int(shot["tick"]))
    for d in demo.damages:
        if d.get("is_friendly"):
            continue
        if d.get("attacker"):
            activity[d["attacker"]].append(int(d["tick"]))
        if d.get("victim"):
            activity[d["victim"]].append(int(d["tick"]))
    for ticks in activity.values():
        ticks.sort()
    chain, lookback = int(FIGHT_CHAIN_S * demo.tickrate), int(FIGHT_LOOKBACK_S * demo.tickrate)

    def start(player: str | None, kill_tick: int) -> int:
        ticks = activity.get(player or "", [])
        onset = kill_tick
        i = bisect.bisect_right(ticks, kill_tick) - 1
        while i >= 0 and ticks[i] >= kill_tick - lookback and onset - ticks[i] <= chain:
            onset = ticks[i]
            i -= 1
        return onset

    return start


def compute(demo: ParsedDemo) -> tuple[TeamSummary, TeamSummary, list[PlayerStats], list[RoundSummary]]:
    tr = demo.tickrate
    fight_start = fight_starts(demo)
    activity = round_activity(demo)
    rounds = demo.rounds
    player_team, ct_team_by_round = assign_teams(rounds)
    names = demo.names

    def ref(pid: str | None, side: str | None = None) -> PlayerRef | None:
        if pid is None:
            return None
        return PlayerRef(steamid=pid, name=names.get(pid, pid), side=side)  # type: ignore[arg-type]

    kills_by_round: dict[int, list[dict]] = defaultdict(list)
    for k in demo.kills:
        kills_by_round[k["round"]].append(k)

    # ---- per-round pass: openings, trades, clutches, KAST ingredients ---------------------------
    pr_kills: dict[tuple[int, str], int] = Counter()
    pr_assist: set[tuple[int, str]] = set()
    pr_died: set[tuple[int, str]] = set()
    pr_traded: set[tuple[int, str]] = set()
    opening_kills: Counter[str] = Counter()
    opening_deaths: Counter[str] = Counter()
    trade_kills: Counter[str] = Counter()
    traded_deaths: Counter[str] = Counter()
    clutches: list[ClutchEvent] = []
    summaries: list[RoundSummary] = []
    score = {"team_a": 0, "team_b": 0}
    first_swap_round: int | None = None
    prev_ct: str | None = None
    contiguous, expected_number = True, 1
    team_extra = {k: Counter() for k in ("team_a", "team_b")}

    for r in rounds:
        ct_team = ct_team_by_round.get(r.number, "team_a")
        t_team = "team_b" if ct_team == "team_a" else "team_a"
        if prev_ct is not None and ct_team != prev_ct and first_swap_round is None:
            first_swap_round = r.number
        prev_ct = ct_team
        is_overtime = r.number > REGULATION_ROUNDS
        contiguous = contiguous and r.number == expected_number
        expected_number = r.number + 1
        # Pistol rounds: the first round, and the first after the half-time swap. With a complete demo the swap
        # is observed; with gaps only the canonical MR12 round number can be trusted.
        is_pistol = not is_overtime and (r.number == 1 or r.number == REGULATION_ROUNDS // 2 + 1
                                         or (contiguous and r.number == first_swap_round))
        winner_team = ct_team if r.winner_side == "CT" else t_team
        if len(r.score_after) == 2:  # the scoreboard as recorded in the demo is the truth
            score[ct_team], score[t_team] = r.score_after["CT"], r.score_after["T"]
        else:
            score[winner_team] += 1
        team_extra[winner_team]["ct_wins" if r.winner_side == "CT" else "t_wins"] += 1
        if is_pistol:
            team_extra[winner_team]["pistol"] += 1
        if r.plant_tick is not None:
            team_extra[t_team]["plants"] += 1
        if r.defuse_tick is not None:
            team_extra[ct_team]["defuses"] += 1

        rk = sorted(kills_by_round.get(r.number, []), key=lambda k: k["tick"])
        alive = {"CT": {p for p, s in r.sides.items() if s == "CT"},
                 "T": {p for p, s in r.sides.items() if s == "T"}}
        team_size = {s: len(ps) for s, ps in alive.items()}
        in_clutch: dict[str, dict] = {}
        opening_done = False
        events: list[KillEvent] = []

        for i, k in enumerate(rk):
            a, v = k["attacker"], k["victim"]
            enemy_kill = not k["is_suicide"] and not k["is_teamkill"] and a is not None
            is_opening = False
            if enemy_kill and not opening_done and k["tick"] <= r.end_tick:
                opening_done = is_opening = True
                opening_kills[a] += 1
                opening_deaths[v] += 1
                if a in player_team:
                    team_extra[player_team[a]]["openings"] += 1
            is_trade = False
            if enemy_kill:
                for j in rk[:i]:
                    if (k["tick"] - j["tick"]) / tr > TRADE_WINDOW_S:
                        continue
                    if j["attacker"] == v and j["victim"] != a and j["victim_side"] == k["attacker_side"] \
                            and not j["is_teamkill"]:
                        is_trade = True
                        if (r.number, j["victim"]) not in pr_traded:
                            pr_traded.add((r.number, j["victim"]))
                            traded_deaths[j["victim"]] += 1
                if is_trade:
                    trade_kills[a] += 1
                pr_kills[(r.number, a)] += 1
                if a in in_clutch and k["tick"] <= r.end_tick:
                    in_clutch[a]["kills"] += 1
            pr_died.add((r.number, v))
            if k["assister"] and k["assister_side"] != k["victim_side"]:
                pr_assist.add((r.number, k["assister"]))

            # clutch bookkeeping (only while the round is still live)
            for s in ("CT", "T"):
                alive[s].discard(v)
            if k["tick"] <= r.end_tick:
                for s, o in (("CT", "T"), ("T", "CT")):
                    if len(alive[s]) == 1 and len(alive[o]) >= 1 and team_size[s] >= 2:
                        p = next(iter(alive[s]))
                        if p not in in_clutch:
                            in_clutch[p] = {"side": s, "versus": len(alive[o]), "tick": k["tick"], "kills": 0}

            events.append(KillEvent(
                tick=k["tick"], round=r.number,
                clock_s=round(max(0, k["tick"] - r.freeze_end_tick) / tr, 2),
                attacker=ref(a, k["attacker_side"]), victim=ref(v, k["victim_side"]),  # type: ignore[arg-type]
                assister=ref(k["assister"], k["assister_side"]),
                weapon=k["weapon"], weapon_name=weapons.display_name(k["weapon"]),
                headshot=k["headshot"], wallbang=k["wallbang"], noscope=k["noscope"],
                through_smoke=k["through_smoke"], attacker_blind=k["attacker_blind"],
                attacker_airborne=k["attacker_airborne"], flash_assist=k["flash_assist"],
                distance_m=k["distance_m"], is_teamkill=k["is_teamkill"], is_suicide=k["is_suicide"],
                is_opening=is_opening, is_trade=is_trade, after_round_end=k["after_round_end"],
                attacker_place=k["attacker_place"], victim_place=k["victim_place"],
                fight_start_tick=fight_start(a, k["tick"]) if a else None,
                victim_weapon=k.get("victim_weapon"), attacker_health=k.get("attacker_health"),
                victim_blind_s=k.get("victim_blind_s"),
            ))

        round_clutches = []
        for p, c in in_clutch.items():
            ce = ClutchEvent(round=r.number, player=ref(p, c["side"]), versus=c["versus"],  # type: ignore[arg-type]
                             start_tick=c["tick"], won=r.winner_side == c["side"], kills=c["kills"],
                             survived=p in alive[c["side"]])
            round_clutches.append(ce)
            clutches.append(ce)

        ct_eq = sum(v for p, v in r.equip.items() if r.sides.get(p) == "CT")
        t_eq = sum(v for p, v in r.equip.items() if r.sides.get(p) == "T")
        summaries.append(RoundSummary(
            number=r.number, start_tick=r.start_tick, freeze_end_tick=r.freeze_end_tick, end_tick=r.end_tick,
            official_end_tick=r.official_end_tick,
            duration_s=round(max(0, r.end_tick - r.freeze_end_tick) / tr, 1),
            winner_side=r.winner_side, winner_team=winner_team, reason=r.reason,  # type: ignore[arg-type]
            ct_team=ct_team, t_team=t_team,  # type: ignore[arg-type]
            ct_players=sorted(p for p, s in r.sides.items() if s == "CT"),
            t_players=sorted(p for p, s in r.sides.items() if s == "T"),
            score_team_a=score["team_a"], score_team_b=score["team_b"],
            is_pistol=is_pistol, is_overtime=is_overtime,
            bomb_planted=r.plant_tick is not None, bomb_site=r.bomb_site, plant_tick=r.plant_tick,
            defuse_tick=r.defuse_tick, explode_tick=r.explode_tick,
            planter=ref(r.planter, "T"), defuser=ref(r.defuser, "CT"),
            ct_equip_value=ct_eq or None, t_equip_value=t_eq or None,
            activity=activity.get(r.number, {}),
            ct_buy=_buy_type(ct_eq, team_size["CT"], is_pistol) if r.equip else None,
            t_buy=_buy_type(t_eq, team_size["T"], is_pistol) if r.equip else None,
            kills=events, clutches=round_clutches,
        ))

    # ---- per-player aggregation ------------------------------------------------------------------
    all_players = sorted({p for r in rounds for p in r.sides})
    side_in_round = {(r.number, p): s for r in rounds for p, s in r.sides.items()}
    damage_pr: dict[tuple[int, str], int] = Counter()
    for d in demo.damages:
        if not d["is_friendly"] and d["attacker"]:
            damage_pr[(d["round"], d["attacker"])] += d["damage"]

    stats: list[PlayerStats] = []
    for p in all_players:
        played = [r.number for r in rounds if p in r.sides]
        n = len(played)
        if n == 0:
            continue
        my_kills = [k for k in demo.kills if k["attacker"] == p and not k["is_suicide"] and not k["is_teamkill"]]
        deaths = sum(1 for k in demo.kills if k["victim"] == p)
        assists = sum(1 for k in demo.kills if k["assister"] == p and k["assister_side"] != k["victim_side"])
        dmg = sum(damage_pr[(rn, p)] for rn in played)
        kast_rounds = sum(
            1 for rn in played
            if pr_kills[(rn, p)] > 0 or (rn, p) in pr_assist or (rn, p) not in pr_died or (rn, p) in pr_traded
        )
        kills = len(my_kills)
        hs = sum(1 for k in my_kills if k["headshot"])
        kpr, dpr, apr = kills / n, deaths / n, assists / n
        adr = dmg / n
        kast = 100.0 * kast_rounds / n
        rating, impact = _rating(kpr, dpr, apr, adr, kast)
        multi = Counter(pr_kills[(rn, p)] for rn in played)
        my_clutches = [c for c in clutches if c.player.steamid == p]
        won_by_size = Counter(f"1v{c.versus}" for c in my_clutches if c.won)
        shots = sum(1 for s in demo.shots if s["player"] == p and weapons.is_bullet_weapon(s["weapon"]))
        hits = sum(1 for d in demo.damages if d["attacker"] == p and not d["is_friendly"]
                   and weapons.is_bullet_weapon(d["weapon"]))
        dists = [k["distance_m"] for k in my_kills if k["distance_m"] is not None]

        util = UtilityStats(
            he_damage=sum(d["damage"] for d in demo.damages
                          if d["attacker"] == p and not d["is_friendly"] and d["weapon"] == "hegrenade"),
            fire_damage=sum(d["damage"] for d in demo.damages if d["attacker"] == p and not d["is_friendly"]
                            and d["weapon"] in {"inferno", "molotov", "incgrenade"}),
            flashes_thrown=sum(1 for g in demo.grenades if g["player"] == p and g["type"] == "flash"),
            smokes_thrown=sum(1 for g in demo.grenades if g["player"] == p and g["type"] == "smoke"),
            hes_thrown=sum(1 for g in demo.grenades if g["player"] == p and g["type"] == "he"),
            molotovs_thrown=sum(1 for g in demo.grenades if g["player"] == p and g["type"] == "fire"),
            enemies_flashed=sum(1 for b in demo.blinds if b["attacker"] == p and b["victim"] != p
                                and b["attacker_side"] != b["victim_side"] and b["duration"] >= 1.0),
            teammates_flashed=sum(1 for b in demo.blinds if b["attacker"] == p and b["victim"] != p
                                  and b["attacker_side"] == b["victim_side"] and b["duration"] >= 1.0),
            enemy_blind_time_s=round(sum(b["duration"] for b in demo.blinds if b["attacker"] == p
                                         and b["attacker_side"] != b["victim_side"]), 1),
            flash_assists=sum(1 for k in demo.kills if k["assister"] == p and k["flash_assist"]
                              and k["assister_side"] != k["victim_side"]),
        )

        by_side: dict[str, SideStats] = {}
        for side in ("CT", "T"):
            srounds = [rn for rn in played if side_in_round.get((rn, p)) == side]
            if not srounds:
                continue
            sset = set(srounds)
            sk = sum(1 for k in my_kills if k["round"] in sset)
            sd = sum(1 for k in demo.kills if k["victim"] == p and k["round"] in sset)
            sa = sum(1 for k in demo.kills if k["assister"] == p and k["round"] in sset
                     and k["assister_side"] != k["victim_side"])
            sdmg = sum(damage_pr[(rn, p)] for rn in srounds)
            skast = 100.0 * sum(
                1 for rn in srounds
                if pr_kills[(rn, p)] > 0 or (rn, p) in pr_assist or (rn, p) not in pr_died or (rn, p) in pr_traded
            ) / len(srounds)
            m = len(srounds)
            srating, _ = _rating(sk / m, sd / m, sa / m, sdmg / m, skast)
            by_side[side] = SideStats(rounds=m, kills=sk, deaths=sd, assists=sa, damage=sdmg,
                                      adr=round(sdmg / m, 1), kast=round(skast, 1), rating=srating)

        extra = demo.final_props.get(p, {})
        stats.append(PlayerStats(
            steamid=p, name=names.get(p, p), team=player_team.get(p, "team_a"),  # type: ignore[arg-type]
            rounds_played=n, kills=kills, deaths=deaths, assists=assists, kd_diff=kills - deaths,
            kd_ratio=round(kills / deaths, 2) if deaths else float(kills),
            headshot_kills=hs, headshot_pct=round(100.0 * hs / kills, 1) if kills else 0.0,
            damage=dmg, adr=round(adr, 1), kast=round(kast, 1), kpr=round(kpr, 2), dpr=round(dpr, 2),
            impact=impact, rating=rating,
            opening_kills=opening_kills[p], opening_deaths=opening_deaths[p],
            trade_kills=trade_kills[p], traded_deaths=traded_deaths[p],
            multi_kills={"2k": multi[2], "3k": multi[3], "4k": multi[4], "5k": sum(v for c, v in multi.items() if c >= 5)},
            clutches_won=sum(1 for c in my_clutches if c.won), clutches_attempted=len(my_clutches),
            clutches_by_size=dict(won_by_size),
            wallbang_kills=sum(1 for k in my_kills if k["wallbang"]),
            noscope_kills=sum(1 for k in my_kills if k["noscope"] and weapons.weapon_class(k["weapon"]) == "sniper"),
            through_smoke_kills=sum(1 for k in my_kills if k["through_smoke"]),
            blind_kills=sum(1 for k in my_kills if k["attacker_blind"]),
            airborne_kills=sum(1 for k in my_kills if k["attacker_airborne"]),
            team_kills=sum(1 for k in demo.kills if k["attacker"] == p and k["is_teamkill"]),
            shots_fired=shots, shots_hit=hits, accuracy=round(100.0 * hits / shots, 1) if shots else 0.0,
            avg_kill_distance_m=round(sum(dists) / len(dists), 1) if dists else None,
            money_spent=extra.get("total_cash_spent"), mvps=extra.get("mvps"),
            utility=util,
            weapon_kills=dict(Counter(weapons.display_name(k["weapon"]) for k in my_kills).most_common()),
            by_side=by_side,
        ))
    stats.sort(key=lambda s: (-s.rating, -s.kills))

    # ---- teams -----------------------------------------------------------------------------------
    clan_votes: dict[str, Counter[str]] = {"team_a": Counter(), "team_b": Counter()}
    for r in rounds:
        ct_team = ct_team_by_round.get(r.number, "team_a")
        t_team = "team_b" if ct_team == "team_a" else "team_a"
        if r.clan_names.get("CT"):
            clan_votes[ct_team][r.clan_names["CT"]] += 1
        if r.clan_names.get("T"):
            clan_votes[t_team][r.clan_names["T"]] += 1
    clan = {k: (v.most_common(1)[0][0] if v else "") for k, v in clan_votes.items()}
    use_clan = bool(clan["team_a"]) and bool(clan["team_b"]) and clan["team_a"] != clan["team_b"]

    teams = []
    for key, start_side in (("team_a", "CT"), ("team_b", "T")):
        roster = [s for s in stats if s.team == key]
        fallback = f"Team {roster[0].name}" if roster else key
        other = "team_b" if key == "team_a" else "team_a"
        won = None if score[key] == score[other] else score[key] > score[other]
        teams.append(TeamSummary(
            key=key, name=clan[key] if use_clan else fallback, score=score[key],  # type: ignore[arg-type]
            starting_side=start_side, players=[s.steamid for s in roster],  # type: ignore[arg-type]
            rounds_won_ct=team_extra[key]["ct_wins"], rounds_won_t=team_extra[key]["t_wins"],
            pistol_rounds_won=team_extra[key]["pistol"], opening_kills=team_extra[key]["openings"],
            bomb_plants=team_extra[key]["plants"], bomb_defuses=team_extra[key]["defuses"], won=won,
        ))
    return teams[0], teams[1], stats, summaries
