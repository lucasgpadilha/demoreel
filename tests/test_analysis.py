from __future__ import annotations

import pytest
from conftest import CT, ROUND_TICKS, T, ace_round, analyze, kill, make_demo

from demoreel.analysis import weapons
from demoreel.analysis.parser import norm_side, player_id


def by_id(analysis):
    return {p.steamid: p for p in analysis.players}


def test_player_id_and_sides():
    assert player_id(76561198000000001) == "76561198000000001"
    assert player_id("76561198000000001") == "76561198000000001"
    assert player_id(float("nan"), "Bot Bob") == "BOT:Bot Bob"
    assert player_id(0, None) is None
    assert norm_side("TERRORIST") == "T" and norm_side(3) == "CT" and norm_side(None) is None


def test_weapon_names():
    assert weapons.normalize("weapon_knife_karambit") == "knife"
    assert weapons.display_name("m4a1_silencer") == "M4A1-S"
    assert weapons.weapon_class("awp") == "sniper"
    assert not weapons.is_bullet_weapon("hegrenade") and weapons.is_bullet_weapon("ak47")


def test_score_and_teams(sample_analysis):
    a = sample_analysis
    assert (a.team_a.score, a.team_b.score) == (1, 1)
    assert a.team_a.starting_side == "CT" and set(a.team_a.players) == set(CT)
    assert a.rounds[0].is_pistol and not a.rounds[1].is_pistol
    assert a.rounds[1].score_team_a == 1 and a.rounds[1].score_team_b == 1


def test_basic_stats(sample_analysis):
    p = by_id(sample_analysis)
    assert p["ct1"].kills == 5 and p["ct1"].deaths == 1
    assert p["ct1"].multi_kills["5k"] == 1
    assert p["ct1"].headshot_kills == 3 and p["ct1"].headshot_pct == 60.0
    assert p["t2"].kills == 3 and p["t2"].multi_kills["3k"] == 1


def test_opening_and_trades(sample_analysis):
    p = by_id(sample_analysis)
    assert p["t1"].opening_kills == 1 and p["ct1"].opening_deaths == 1
    assert p["ct1"].opening_kills == 1  # first kill of round 1
    assert p["ct2"].trade_kills == 1 and p["ct1"].traded_deaths == 1
    r2 = sample_analysis.rounds[1]
    assert r2.kills[0].is_opening and r2.kills[1].is_trade and not r2.kills[2].is_trade


def test_kast(sample_analysis):
    p = by_id(sample_analysis)
    # ct1: round 1 kills, round 2 died but was traded -> 100%
    assert p["ct1"].kast == 100.0
    # t5: died in round 1 without impact, survived round 2 -> 50%
    assert p["t5"].kast == 50.0
    # ct5: survived round 1, died untraded in round 2 -> 50%
    assert p["ct5"].kast == 50.0


def test_clutch_detection(sample_analysis):
    r1, r2 = sample_analysis.rounds
    lost = [c for c in r2.clutches if c.player.steamid == "ct5"]
    assert len(lost) == 1 and lost[0].versus == 4 and not lost[0].won and not lost[0].survived
    # In round 1 the last terrorist was left 1v5 and lost.
    assert any(c.player.side == "T" and c.versus == 5 and not c.won for c in r1.clutches)
    assert by_id(sample_analysis)["ct5"].clutches_attempted == 1


def test_clutch_won():
    base = 2000
    kills = [kill(base + i * 100, 1, "t1", ct) for i, ct in enumerate(CT[:4])]          # ct5 left 1v5
    kills += [kill(base + 1000 + i * 300, 1, "ct5", t) for i, t in enumerate(T)]        # …and wins it
    a = analyze(make_demo([("CT", kills)]))
    c = next(c for c in a.rounds[0].clutches if c.player.steamid == "ct5")
    assert c.won and c.versus == 5 and c.kills == 5 and c.survived
    p = by_id(a)["ct5"]
    assert p.clutches_won == 1 and p.clutches_by_size == {"1v5": 1}
    top = a.highlights[0]
    assert top.player.steamid == "ct5" and top.kind == "ace" and "clutch_1v5" in top.tags


def test_damage_is_adr_source():
    dmg = [{"tick": 2000, "round": 1, "attacker": "ct1", "victim": "t1", "attacker_side": "CT", "victim_side": "T",
            "damage": 80, "weapon": "ak47", "hitgroup": "chest", "is_friendly": False},
           {"tick": 2001, "round": 1, "attacker": "ct1", "victim": "ct2", "attacker_side": "CT", "victim_side": "CT",
            "damage": 50, "weapon": "hegrenade", "hitgroup": "generic", "is_friendly": True}]
    a = analyze(make_demo([("CT", []), ("CT", [])], damages=dmg))
    p = by_id(a)["ct1"]
    assert p.damage == 80 and p.adr == 40.0  # friendly fire ignored, 2 rounds played
    assert p.utility.he_damage == 0


