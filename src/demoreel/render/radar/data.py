"""Loads what the 2D renderer needs straight from the demo: per-tick player state and world events."""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from demoparser2 import DemoParser

from ...analysis import weapons
from ...analysis.parser import _clean, player_id

SMOKE_TICKS = 18 * 64
FIRE_TICKS = 7 * 64
T_SIDE, CT_SIDE = 2, 3
_SIDE_NAMES = {T_SIDE: "T", CT_SIDE: "CT"}


@dataclass
class Track:
    """One player's state for every tick of a segment (arrays indexed by ``tick - start``)."""

    pid: str
    name: str
    x: np.ndarray
    y: np.ndarray
    z: np.ndarray
    yaw: np.ndarray
    health: np.ndarray
    armor: np.ndarray
    alive: np.ndarray
    present: np.ndarray
    team: np.ndarray  # 2 = T, 3 = CT, 0 = unknown
    weapon: list[str]
    defuser: np.ndarray
    flash: np.ndarray  # > 0 while blinded: the length in seconds of the blindness he is in
    defusing: np.ndarray
    has_c4: np.ndarray

    def side(self, i: int) -> str | None:
        return _SIDE_NAMES.get(int(self.team[i]))

    def main_side(self) -> str | None:
        """The side this player is on for (most of) the segment, even at ticks where he is absent."""
        known = self.team[self.team > 0]
        if len(known) == 0:
            return None
        return _SIDE_NAMES.get(int(np.bincount(known).argmax()))


@dataclass
class SegmentData:
    start: int
    end: int
    tracks: dict[str, Track]

    @property
    def n(self) -> int:
        return self.end - self.start + 1

    def index(self, tick: float) -> int:
        return max(0, min(int(tick) - self.start, self.n - 1))


@dataclass
class Area:
    start: int
    end: int
    x: float
    y: float
    z: float


@dataclass
class Shot:
    tick: int
    pid: str
    x: float
    y: float
    z: float
    yaw: float


@dataclass
class Hurt:
    tick: int
    attacker: str | None
    victim: str
    damage: int
    x: float  # where the victim stood
    y: float
    z: float


@dataclass
class Flight:
    """A thrown grenade on its way: one position per tick until it lands or bursts."""

    kind: str  # smoke | flash | he | molotov | decoy
    ticks: np.ndarray
    x: np.ndarray
    y: np.ndarray
    z: np.ndarray

    @property
    def start(self) -> int:
        return int(self.ticks[0])

    @property
    def end(self) -> int:
        return int(self.ticks[-1])


@dataclass
class Events:
    smokes: list[Area] = field(default_factory=list)
    fires: list[Area] = field(default_factory=list)
    flashes: list[Area] = field(default_factory=list)
    hes: list[Area] = field(default_factory=list)
    shots: list[Shot] = field(default_factory=list)
    shot_ticks: list[int] = field(default_factory=list)
    hurts: list[Hurt] = field(default_factory=list)
    hurt_ticks: list[int] = field(default_factory=list)
    flights: list[Flight] = field(default_factory=list)
    bomb_pos: dict[int, tuple[float, float, float]] = field(default_factory=dict)  # plant tick -> position
    drops: list[tuple[int, float, float, float]] = field(default_factory=list)  # bomb dropped: tick, x, y, z
    pickups: list[int] = field(default_factory=list)

    def shots_between(self, lo: float, hi: float) -> list[Shot]:
        return self.shots[bisect.bisect_left(self.shot_ticks, lo):bisect.bisect_right(self.shot_ticks, hi)]

    def hurts_between(self, lo: float, hi: float) -> list[Hurt]:
        return self.hurts[bisect.bisect_left(self.hurt_ticks, lo):bisect.bisect_right(self.hurt_ticks, hi)]

    def bomb_on_ground(self, tick: float, since: int) -> tuple[float, float, float] | None:
        """Where the bomb lies at ``tick`` if it was dropped (after ``since``) and nobody picked it up yet."""
        last = None
        for d in self.drops:
            if d[0] > tick:
                break
            if d[0] >= since:
                last = d
        if last is None:
            return None
        i = bisect.bisect_right(self.pickups, last[0])
        if i < len(self.pickups) and self.pickups[i] <= tick:
            return None
        return last[1], last[2], last[3]


_PROJECTILES = {"CSmokeGrenadeProjectile": "smoke", "CFlashbangProjectile": "flash", "CHEGrenadeProjectile": "he",
                "CMolotovProjectile": "molotov", "CDecoyProjectile": "decoy"}
_EVENTS = ["smokegrenade_detonate", "smokegrenade_expired", "inferno_startburn", "inferno_expire",
           "flashbang_detonate", "hegrenade_detonate", "weapon_fire", "player_hurt", "bomb_planted", "bomb_dropped",
           "bomb_pickup"]


