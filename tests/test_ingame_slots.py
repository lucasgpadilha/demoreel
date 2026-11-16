"""SteamID -> ``spec_player`` slot mapping."""

from __future__ import annotations

from pathlib import Path

import pytest

from demoreel.render.ingame import slots

DEMOS = sorted((Path(__file__).resolve().parents[1] / "demos").glob("*.dem"))


def test_slot_is_user_id_plus_one():
    assert slots.spec_slot_from_user_id(0) == 1
    assert slots.spec_slot_from_user_id(11) == 12
    assert slots.spec_slot_from_user_id(63) == 64
    assert slots.spec_slot_from_user_id(0x0105) == 6  # upper bits of a raw userid are a connection counter
    assert slots.spec_slot_from_user_id(7.0) == 8  # pandas may hand back floats


def test_invalid_user_ids_have_no_slot():
    for bad in (None, float("nan"), "abc", 64, 200):
        assert slots.spec_slot_from_user_id(bad) is None


def test_slot_table_prefers_the_exact_tick_then_the_nearest_then_any():
    table = slots.SlotTable()
    table.add(1000, "A", 3)
    table.add(9000, "A", 7)  # reconnected in another slot later in the match
    table.add(9000, "B", 4)
    assert table("A", 1000) == 3 and table("A", 9000) == 7
    assert table("A", 8000) == 7 and table("A", 2000) == 3
    assert table("B", 1000) == 4  # not present at the nearest tick: fall back to any known slot
    assert table("C", 1000) is None
    assert table.as_dict() == {"A": 3, "B": 4}
    assert slots.SlotTable()("A", 1) is None


@pytest.mark.demo
@pytest.mark.skipif(not DEMOS, reason="no demo in demos/")
def test_slots_from_a_real_demo_are_unique_controller_indexes():
    from demoparser2 import DemoParser

    demo = DEMOS[0]
    tick = 20000
    table = slots.resolve_slots(demo, [tick])
    found = table.as_dict()
    assert len(found) >= 2
    assert all(1 <= slot <= slots.MAX_PLAYER_SLOTS for slot in found.values())
    assert len(set(found.values())) == len(found)  # one slot per player
    # Same players as the rest of the analysis sees at that tick, keyed by the same SteamID strings.
    frame = DemoParser(str(demo)).parse_ticks(["user_id"], ticks=[tick])
    assert {str(int(s)) for s in frame["steamid"]} == set(found)
    assert all(table(steamid, tick) == int(uid) + 1 for steamid, uid in zip(
        (str(int(s)) for s in frame["steamid"]), frame["user_id"], strict=True))
    assert slots.demo_patch_version(demo) is not None


def test_demo_patch_version_of_a_non_demo_is_none(tmp_path):
    fake = tmp_path / "fake.dem"
    fake.write_bytes(b"not a demo")
    assert slots.demo_patch_version(fake) is None
