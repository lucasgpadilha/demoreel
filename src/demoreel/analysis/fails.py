"""Fails: the moments friends roast each other for.

Detected from what the demo already gives (shots, damage, blinds, deaths) and stored like highlights, with
``category="fail"`` - so they get 2D replays, and the funniest of a match get first-person videos, through
the same pipeline. Scores are on the same 0-100 scale as plays but only compare fails with fails.

* ``fail_whiff``       - emptied the magazine at somebody close, hit nothing and died;
* ``fail_team_flash``  - blinded a team-mate who was killed while blind (the thrower is the culprit);
* ``fail_self_flash``  - blinded himself and died blind;
* ``fail_teamkill``    - killed a team-mate during the round;
* ``fail_own_grenade`` - died to his own grenade or fire;
* ``fail_fall``        - fell to his death;
* ``fail_bomb``        - died to the bomb;
* ``fail_knife_out``   - was killed holding the knife in a round where everybody had guns.
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from dataclasses import dataclass

from . import weapons
from .parser import ParsedDemo
from .schema import HighlightCandidate, HighlightSegment, PlayerRef, RoundSummary


@dataclass
class FailConfig:
    whiff_window_s: float = 5.0
    whiff_min_shots: int = 8
    whiff_min_sniper_shots: int = 2
    whiff_max_distance_m: float = 12.0
    min_score: float = 22.0
    max_fails: int = 12  # per match
    featured: int = 2  # fails of a match that get first-person videos
    featured_floor: float = 40.0


_OWN_GRENADE = {"hegrenade", "inferno", "molotov", "incgrenade"}
_FALL = {"trigger_hurt"}
_BOMB = {"planted_c4"}


def _facts(facts: dict | None) -> dict:
    out = {k: v for k, v in (facts or {}).items() if v is not None}
    if out.get("weapon"):
        out["weapon_name"] = weapons.display_name(out["weapon"])
    return out


def tier_of(score: float) -> str:
    return "S" if score >= 62 else "A" if score >= 45 else "B" if score >= 30 else "C"


def detect(demo: ParsedDemo, rounds: list[RoundSummary], cfg: FailConfig | None = None) -> list[HighlightCandidate]:
    cfg = cfg or FailConfig()
    tr = demo.tickrate
    by_number = {r.number: r for r in rounds}
    info = {r.number: r for r in demo.rounds}

    def ref(pid: str, rnd: int) -> PlayerRef:
        return PlayerRef(steamid=pid, name=demo.names.get(pid, pid), side=info[rnd].sides.get(pid))

    def team_of(pid: str, rnd: int) -> str | None:
        r = by_number[rnd]
        return r.ct_team if pid in r.ct_players else r.t_team if pid in r.t_players else None

    shots: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for s in demo.shots:
        if weapons.is_bullet_weapon(s.get("weapon")):
            shots[s["player"]].append((int(s["tick"]), s.get("weapon") or ""))
    for rows in shots.values():
        rows.sort()
    dealt: dict[str, list[int]] = defaultdict(list)  # ticks at which a player damaged an enemy
    for d in demo.damages:
        if d.get("attacker") and not d.get("is_friendly") and (d.get("damage") or 0) > 0:
            dealt[d["attacker"]].append(int(d["tick"]))
    for rows in dealt.values():
        rows.sort()

    found: dict[tuple[int, str, str], HighlightCandidate] = {}

    def who(pid: str | None) -> dict | None:
        return {"steamid": pid, "name": demo.names.get(pid, pid)} if pid else None

    def add(kind: str, rnd: int, player: str, score: float, title: str, description: str, start: int, end: int,
            pov: str, tags: list[str], tick: int, caption: str | None = None, facts: dict | None = None) -> None:
        r = by_number.get(rnd)
        if r is None or score < cfg.min_score:
            return
        lo, hi = r.start_tick, min(max(r.official_end_tick, r.end_tick) + int(2 * tr), demo.total_ticks)
        start, end = max(lo, start), min(hi, end)
        if end - start < tr:
            return
        key = (rnd, player, kind)
        old = found.get(key)
        if old is not None and old.score >= score:
            return
        seg = HighlightSegment(start_tick=start, end_tick=end, pov_steamid=pov, caption=caption)
        found[key] = HighlightCandidate(
            kind=kind, title=title, description=description, round=rnd, player=ref(player, rnd),
            score=round(min(100.0, score), 1), tags=["fail", *tags], segments=[seg], kill_ticks=[tick],
            start_tick=start, end_tick=end, duration_s=round((end - start) / tr, 1), team=team_of(player, rnd),
            tier=tier_of(score), breakdown={"fail": round(min(100.0, score), 1)}, category="fail",
            facts=_facts(facts))

    name = lambda pid: demo.names.get(pid, pid) if pid else "?"  # noqa: E731
    deaths_at: dict[int, int] = defaultdict(int)
    for k in demo.kills:
        if k["is_suicide"]:
            deaths_at[k["tick"]] += 1

    for k in demo.kills:
        rnd, t, v, a = k["round"], int(k["tick"]), k["victim"], k["attacker"]
        r = by_number.get(rnd)
        if r is None or k["after_round_end"]:
            continue
        weapon = k["weapon"]

        if k["is_teamkill"] and a:
            add("fail_teamkill", rnd, a, 60 + (10 if weapons.weapon_class(weapon) == "knife" else 0),
                f"{name(a)} — team kill on {name(v)}",
                f"Round {rnd}: {name(a)} killed team-mate {name(v)} with {weapons.display_name(weapon)}.",
                t - int(3.0 * tr), t + int(1.5 * tr), a, ["teamkill"], t,
                facts={"victim": who(v), "weapon": weapon})
            continue
        if k["is_suicide"]:
            if deaths_at[t] >= 3:  # everybody "dying" on the same tick is a restart, not a fail
                continue
            if weapon in _OWN_GRENADE:
                add("fail_own_grenade", rnd, v, 65, f"{name(v)} — killed by his own grenade",
                    f"Round {rnd}: {name(v)} died to his own {weapons.display_name(weapon)}.",
                    t - int(4.0 * tr), t + int(1.0 * tr), v, ["own_grenade"], t, facts={"weapon": weapon})
            elif weapon in _FALL:
                add("fail_fall", rnd, v, 60, f"{name(v)} — fell to his death",
                    f"Round {rnd}: {name(v)} fell off the map.", t - int(4.0 * tr), t + int(1.0 * tr), v, ["fall"], t,
                    facts={"weapon": weapon})
            elif weapon in _BOMB:
                add("fail_bomb", rnd, v, 35, f"{name(v)} — blown up by the bomb",
                    f"Round {rnd}: {name(v)} was still next to the bomb when it went off.",
                    t - int(4.0 * tr), t + int(1.0 * tr), v, ["bomb"], t, facts={"weapon": weapon})
            continue
        if not a or not v:
            continue

        # --- whiff: a magazine at somebody close, nothing hit, dead ------------------------------------
        window = int(cfg.whiff_window_s * tr)
        mine = shots.get(v, [])
        lo = bisect.bisect_left(mine, (t - window, ""))
        fired = [s for s in mine[lo:] if s[0] <= t]
        hits = dealt.get(v, [])
        hit = bisect.bisect_right(hits, t + 2) - bisect.bisect_left(hits, t - window)
        dist = k.get("distance_m")
        if fired and hit == 0 and dist is not None and dist <= cfg.whiff_max_distance_m and not k["through_smoke"]:
            sniper = weapons.weapon_class(fired[-1][1]) == "sniper"
            n = len(fired)
            if (sniper and n >= cfg.whiff_min_sniper_shots) or (not sniper and n >= cfg.whiff_min_shots):
                score = (42 + 6 * (n - 2)) if sniper else (30 + 1.5 * (n - cfg.whiff_min_shots))
                score += max(0.0, cfg.whiff_max_distance_m - dist) * 1.2
                add("fail_whiff", rnd, v, min(score, 85.0),
                    f"{name(v)} — {n} shots, zero hits",
                    f"Round {rnd}: {name(v)} fired {n} {weapons.display_name(fired[-1][1])} rounds at {name(a)} "
                    f"from {dist:.0f} m without a hit and died.",
                    max(fired[0][0] - int(1.0 * tr), t - int(8.0 * tr)), t + int(1.2 * tr), v,
                    ["whiff", *(["sniper"] if sniper else [])], t,
                    facts={"shots": n, "weapon": fired[-1][1], "distance_m": dist, "killer": who(a)})

        # --- killed holding the knife while everybody had guns ----------------------------------------------
        if (not r.is_pistol and weapons.weapon_class(k.get("victim_weapon")) == "knife"
                and weapons.weapon_class(weapon) != "knife"):
            add("fail_knife_out", rnd, v, 24 + (6 if (dist or 99) <= 5 else 0),
                f"{name(v)} — caught with the knife out",
                f"Round {rnd}: {name(v)} was killed by {name(a)} with the knife in his hand.",
                t - int(3.5 * tr), t + int(1.0 * tr), v, ["knife_out"], t,
                facts={"killer": who(a), "distance_m": dist})

    # --- flashes that got a team-mate (or the thrower) killed --------------------------------------------
    enemy_deaths: dict[str, list[tuple[int, dict]]] = defaultdict(list)
    for k in demo.kills:
        if not k["is_suicide"] and not k["is_teamkill"] and not k["after_round_end"]:
            enemy_deaths[k["victim"]].append((int(k["tick"]), k))
    for b in demo.blinds:
        dur = float(b.get("duration") or 0)
        thrower, blinded = b.get("attacker"), b.get("victim")
        if dur < 1.5 or not thrower or not blinded or b.get("attacker_side") != b.get("victim_side"):
            continue
        t0 = int(b["tick"])
        death = next((k for t, k in enemy_deaths.get(blinded, []) if 0 <= t - t0 <= min(dur, 4.0) * tr), None)
        if death is None:
            continue
        t = int(death["tick"])
        if thrower == blinded:
            add("fail_self_flash", b["round"], blinded, 40 + 4 * min(dur, 5.0), f"{name(blinded)} — flashed himself and died",
                f"Round {b['round']}: {name(blinded)} was blind from his own flash for {dur:.1f} s and died.",
                t0 - int(2.5 * tr), t + int(1.2 * tr), blinded, ["self_flash"], t,
                facts={"thrower": who(thrower), "blinded": who(blinded), "blind_s": round(dur, 1),
                       "killer": who(death["attacker"]), "seconds_to_death": round((t - t0) / tr, 1)})
        else:
            add("fail_team_flash", b["round"], thrower, 46 + 5 * min(dur, 5.0),
                f"{name(thrower)} — flashed {name(blinded)}, who died blind",
                f"Round {b['round']}: {name(thrower)}'s flash blinded team-mate {name(blinded)} for {dur:.1f} s; "
                f"{name(death['attacker'])} killed him {((t - t0) / tr):.1f} s later.",
                t0 - int(2.0 * tr), t + int(1.2 * tr), blinded, ["team_flash"], t,
                caption=f"Flash: {name(thrower)}",
                facts={"thrower": who(thrower), "blinded": who(blinded), "blind_s": round(dur, 1),
                       "killer": who(death["attacker"]), "seconds_to_death": round((t - t0) / tr, 1)})

    out = sorted(found.values(), key=lambda h: -h.score)[: cfg.max_fails]
    seen: set[str] = set()
    rank = 0
    for h in out:  # the funniest few get first-person videos; one per player
        if rank >= cfg.featured or h.score < cfg.featured_floor or h.player.steamid in seen:
            continue
        rank += 1
        seen.add(h.player.steamid)
        h.featured, h.featured_rank = True, rank
    return out
