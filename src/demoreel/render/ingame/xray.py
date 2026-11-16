"""When to show the spectator x-ray (player outlines through walls and smoke) on first-person footage.

A kill through a smoke or a wall is unreadable from the player's eyes alone: the viewer sees someone spraying
at nothing. So by default (``ingame_xray: "auto"``) the outlines are switched on shortly before each such kill
of the player being followed and off again right after it, and stay off for the rest of the footage.

``ingame_xray: true`` keeps them on for the whole recording; ``false`` never shows them.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

LEAD_S = 3.0  # outlines appear this long before the kill: enough to read the spray or the pre-aim
TAIL_S = 1.0  # and stay until the body has dropped

Window = tuple[int, int]


def mode(extra: dict[str, Any] | None) -> str:
    """``"auto"`` (default), ``"on"`` or ``"off"`` from the ``ingame_xray`` option."""
    value = (extra or {}).get("ingame_xray", "auto")
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"1", "true", "yes", "on", "always"}:
            return "on"
        if v in {"0", "false", "no", "off", "never"}:
            return "off"
        return "auto"
    if value is None:
        return "auto"
    return "on" if value else "off"


def match_kills(analysis: Any) -> list[Any]:
    """Every kill of the match, from the analysis (kills are stored per round)."""
    return [k for r in getattr(analysis, "rounds", None) or [] for k in r.kills]


def windows(kills: Iterable[Any], tickrate: float) -> dict[str, list[Window]]:
    """Tick ranges with outlines, per attacker SteamID: around every kill made through smoke or through a wall.

    Computed from the whole match and independent of any clip, so the same play always gets the same ranges
    however it is trimmed."""
    lead, tail = int(LEAD_S * tickrate), int(TAIL_S * tickrate)
    raw: dict[str, list[Window]] = {}
    for k in kills:
        attacker = getattr(k, "attacker", None)
        if attacker is None or not (getattr(k, "through_smoke", False) or getattr(k, "wallbang", False)):
            continue
        raw.setdefault(str(attacker.steamid), []).append((max(1, int(k.tick) - lead), int(k.tick) + tail))
    out: dict[str, list[Window]] = {}
    for steamid, spans in raw.items():
        merged: list[Window] = []
        for a, b in sorted(spans):
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        out[steamid] = merged
    return out


def within(by_pov: dict[str, list[Window]], pov: str | None, start_tick: int, end_tick: int) -> list[Window]:
    """The part of a player's ranges that falls inside ``start_tick..end_tick``."""
    out = []
    for a, b in by_pov.get(str(pov), []) if pov else []:
        lo, hi = max(a, start_tick), min(b, end_tick)
        if hi > lo:
            out.append((lo, hi))
    return out


def needed(kills: Iterable[Any] | None, segments: Sequence[Any], tickrate: float) -> bool:
    """True if footage of these segments differs between x-ray "auto" and "off"."""
    if kills is None:
        return False
    by_pov = windows(kills, tickrate)
    return any(within(by_pov, s.pov_steamid, int(s.start_tick), int(s.end_tick)) for s in segments)