def test_teamkill_and_suicide_do_not_count():
    kills = [kill(2000, 1, "ct1", "ct2"), kill(2100, 1, None, "t1")]
    a = analyze(make_demo([("CT", kills)]))
    p = by_id(a)
    assert p["ct1"].kills == 0 and p["ct1"].team_kills == 1
    assert p["ct2"].deaths == 1 and p["t1"].deaths == 1
    assert not any(k.is_opening for k in a.rounds[0].kills)


def test_side_swap_keeps_teams():
    demo = make_demo([ace_round(1), ("CT", [])])
    r2 = demo.rounds[1]
    r2.sides = {**{p: "T" for p in CT}, **{p: "CT" for p in T}}  # teams switched sides
    a = analyze(demo)
    assert a.rounds[1].ct_team == "team_b" and a.rounds[1].winner_team == "team_b"
    assert (a.team_a.score, a.team_b.score) == (1, 1)
    assert a.rounds[1].is_pistol  # first round after the swap


def test_ace_highlight(sample_analysis):
    top = sample_analysis.highlights[0]
    assert top.kind == "ace" and top.player.steamid == "ct1" and top.round == 1
    plays = [s for s in top.segments if s.role == "play"]
    assert len(top.kill_ticks) == 5 and len(plays) == 1
    # an ace is a play of the match: its last kill is shown again, slowed down, from the victim's eyes
    replay = top.segments[-1]
    assert replay.role == "replay" and replay.pov_steamid == "t5" and replay.speed == 0.5
    assert replay.start_tick < top.kill_ticks[-1] < replay.end_tick and "victim_replay" in top.tags
    seg = plays[0]
    assert seg.pov_steamid == "ct1"
    assert seg.start_tick < top.kill_ticks[0] and seg.end_tick > top.kill_ticks[-1]
    assert "ACE" in top.title


def test_far_apart_kills_become_separate_segments():
    base = 2000
    kills = [kill(base, 1, "ct1", "t1"), kill(base + 64, 1, "ct1", "t2"), kill(base + 64 * 30, 1, "ct1", "t3")]
    a = analyze(make_demo([("CT", kills)]))
    h = next(h for h in a.highlights if h.player.steamid == "ct1")
    assert h.kind == "triple_kill" and len(h.segments) == 2
    assert h.segments[0].end_tick < h.segments[1].start_tick
    assert all(0 <= s.start_tick < s.end_tick <= ROUND_TICKS for s in h.segments)


def test_single_boring_kill_is_not_a_highlight():
    a = analyze(make_demo([("CT", [kill(2000, 1, "ct1", "t1")])]))
    assert a.highlights == []


def test_flashy_single_kill_is_a_highlight():
    a = analyze(make_demo([("CT", [kill(2000, 1, "ct1", "t1", weapon="awp", noscope=True, through_smoke=True,
                                         headshot=True, wallbang=True)])]))
    assert len(a.highlights) == 1 and a.highlights[0].kind == "special_kill"
    assert {"noscope", "through_smoke", "wallbang"} <= set(a.highlights[0].tags)


@pytest.mark.demo
def test_real_demo_analysis():
    from conftest import real_demos

    from demoreel.analysis import analyze_demo

    demos = real_demos()
    if not demos:
        pytest.skip("no demos/*.dem available")
    a = analyze_demo(min(demos, key=lambda p: p.stat().st_size))
    assert a.rounds_played >= 1 and a.tickrate == 64
    assert a.team_a.score + a.team_b.score == a.rounds_played
    assert len(a.players) >= 2 and all(p.rounds_played > 0 for p in a.players)
    assert sum(p.kills for p in a.players) > 0
    assert all(0 <= p.kast <= 100 and p.adr >= 0 for p in a.players)
    for h in a.highlights:
        assert h.segments and all(0 <= s.start_tick < s.end_tick <= a.total_ticks for s in h.segments)
    assert T and CT  # keep imports used


def test_incomplete_demo_keeps_real_scores_and_round_numbers():
    """A demo that only holds rounds 1 and 19–20 of a match: the scoreboard recorded in it is the truth."""
    demo = make_demo([ace_round(1), ("CT", []), ("T", [])])
    r1, r19, r20 = demo.rounds
    r1.score_after = {"CT": 1, "T": 0}
    r19.number, r19.score_after = 19, {"CT": 7, "T": 12}   # sides already swapped at half time
    r20.number, r20.score_after = 20, {"CT": 7, "T": 13}
    for r in (r19, r20):
        r.sides = {**{p: "T" for p in CT}, **{p: "CT" for p in T}}
    a = analyze(demo)
    assert [r.number for r in a.rounds] == [1, 19, 20]
    assert (a.team_a.score, a.team_b.score) == (13, 7) and a.team_a.won is True  # not 2:1 from counting rounds
    assert (a.rounds[1].score_team_a, a.rounds[1].score_team_b) == (12, 7)
    assert a.rounds[0].is_pistol and not a.rounds[1].is_pistol  # round 19 is not "the first after the swap"
    assert by_id(a)["ct1"].rounds_played == 3  # per-round stats cover what the demo holds
