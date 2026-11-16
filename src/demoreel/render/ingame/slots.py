"""SteamID -> the number CS2's ``spec_player`` command expects.

How CS Demo Manager derives it (and what is reproduced here):

* cs-demo-analyzer ``pkg/api/player.go``: ``UserID = player.UserID & 0xff`` (the ``userid`` of the player's entry
  in the demo's ``userinfo`` string table; the upper bits are a connection counter), commented "+1 to get the
  player's slot", and ``pkg/api/export_csdm.go`` exports ``player.UserID + 1`` as the player's ``index``.
* cs-demo-manager ``src/node/database/match/fetch-match-players-slots.ts`` reads that ``index`` back as ``slot``
  ("The player slot is the user id + 1 in demos") and ``create-cs2-video-json-file.ts`` emits
  ``spec_player <slot>``.

So ``slot = (userinfo.userid & 0xff) + 1``, which is the entity index of the player's controller. ``demoparser2``
exposes exactly that masked value as the ``user_id`` player prop (its ``parse_userinfo`` also applies ``& 0xff``),
so no second demo parser is needed. Bots have no SteamID and therefore no entry: they cannot be resolved.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

MAX_PLAYER_SLOTS = 64


def spec_slot_from_user_id(user_id: object) -> int | None:
    """``user_id`` (already masked with 0xff) -> ``spec_player`` argument, or ``None`` when invalid."""
    try:
        if user_id is None or user_id != user_id:  # None or NaN
            return None
        value = int(user_id) & 0xFF  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None
    slot = value + 1
    return slot if 1 <= slot <= MAX_PLAYER_SLOTS else None


class SlotTable:
    """Slots per tick (a player who reconnects may come back in another slot) with a per-demo fallback."""

    def __init__(self) -> None:
        self._by_tick: dict[int, dict[str, int]] = {}
        self._any: dict[str, int] = {}

    def add(self, tick: int, steamid: str, slot: int) -> None:
        self._by_tick.setdefault(int(tick), {})[steamid] = slot
        self._any.setdefault(steamid, slot)

    def __call__(self, steamid: str, tick: int) -> int | None:
        at_tick = self._by_tick.get(int(tick), {}).get(steamid)
        if at_tick is not None:
            return at_tick
        if self._by_tick:
            nearest = min(self._by_tick, key=lambda t: abs(t - int(tick)))
            near = self._by_tick[nearest].get(steamid)
            if near is not None:
                return near
        return self._any.get(steamid)

    def as_dict(self) -> dict[str, int]:
        return dict(self._any)


def resolve_slots(demo_path: Path, ticks: Iterable[int]) -> SlotTable:
    """Read the players' slots from the demo at the given ticks (typically each segment's start tick)."""
    from demoparser2 import DemoParser  # noqa: PLC0415 - heavy native module, only needed when rendering

    wanted = sorted({max(1, int(t)) for t in ticks})
    table = SlotTable()
    if not wanted:
        return table
    frame = DemoParser(str(demo_path)).parse_ticks(["user_id"], ticks=wanted)
    for row in frame.itertuples(index=False):
        slot = spec_slot_from_user_id(getattr(row, "user_id", None))
        steamid = getattr(row, "steamid", None)
        if slot is None or steamid is None or steamid != steamid:
            continue
        try:
            steamid_text = str(int(steamid))
        except (TypeError, ValueError):
            continue
        if steamid_text != "0":
            table.add(int(row.tick), steamid_text, slot)
    return table


def demo_patch_version(demo_path: Path) -> int | None:
    """``patch_version`` of the demo header (e.g. 14197 for CS2 1.41.9.7), or ``None``."""
    try:
        from demoparser2 import DemoParser  # noqa: PLC0415

        return int(str(DemoParser(str(demo_path)).parse_header().get("patch_version")).strip())
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001 - informational only; a Rust panic surfaces as a BaseException
        return None