def _records(df: object) -> list[dict]:
    return df.to_dict("records") if isinstance(df, pd.DataFrame) else []


def _all_events(parser: DemoParser) -> dict[str, list[dict]]:
    """Every event we draw, in one pass over the demo when the parser allows it."""
    try:
        present = set(parser.list_game_events())
    except Exception:  # noqa: BLE001
        present = set(_EVENTS)
    names = [n for n in _EVENTS if n in present]
    out: dict[str, list[dict]] = {n: [] for n in _EVENTS}
    if not names:
        return out
    try:
        for name, df in parser.parse_events(names, player=["X", "Y", "Z", "yaw"], other=[]):
            out[name] = _records(df)
        return out
    except Exception:  # noqa: BLE001 - older parser, or one odd event: read them one by one
        pass
    for name in names:
        try:
            out[name] = _records(parser.parse_event(name, player=["X", "Y", "Z", "yaw"], other=[]))
        except Exception:  # noqa: BLE001 - a missing event only removes its drawing
            out[name] = []
    return out


def _num(v: object, default: float = 0.0) -> float:
    c = _clean(v)
    return float(c) if c is not None else default


def split_flights(kind: str, ticks: np.ndarray, x: np.ndarray, y: np.ndarray, z: np.ndarray) -> list[Flight]:
    """Rows of one projectile entity -> its flights. Entity ids are reused (split on gaps) and a projectile
    keeps existing after it landed (a smoke for 18 s): only the moving part is a flight."""
    out: list[Flight] = []
    if len(ticks) < 2:
        return out
    order = np.argsort(ticks, kind="stable")
    ticks, x, y, z = ticks[order], x[order], y[order], z[order]
    cuts = np.nonzero(np.diff(ticks) > 8)[0] + 1
    for part in np.split(np.arange(len(ticks)), cuts):
        if len(part) < 3:
            continue
        px, py = x[part], y[part]
        moving = np.nonzero(np.hypot(px - px[-1], py - py[-1]) > 2.0)[0]
        if len(moving) == 0:
            continue
        keep = part[: min(len(part), int(moving[-1]) + 2)]
        out.append(Flight(kind, ticks[keep].astype(np.int64), x[keep].astype(np.float32), y[keep].astype(np.float32),
                          z[keep].astype(np.float32)))
    return out


def _flights(parser: DemoParser, windows: list[tuple[int, int]]) -> list[Flight]:
    try:
        df = parser.parse_grenades()
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(df, pd.DataFrame) or df.empty or "grenade_type" not in df:
        return []
    tick = df["tick"].to_numpy()
    wanted = np.zeros(len(df), dtype=bool)
    for lo, hi in windows:
        wanted |= (tick >= lo - 192) & (tick <= hi)
    df = df[wanted & df["grenade_type"].isin(list(_PROJECTILES)).to_numpy() & df["x"].notna().to_numpy()]
    out: list[Flight] = []
    for (_eid, typ), g in df.groupby(["grenade_entity_id", "grenade_type"], sort=False):
        out.extend(split_flights(_PROJECTILES[str(typ)], g["tick"].to_numpy(), g["x"].to_numpy(dtype=np.float64),
                                 g["y"].to_numpy(dtype=np.float64), g["z"].to_numpy(dtype=np.float64)))
    return out


