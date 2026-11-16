"""Thin, defensive wrapper around ``demoparser2`` that turns a .dem file into normalized tables.

The goal is to hide the quirks of raw demo events (missing events, warm-up, knife rounds / restarts,
bots without SteamIDs, side swaps) so the stats and highlight code can stay simple.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
from demoparser2 import DemoParser

from . import weapons


def player_id(steamid: Any, name: Any = None) -> str | None:
    """Stable string id for a player. Bots have no SteamID, so they get ``BOT:<name>``."""
    if steamid is not None and not (isinstance(steamid, float) and math.isnan(steamid)):
        if isinstance(steamid, str):
            s = steamid.strip()
            if s and s not in {"0", "nan", "None"}:
                return s
        else:
            try:
                i = int(steamid)
                if i > 0:
                    return str(i)
            except (TypeError, ValueError):
                pass
    if isinstance(name, str) and name.strip():
        return f"BOT:{name.strip()}"
    return None


def norm_side(v: Any) -> str | None:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    if isinstance(v, str):
        u = v.upper()
        if u in {"CT", "COUNTER-TERRORIST", "COUNTERTERRORIST"}:
            return "CT"
        if u in {"T", "TERRORIST"}:
            return "T"
        if u.isdigit():
            v = int(u)
        else:
            return None
    try:
        return {2: "T", 3: "CT"}.get(int(v))
    except (TypeError, ValueError):
        return None


def _clean(v: Any) -> Any:
    """NaN/NA -> None so values are JSON friendly."""
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v


@dataclass
class RoundInfo:
    number: int
    start_tick: int
    freeze_end_tick: int
    end_tick: int
    official_end_tick: int
    winner_side: str
    reason: str
    score_after: dict[str, int] = field(default_factory=dict)  # scoreboard right after the round: {"CT": n, "T": n}
    sides: dict[str, str] = field(default_factory=dict)  # player id -> "CT" | "T"
    equip: dict[str, int] = field(default_factory=dict)  # player id -> equipment value at freeze end
    clan_names: dict[str, str] = field(default_factory=dict)  # side -> clan name
    plant_tick: int | None = None
    bomb_site: str | None = None
    planter: str | None = None
    defuse_tick: int | None = None
    defuser: str | None = None
    explode_tick: int | None = None


@dataclass
class ParsedDemo:
    path: Path
    header: dict[str, Any]
    map_name: str
    tickrate: int
    total_ticks: int
    rounds: list[RoundInfo]
    names: dict[str, str]  # player id -> last seen name
    kills: list[dict[str, Any]]
    damages: list[dict[str, Any]]
    blinds: list[dict[str, Any]]
    grenades: list[dict[str, Any]]  # detonations: {tick, round, player, type, x, y, z}
    shots: list[dict[str, Any]]
    chat: list[dict[str, Any]]
    final_props: dict[str, dict[str, Any]]  # player id -> {mvps, total_cash_spent}
    warnings: list[str] = field(default_factory=list)

    def round_at(self, tick: int) -> RoundInfo | None:
        ends = [r.official_end_tick for r in self.rounds]
        i = bisect.bisect_left(ends, tick)
        if i < len(self.rounds) and self.rounds[i].start_tick <= tick:
            return self.rounds[i]
        return None


class _Events:
    """Event access that never raises: a missing event is just an empty frame.

    Note: ``list_game_events()`` is deliberately not used as a filter — demoparser2 synthesizes events
    such as ``round_start`` / ``round_end`` that do not appear in that list.
    """

    def __init__(self, parser: DemoParser):
        self.parser = parser

    def get(self, name: str, player: list[str] | None = None, other: list[str] | None = None) -> pd.DataFrame:
        try:
            df = self.parser.parse_event(name, player=player or [], other=other or [])
        except Exception:
            return pd.DataFrame()
        if not isinstance(df, pd.DataFrame):
            return pd.DataFrame()
        if "is_warmup_period" in df.columns:
            df = df[df["is_warmup_period"] != True]  # noqa: E712 - NaN-safe comparison
        if "tick" in df.columns:
            df = df.sort_values("tick", kind="stable")
        return df.reset_index(drop=True)


def _ticks_of(df: pd.DataFrame) -> list[int]:
    if df.empty or "tick" not in df.columns:
        return []
    return sorted({int(t) for t in df["tick"].tolist()})


def _build_rounds(ev: _Events, max_tick: int, tickrate: int, warnings: list[str]) -> list[RoundInfo]:
    ends = ev.get("round_end", other=["is_warmup_period"])
    if ends.empty or "winner" not in ends.columns:
        return []
    ends = ends.assign(_side=ends["winner"].map(norm_side))
    ends = ends[ends["_side"].notna()].drop_duplicates(subset=["tick"]).reset_index(drop=True)

    start_ticks = _ticks_of(ev.get("round_start"))
    freeze_ticks = _ticks_of(ev.get("round_freeze_end"))
    official_ticks = _ticks_of(ev.get("round_officially_ended"))
    match_end = _ticks_of(ev.get("cs_win_panel_match"))

    rounds: list[RoundInfo] = []
    prev_boundary = 0
    for i, row in ends.iterrows():
        end_tick = int(row["tick"])
        # Start: the latest round_start after the previous round finished, else the previous boundary.
        candidates = [t for t in start_ticks if prev_boundary <= t < end_tick]
        start_tick = candidates[-1] if candidates else prev_boundary
        fz = [t for t in freeze_ticks if start_tick <= t < end_tick]
        freeze_end = fz[0] if fz else start_tick
        next_end = int(ends.loc[i + 1, "tick"]) if i + 1 < len(ends) else None
        off = [t for t in official_ticks if t > end_tick and (next_end is None or t < next_end)]
        if off:
            official_end = off[0]
        else:
            tail = [t for t in match_end if t >= end_tick]
            official_end = tail[0] if tail else end_tick + 5 * tickrate
            if next_end is not None:
                official_end = min(official_end, next_end - 1)
        official_end = max(end_tick, min(official_end, max_tick))
        rounds.append(
            RoundInfo(
                number=len(rounds) + 1,
                start_tick=start_tick,
                freeze_end_tick=freeze_end,
                end_tick=end_tick,
                official_end_tick=official_end,
                winner_side=row["_side"],
                reason=str(_clean(row.get("reason")) or "unknown"),
            )
        )
        prev_boundary = official_end
    return rounds


def _drop_pre_restart_rounds(parser: DemoParser, rounds: list[RoundInfo], max_tick: int, tickrate: int,
                             warnings: list[str]) -> list[RoundInfo]:
    """Knife rounds and admin restarts reset the scoreboard; keep only rounds after the last reset."""
    if len(rounds) < 2:
        return rounds
    probe = {min(r.end_tick + tickrate, max_tick): r for r in rounds}
    try:
        df = parser.parse_ticks(["team_rounds_total", "team_num"], ticks=list(probe))
    except Exception:
        return rounds
    if df.empty:
        return rounds
    board = df.drop_duplicates(subset=["tick", "team_num"])
    for row in board.itertuples(index=False):
        side = norm_side(row.team_num)
        r = probe.get(int(row.tick))
        if r is not None and side is not None:
            r.score_after[side] = int(_clean(row.team_rounds_total) or 0)
    totals = [sum(r.score_after.values()) if len(r.score_after) == 2 else -1 for r in probe.values()]
    first = 0
    for i in range(1, len(totals)):
        if totals[i] >= 0 and totals[i - 1] >= 0 and totals[i] <= totals[i - 1]:
            first = i
    if first:
        warnings.append(f"Dropped {first} round(s) played before a scoreboard reset (knife round / restart).")
        rounds = rounds[first:]
        totals = totals[first:]
        for n, r in enumerate(rounds, start=1):
            r.number = n
    # The scoreboard knows the real round numbers. A demo can miss whole stretches of a match (recording
    # started late, stopped, or the file is a fragment): then rounds keep their true numbers and scores, and
    # the gap is reported instead of silently renumbering what is left as 1..N.
    if rounds and all(t > 0 for t in totals) and all(b > a for a, b in zip(totals, totals[1:], strict=False)):
        for r, t in zip(rounds, totals, strict=False):
            r.number = t
        played = totals[-1]
        if len(rounds) < played:
            have = {r.number for r in rounds}
            missing = [n for n in range(1, played + 1) if n not in have]
            ranges, start = [], missing[0]
            for a, b in zip(missing, missing[1:] + [None], strict=False):
                if b != a + 1:
                    ranges.append(f"{start}" if start == a else f"{start}–{a}")
                    start = b
            warnings.append(f"Incomplete demo: it contains {len(rounds)} of the {played} rounds played "
                            f"(missing rounds {', '.join(ranges)}). Scores are the real ones; player stats and "
                            f"highlights only cover the recorded rounds.")
    else:
        for r in rounds:
            r.score_after = {}
    return rounds


def _fill_rosters(parser: DemoParser, rounds: list[RoundInfo], names: dict[str, str]) -> None:
    probe = {min(r.freeze_end_tick + 1, r.end_tick): r for r in rounds}
    df = parser.parse_ticks(["team_num", "team_clan_name", "current_equip_value"], ticks=list(probe))
    for row in df.itertuples(index=False):
        r = probe.get(int(row.tick))
        side = norm_side(row.team_num)
        pid = player_id(row.steamid, row.name)
        if r is None or side is None or pid is None:
            continue
        r.sides[pid] = side
        r.equip[pid] = int(_clean(row.current_equip_value) or 0)
        clan = _clean(row.team_clan_name)
        if isinstance(clan, str) and clan.strip():
            r.clan_names[side] = clan.strip()
        if isinstance(row.name, str):
            names[pid] = row.name


def _derive_blinds(parser: DemoParser, demo: ParsedDemo) -> None:
    """Some demos carry no ``player_blind`` events. Rebuild them from the ``flash_duration`` player prop:
    whoever's value changes right after a flashbang detonation was blinded by that flashbang.
    (Validated against demos that do have the event: identical players and durations.)"""
    flashes = [g for g in demo.grenades if g["type"] == "flash" and g["player"]]
    if not flashes:
        return
    wanted = sorted({max(0, g["tick"] - 1) for g in flashes} | {g["tick"] + 2 for g in flashes})
    try:
        df = parser.parse_ticks(["flash_duration", "is_alive"], ticks=wanted)
    except Exception:
        return
    state: dict[int, dict[str, tuple[float, bool]]] = {}
    for row in df.itertuples(index=False):
        pid = player_id(row.steamid, row.name)
        if pid:
            state.setdefault(int(row.tick), {})[pid] = (float(_clean(row.flash_duration) or 0.0), bool(row.is_alive))
    for g in flashes:
        r = demo.round_at(g["tick"])
        before, after = state.get(max(0, g["tick"] - 1)), state.get(g["tick"] + 2)
        if r is None or before is None or after is None:
            continue
        for pid, (dur, alive) in after.items():
            if not alive or pid not in before or dur <= 0 or abs(dur - before[pid][0]) < 1e-3:
                continue
            demo.blinds.append({
                "tick": g["tick"], "round": r.number, "attacker": g["player"], "victim": pid, "duration": dur,
                "attacker_side": r.sides.get(g["player"]), "victim_side": r.sides.get(pid),
            })


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def parse_demo(path: str | Path) -> ParsedDemo:
    path = Path(path)
    parser = DemoParser(str(path))
    header = {k: _clean(v) for k, v in dict(parser.parse_header()).items()}
    warnings: list[str] = []
    ev = _Events(parser)

    clock = parser.parse_ticks(["game_time"]).drop_duplicates(subset=["tick"])
    max_tick = int(clock["tick"].max()) if len(clock) else 0
    tickrate = 64
    if len(clock) > 1000:
        a, b = clock.iloc[len(clock) // 4], clock.iloc[3 * len(clock) // 4]
        dt = float(b["game_time"]) - float(a["game_time"])
        if dt > 0:
            est = (int(b["tick"]) - int(a["tick"])) / dt
            tickrate = min((64, 128, 32), key=lambda c: abs(c - est))
    del clock

    rounds = _build_rounds(ev, max_tick, tickrate, warnings)
    rounds = _drop_pre_restart_rounds(parser, rounds, max_tick, tickrate, warnings)
    names: dict[str, str] = {}
    if rounds:
        _fill_rosters(parser, rounds, names)
    else:
        warnings.append("No completed rounds found in this demo.")

    demo = ParsedDemo(
        path=path, header=header, map_name=str(header.get("map_name") or "unknown"), tickrate=tickrate,
        total_ticks=max_tick, rounds=rounds, names=names, kills=[], damages=[], blinds=[], grenades=[],
        shots=[], chat=[], final_props={}, warnings=warnings,
    )
    if not rounds:
        return demo

    def side_of(r: RoundInfo, pid: str | None, hint: Any = None) -> str | None:
        return norm_side(hint) or (r.sides.get(pid) if pid else None)

    # --- kills -------------------------------------------------------------------------------------
    deaths = ev.get("player_death", player=["X", "Y", "Z", "team_name", "last_place_name", "active_weapon_name",
                                             "health", "flash_duration"],
                    other=["is_warmup_period"])
    for row in deaths.to_dict("records"):
        tick = int(row["tick"])
        r = demo.round_at(tick)
        if r is None:
            continue
        victim = player_id(row.get("user_steamid"), row.get("user_name"))
        if victim is None:
            continue
        attacker = player_id(row.get("attacker_steamid"), row.get("attacker_name"))
        assister = player_id(row.get("assister_steamid"), row.get("assister_name"))
        for pid, key in ((victim, "user_name"), (attacker, "attacker_name"), (assister, "assister_name")):
            if pid and isinstance(row.get(key), str):
                names.setdefault(pid, row[key])
        v_side = side_of(r, victim, row.get("user_team_name"))
        a_side = side_of(r, attacker, row.get("attacker_team_name"))
        dist = _clean(row.get("distance"))
        demo.kills.append({
            "tick": tick, "round": r.number,
            "attacker": attacker, "attacker_side": a_side,
            "victim": victim, "victim_side": v_side,
            "assister": assister, "assister_side": side_of(r, assister, row.get("assister_team_name")),
            "weapon": weapons.normalize(row.get("weapon")),
            "headshot": bool(_clean(row.get("headshot")) or False),
            "wallbang": int(_clean(row.get("penetrated")) or 0) > 0,
            "noscope": bool(_clean(row.get("noscope")) or False),
            "through_smoke": bool(_clean(row.get("thrusmoke")) or False),
            "attacker_blind": bool(_clean(row.get("attackerblind")) or False),
            "attacker_airborne": bool(_clean(row.get("attackerinair")) or False),
            "flash_assist": bool(_clean(row.get("assistedflash")) or False),
            "distance_m": round(float(dist), 1) if dist is not None else None,
            "is_suicide": attacker is None or attacker == victim,
            "is_teamkill": attacker is not None and attacker != victim and a_side is not None and a_side == v_side,
            "after_round_end": tick > r.end_tick,
            "attacker_place": _clean(row.get("attacker_last_place_name")),
            "victim_place": _clean(row.get("user_last_place_name")),
            # What the victim was holding and how the two were doing at that moment (props on the event are
            # in every demo; the dedicated events - player_blind, item_equip - are missing from half of them).
            "victim_weapon": weapons.normalize(row.get("user_active_weapon_name")) or None,
            "attacker_health": _int_or_none(_clean(row.get("attacker_health"))),
            "victim_blind_s": _float_or_none(_clean(row.get("user_flash_duration"))),
            "attacker_pos": [_clean(row.get("attacker_X")), _clean(row.get("attacker_Y")), _clean(row.get("attacker_Z"))],
            "victim_pos": [_clean(row.get("user_X")), _clean(row.get("user_Y")), _clean(row.get("user_Z"))],
        })

    # --- damage (capped to the health actually removed) --------------------------------------------
    hurts = ev.get("player_hurt", other=["is_warmup_period"])
    hp: dict[tuple[int, str], int] = {}
    for row in hurts.to_dict("records"):
        tick = int(row["tick"])
        r = demo.round_at(tick)
        victim = player_id(row.get("user_steamid"), row.get("user_name"))
        if r is None or victim is None:
            continue
        attacker = player_id(row.get("attacker_steamid"), row.get("attacker_name"))
        before = hp.get((r.number, victim), 100)
        after = int(_clean(row.get("health")) or 0)
        raw = int(_clean(row.get("dmg_health")) or 0)
        real = before - after if 0 <= after <= before else min(raw, before)
        hp[(r.number, victim)] = after
        a_side, v_side = side_of(r, attacker), side_of(r, victim)
        demo.damages.append({
            "tick": tick, "round": r.number, "attacker": attacker, "victim": victim,
            "attacker_side": a_side, "victim_side": v_side, "damage": max(0, real),
            "weapon": weapons.normalize(row.get("weapon")), "hitgroup": _clean(row.get("hitgroup")),
            "is_friendly": attacker is None or attacker == victim or (a_side is not None and a_side == v_side),
        })

    # --- utility ------------------------------------------------------------------------------------
    for row in ev.get("player_blind", other=["is_warmup_period"]).to_dict("records"):
        r = demo.round_at(int(row["tick"]))
        victim = player_id(row.get("user_steamid"), row.get("user_name"))
        attacker = player_id(row.get("attacker_steamid"), row.get("attacker_name"))
        if r is None or victim is None or attacker is None:
            continue
        demo.blinds.append({
            "tick": int(row["tick"]), "round": r.number, "attacker": attacker, "victim": victim,
            "duration": float(_clean(row.get("blind_duration")) or 0.0),
            "attacker_side": side_of(r, attacker), "victim_side": side_of(r, victim),
        })
    for event, kind in (("flashbang_detonate", "flash"), ("hegrenade_detonate", "he"),
                        ("smokegrenade_detonate", "smoke"), ("inferno_startburn", "fire"),
                        ("decoy_started", "decoy")):
        for row in ev.get(event, other=["is_warmup_period"]).to_dict("records"):
            r = demo.round_at(int(row["tick"]))
            if r is None:
                continue
            demo.grenades.append({
                "tick": int(row["tick"]), "round": r.number, "type": kind,
                "player": player_id(row.get("user_steamid"), row.get("user_name")),
                "x": _clean(row.get("x")), "y": _clean(row.get("y")), "z": _clean(row.get("z")),
            })
    demo.grenades.sort(key=lambda g: g["tick"])
    if not demo.blinds:
        _derive_blinds(parser, demo)
    for row in ev.get("weapon_fire", other=["is_warmup_period"]).to_dict("records"):
        r = demo.round_at(int(row["tick"]))
        pid = player_id(row.get("user_steamid"), row.get("user_name"))
        if r is None or pid is None:
            continue
        demo.shots.append({"tick": int(row["tick"]), "round": r.number, "player": pid,
                           "weapon": weapons.normalize(row.get("weapon"))})

    # --- bomb ---------------------------------------------------------------------------------------
    for row in ev.get("bomb_planted", player=["last_place_name"]).to_dict("records"):
        r = demo.round_at(int(row["tick"]))
        if r is None:
            continue
        r.plant_tick = int(row["tick"])
        r.planter = player_id(row.get("user_steamid"), row.get("user_name"))
        place = str(_clean(row.get("user_last_place_name")) or "")
        r.bomb_site = "A" if place.lower().endswith("sitea") else "B" if place.lower().endswith("siteb") else None
    for row in ev.get("bomb_defused").to_dict("records"):
        r = demo.round_at(int(row["tick"]))
        if r is not None:
            r.defuse_tick = int(row["tick"])
            r.defuser = player_id(row.get("user_steamid"), row.get("user_name"))
    for row in ev.get("bomb_exploded").to_dict("records"):
        r = demo.round_at(int(row["tick"]))
        if r is not None:
            r.explode_tick = int(row["tick"])

    # --- chat ---------------------------------------------------------------------------------------
    for row in ev.get("chat_message").to_dict("records"):
        text = _clean(row.get("chat_message"))
        if isinstance(text, str):
            demo.chat.append({"tick": int(row["tick"]), "name": _clean(row.get("user_name")),
                              "steamid": player_id(row.get("user_steamid")), "text": text})

    # --- end-of-match scoreboard extras ------------------------------------------------------------
    try:
        last = parser.parse_ticks(["mvps", "total_cash_spent"], ticks=[rounds[-1].end_tick])
        for row in last.itertuples(index=False):
            pid = player_id(row.steamid, row.name)
            if pid:
                demo.final_props[pid] = {"mvps": int(_clean(row.mvps) or 0),
                                         "total_cash_spent": int(_clean(row.total_cash_spent) or 0)}
    except Exception:  # optional extras; never fail the analysis for them
        pass

    return demo


def parse_positions(path: str | Path, start_tick: int, end_tick: int, step: int = 1) -> pd.DataFrame:
    """Per-tick player state for a tick range, used by the 2D renderer."""
    parser = DemoParser(str(path))
    props = ["X", "Y", "Z", "yaw", "health", "is_alive", "team_num", "active_weapon_name", "flash_duration",
             "has_defuser", "armor_value", "balance"]
    ticks = list(range(max(0, int(start_tick)), int(end_tick) + 1, max(1, int(step))))
    df = parser.parse_ticks(props, ticks=ticks)
    df["player"] = [player_id(s, n) for s, n in zip(df["steamid"], df["name"], strict=False)]
    return df