def load_events(demo_path: Path, windows: list[tuple[int, int]] | None = None) -> Events:
    """World events of the whole demo. ``windows`` (tick ranges that will be drawn) limits the costly part,
    the grenade trajectories."""
    parser = DemoParser(str(demo_path))
    raw = _all_events(parser)
    ev = Events()

    def areas(start_event: str, end_event: str | None, default_ticks: int, max_ticks: int) -> list[Area]:
        ends: dict[int, list[int]] = {}
        if end_event:
            for row in raw[end_event]:
                ends.setdefault(int(_num(row.get("entityid"), -1)), []).append(int(row["tick"]))
        out = []
        for row in raw[start_event]:
            x, y = _clean(row.get("x")), _clean(row.get("y"))
            if x is None or y is None:
                continue
            start = int(row["tick"])
            later = [t for t in ends.get(int(_num(row.get("entityid"), -2)), []) if start < t <= start + max_ticks]
            out.append(Area(start, min(later) if later else start + default_ticks, float(x), float(y),
                            _num(row.get("z"))))
        return out

    ev.smokes = areas("smokegrenade_detonate", "smokegrenade_expired", SMOKE_TICKS, 22 * 64)
    ev.fires = areas("inferno_startburn", "inferno_expire", FIRE_TICKS, 8 * 64)
    ev.flashes = areas("flashbang_detonate", None, 28, 28)
    ev.hes = areas("hegrenade_detonate", None, 34, 34)

    for row in raw["weapon_fire"]:
        if not weapons.is_bullet_weapon(row.get("weapon")):
            continue
        pid = player_id(row.get("user_steamid"), row.get("user_name"))
        x, y, yaw = _clean(row.get("user_X")), _clean(row.get("user_Y")), _clean(row.get("user_yaw"))
        if pid is None or x is None or y is None or yaw is None:
            continue
        ev.shots.append(Shot(int(row["tick"]), pid, float(x), float(y), _num(row.get("user_Z")), float(yaw)))
    ev.shots.sort(key=lambda s: s.tick)
    ev.shot_ticks = [s.tick for s in ev.shots]

    for row in raw["player_hurt"]:
        victim = player_id(row.get("user_steamid"), row.get("user_name"))
        x, y = _clean(row.get("user_X")), _clean(row.get("user_Y"))
        if victim is None or x is None or y is None:
            continue
        ev.hurts.append(Hurt(int(row["tick"]), player_id(row.get("attacker_steamid"), row.get("attacker_name")), victim,
                             int(_num(row.get("dmg_health"))), float(x), float(y), _num(row.get("user_Z"))))
    ev.hurts.sort(key=lambda h: h.tick)
    ev.hurt_ticks = [h.tick for h in ev.hurts]

    for row in raw["bomb_planted"]:
        x, y = _clean(row.get("user_X")), _clean(row.get("user_Y"))
        if x is not None and y is not None:
            ev.bomb_pos[int(row["tick"])] = (float(x), float(y), _num(row.get("user_Z")))
    for row in raw["bomb_dropped"]:
        x, y = _clean(row.get("user_X")), _clean(row.get("user_Y"))
        if x is not None and y is not None:
            ev.drops.append((int(row["tick"]), float(x), float(y), _num(row.get("user_Z"))))
    ev.drops.sort()
    ev.pickups = sorted(int(row["tick"]) for row in raw["bomb_pickup"])
    if windows:
        ev.flights = _flights(parser, windows)
    return ev


_PROPS = ["X", "Y", "Z", "yaw", "health", "armor_value", "is_alive", "team_num", "active_weapon_name", "has_defuser",
          "flash_duration", "is_defusing", "inventory"]


def load_segment(demo_path: Path, start: int, end: int) -> SegmentData:
    parser = DemoParser(str(demo_path))
    wanted = list(range(start, end + 1))
    try:
        df = parser.parse_ticks(_PROPS, ticks=wanted)
    except Exception:  # noqa: BLE001 - a demo without one of the optional props
        df = parser.parse_ticks(_PROPS[:10], ticks=wanted)
    n = end - start + 1
    tracks: dict[str, Track] = {}
    if df.empty:
        return SegmentData(start, end, tracks)
    df["pid"] = [player_id(s, nm) for s, nm in zip(df["steamid"], df["name"], strict=False)]
    for pid, g in df.groupby("pid", sort=False):
        if not pid:
            continue
        idx = (g["tick"].to_numpy() - start).astype(np.int64)
        ok = (idx >= 0) & (idx < n)
        idx = idx[ok]
        g = g[ok]

        def arr(col: str, dtype, fill=0, _g=g, _idx=idx):
            a = np.full(n, fill, dtype=dtype)
            if col in _g:
                a[_idx] = pd.to_numeric(_g[col], errors="coerce").fillna(fill).to_numpy().astype(dtype)
            return a

        def flag(col: str, _g=g, _idx=idx):
            a = np.zeros(n, dtype=bool)
            if col in _g:
                a[_idx] = _g[col].fillna(False).to_numpy().astype(bool)
            return a

        present = np.zeros(n, dtype=bool)
        present[idx] = True
        weapon = [""] * n
        for i, w in zip(idx, g["active_weapon_name"].tolist(), strict=False):
            weapon[i] = w if isinstance(w, str) else ""
        has_c4 = np.zeros(n, dtype=bool)
        if "inventory" in g:
            has_c4[idx] = [isinstance(inv, (list, tuple, np.ndarray)) and any(str(it).startswith("C4") for it in inv)
                           for inv in g["inventory"].tolist()]
        name = next((v for v in g["name"].tolist() if isinstance(v, str)), str(pid))
        tracks[str(pid)] = Track(pid=str(pid), name=name, x=arr("X", np.float32), y=arr("Y", np.float32),
                                 z=arr("Z", np.float32), yaw=arr("yaw", np.float32),
                                 health=arr("health", np.int16), armor=arr("armor_value", np.int16),
                                 alive=flag("is_alive"), present=present, team=arr("team_num", np.int8),
                                 weapon=weapon, defuser=flag("has_defuser"), flash=arr("flash_duration", np.float32),
                                 defusing=flag("is_defusing"), has_c4=has_c4)
    return SegmentData(start, end, tracks)
