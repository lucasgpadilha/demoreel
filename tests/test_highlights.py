"""Highlight scoring model and curation (synthetic rounds; definitions in docs/metrics.md)."""

from __future__ import annotations

from conftest import CT, ROUND_TICKS, T, ace_round, analyze, kill, make_demo

from demoreel.analysis import highlights
from demoreel.analysis.highlights import HighlightConfig, select_featured, tier_of, win_probability
from demoreel.analysis.schema import HighlightCandidate, HighlightSegment, PlayerRef


def base(rnd: int) -> int:
    return (rnd - 1) * ROUND_TICKS + 2000


def play(a, steamid: str, rnd: int):
    return next(h for h in a.highlights if h.player.steamid == steamid and h.round == rnd)


# ---------------------------------------------------------------------------------------- round model

def test_win_probability_reference_points():
    assert win_probability(5, 5) == 0.5 and win_probability(1, 1) == 0.5
    assert 0.68 < win_probability(5, 4) < 0.72 and 0.68 < win_probability(4, 3) < 0.72
    assert 0.83 < win_probability(2, 1) < 0.87 and 0.13 < win_probability(1, 2) < 0.17
    assert win_probability(1, 3) < 0.05 and win_probability(1, 5) < 0.002
    assert win_probability(3, 0) == 1.0 and win_probability(0, 2) == 0.0
    assert win_probability(1, 1, bomb=1) > 0.5 > win_probability(1, 1, bomb=-1)  # the plant helps the terrorists
    for own in range(1, 6):  # more enemies is never better
        assert all(win_probability(own, e) > win_probability(own, e + 1) for e in range(1, 5))


def test_the_same_three_kills_are_worth_more_when_outnumbered():
    # Round 1: ct1 gets three kills in a 5v5 that his team wins anyway.
    even = ("CT", [kill(base(1) + i * 100, 1, "ct1", T[i]) for i in range(3)]
            + [kill(base(1) + 400, 1, "ct2", "t4"), kill(base(1) + 500, 1, "ct2", "t5")])
    # Round 2: four CTs die first; ct1 then kills three of the remaining... and the last two fall to him too.
    r2 = base(2)
    alone = ("CT", [kill(r2 + i * 50, 2, "t1", CT[i + 1]) for i in range(4)]
             + [kill(r2 + 400, 2, "ct1", "t1"), kill(r2 + 500, 2, "ct1", "t2"), kill(r2 + 600, 2, "ct1", "t3"),
                kill(r2 + 700, 2, "ct1", "t4"), kill(r2 + 800, 2, "ct1", "t5")])
    a = analyze(make_demo([even, alone]))
    easy, hard = play(a, "ct1", 1), play(a, "ct1", 2)
    assert hard.breakdown["impact"] > easy.breakdown["impact"] * 1.5
    assert hard.score > easy.score and hard.tier == "S"


def test_finishing_off_the_last_enemy_five_against_one_is_worth_almost_nothing():
    r = base(1)
    kills = [kill(r + i * 100, 1, CT[i], T[i]) for i in range(3)]  # 5v5 -> 5v2 by three different players
    kills += [kill(r + 400, 1, "ct4", "t4"), kill(r + 420, 1, "ct4", "t5")]  # ct4 cleans up 5v2 -> 5v0
    a = analyze(make_demo([("CT", kills)]))
    cleanup = play(a, "ct4", 1) if any(h.player.steamid == "ct4" for h in a.highlights) else None
    assert cleanup is None or cleanup.breakdown["impact"] < 8  # 2 kills, but the round was already won


def test_a_great_play_in_a_lost_round_counts_less():
    def round_(winner):
        r = base(1)
        return (winner, [kill(r + i * 100, 1, "ct1", T[i]) for i in range(3)])

    won = play(analyze(make_demo([round_("CT")])), "ct1", 1)
    lost = play(analyze(make_demo([round_("T")])), "ct1", 1)
    assert lost.breakdown["outcome"] < won.breakdown["outcome"] == 1.0 and lost.score < won.score * 0.8


def test_farming_an_eco_round_counts_less_and_beating_rifles_on_an_eco_counts_more():
    assert highlights._economy("full", "eco") == 0.78 and highlights._economy("eco", "full") == 1.2
    assert highlights._economy("full", "full") == 1.0 and highlights._economy(None, "full") == 1.0
    assert highlights._economy("pistol", "pistol") == 1.0  # pistol rounds are judged on the stakes instead


def test_an_ace_is_always_a_play_of_the_match():
    a = analyze(make_demo([ace_round(1)]))
    ace = a.highlights[0]
    assert ace.kind == "ace" and ace.tier == "S" and ace.featured and ace.featured_rank == 1
    assert ace.score >= HighlightConfig().tier_s


def test_breakdown_explains_the_score():
    a = analyze(make_demo([ace_round(1)]))
    b = a.highlights[0].breakdown
    assert set(b) == {"feat", "impact", "style", "economy", "stakes", "outcome", "penalty", "impact_max", "win_before"}
    assert b["win_before"] == 0.5 and b["impact_max"] == 40  # the ace started five against five
    computed = (b["feat"] + b["impact"] + b["style"]) * b["economy"] * b["stakes"] * b["outcome"] - b["penalty"]
    assert abs(min(100.0, computed) - a.highlights[0].score) < 0.6 or a.highlights[0].score == HighlightConfig().tier_s
    assert a.highlights[0].team in ("team_a", "team_b")


def test_stakes_rise_at_match_point_and_fall_in_a_blowout():
    from demoreel.analysis.schema import RoundSummary

    def r(number, **kw):
        return RoundSummary(number=number, start_tick=0, freeze_end_tick=0, end_tick=1, official_end_tick=1,
                            duration_s=1, winner_side="CT", winner_team="team_a", reason="x", ct_team="team_a",
                            t_team="team_b", score_team_a=0, score_team_b=0, **kw)

    assert highlights._stakes(r(5), (2, 2), 24, True)[0] == 1.0
    assert highlights._stakes(r(1, is_pistol=True), (0, 0), 24, True) == (1.08, ["pistol_round"])
    assert highlights._stakes(r(23), (12, 10), 24, True)[1] == ["match_point"]
    assert highlights._stakes(r(20), (10, 9), 24, True)[1] == ["close_game"]
    assert highlights._stakes(r(14), (11, 2), 24, True)[0] < 1.0  # garbage time
    m, tags = highlights._stakes(r(24), (12, 11), 24, True)
    assert tags == ["match_point", "final_round"] and m <= 1.3
    assert "final_round" not in highlights._stakes(r(24), (12, 11), 24, False)[1]  # only for the side that won it


# -------------------------------------------------------------------------------------------- curation

def cand(score: float, team: str, player: str, rnd: int) -> HighlightCandidate:
    return HighlightCandidate(kind="triple_kill", title="t", description="d", round=rnd,
                              player=PlayerRef(steamid=player, name=player), score=score, segments=[
                                  HighlightSegment(start_tick=1, end_tick=2, pov_steamid=player)],
                              start_tick=1, end_tick=2, duration_s=1.0, team=team, tier=tier_of(score))


def picked(cands):
    return [(cands[i].team, cands[i].player.steamid, cands[i].score) for i in select_featured(cands)]


def test_curation_takes_the_best_three_of_each_team():
    cands = [cand(55 - i, "team_a", f"a{i}", i + 1) for i in range(6)] + [cand(50 - i, "team_b", f"b{i}", i + 1) for i in range(6)]
    got = picked(cands)
    assert len(got) == 6 and sum(1 for t, _, _ in got if t == "team_a") == 3
    assert [s for t, _, s in got if t == "team_a"] == [55, 54, 53] and got == sorted(got, key=lambda x: -x[2])


def test_curation_caps_one_player_and_one_round_within_the_quota():
    cands = [cand(58, "team_a", "star", 1), cand(57, "team_a", "star", 2), cand(56, "team_a", "star", 3),
             cand(40, "team_a", "other", 4), cand(39, "team_a", "third", 4)]
    got = picked(cands)
    assert [(p, s) for _, p, s in got] == [("star", 58), ("star", 57), ("other", 40)]  # third star play and the
    # second play of round 4 are left to the 2D replays


def test_stand_out_plays_are_featured_beyond_the_quota_up_to_the_maximum():
    cands = [cand(90 - i, "team_a", "star", i + 1) for i in range(8)] + [cand(35, "team_b", "b", 1)]
    got = picked(cands)
    assert len(got) == 9 and all(s >= 62 for _, _, s in got[:8])  # every tier-S play, whoever made it
    many = [cand(95 - i, "team_a", f"p{i}", i + 1) for i in range(14)]
    assert len(select_featured(many)) == HighlightConfig().featured_max


def test_a_team_without_good_plays_still_gets_its_best_one_but_only_one():
    cands = [cand(50, "team_a", "a1", 1), cand(22, "team_b", "b1", 2), cand(21, "team_b", "b2", 3)]
    assert picked(cands) == [("team_a", "a1", 50), ("team_b", "b1", 22)]
    assert select_featured([]) == []


def test_minor_double_kills_are_dropped_and_ordinary_single_kills_never_appear():
    r = base(1)
    kills = [kill(r, 1, "ct1", "t1"), kill(r + 2000, 1, "ct2", "t2"), kill(r + 2600, 1, "ct2", "t3")]
    a = analyze(make_demo([("CT", kills + [kill(r + 3000, 1, "ct3", "t4"), kill(r + 3100, 1, "ct4", "t5")])]))
    assert all(h.player.steamid != "ct1" for h in a.highlights)  # one plain kill is not a highlight
    assert all(h.score >= HighlightConfig().min_score for h in a.highlights)


# ------------------------------------------------------------------------------------------------ cuts

def seg_demo(kills, shots=()):
    demo = make_demo([("CT", kills)])
    demo.shots = [{"tick": t, "round": 1, "player": p, "weapon": "ak47"} for p, t in shots]
    return demo


def test_clip_starts_shortly_before_the_exchange_not_a_fixed_time_before_the_kill():
    k = base(1) + 1000
    two = [kill(k, 1, "ct1", "t1"), kill(k + 64, 1, "ct1", "t2")]
    # no shots known: the minimum pre-roll (2.5 s) applies
    quiet = play(analyze(seg_demo(two)), "ct1", 1)
    assert quiet.segments[0].start_tick == k - int(2.5 * 64)
    # the player had been shooting for 3 s (a shot every second): 3 s of exchange + 1.5 s of lead-in
    busy = analyze(seg_demo(two, shots=[("ct1", k - 64 * i) for i in range(4)]))
    assert busy.rounds[0].kills[0].fight_start_tick == k - 3 * 64
    assert play(busy, "ct1", 1).segments[0].start_tick == k - int(4.5 * 64)
    # a very long exchange is capped (5.5 s), and a shot 4 s earlier is another exchange, not this one
    long = analyze(seg_demo(two, shots=[("ct1", k - 64 * i) for i in range(10)]))
    assert play(long, "ct1", 1).segments[0].start_tick == k - int(5.5 * 64)
    apart = analyze(seg_demo(two, shots=[("ct1", k - 4 * 64)]))
    assert apart.rounds[0].kills[0].fight_start_tick == k
    # somebody else's shots do not count
    other = analyze(seg_demo(two, shots=[("ct2", k - 64 * i) for i in range(4)]))
    assert play(other, "ct1", 1).segments[0].start_tick == k - int(2.5 * 64)


def test_clip_ends_two_seconds_after_the_last_kill_and_sooner_before_a_cut():
    k = base(1) + 600
    far = k + 12 * 64  # more than max_gap_s (9 s) after the previous kill: a second segment
    kills = [kill(k, 1, "ct1", "t1"), kill(k + 64, 1, "ct1", "t2"), kill(far, 1, "ct1", "t3")]
    h = play(analyze(seg_demo(kills)), "ct1", 1)
    first, second = h.segments
    assert first.end_tick == k + 64 + int(1.2 * 64)  # followed by a cut: just long enough for the kill counter
    assert second.start_tick == far - int(1.5 * 64)  # entered by a cut: shorter lead-in
    assert second.end_tick == far + int(2.0 * 64)


def test_kills_up_to_nine_seconds_apart_stay_in_one_segment():
    # The wait between them is kept (composition plays its quiet stretches fast) rather than cut out.
    k = base(1) + 600
    h = play(analyze(seg_demo([kill(k, 1, "ct1", "t1"), kill(k + 8 * 64, 1, "ct1", "t2")])), "ct1", 1)
    assert [(s.start_tick, s.end_tick) for s in h.segments] == [(k - int(2.5 * 64), k + 8 * 64 + int(2.0 * 64))]
    assert HighlightConfig().max_gap_s == 9.0
    apart = play(analyze(seg_demo([kill(k, 1, "ct1", "t1"), kill(k + 10 * 64, 1, "ct1", "t2")])), "ct1", 1)
    assert len(apart.segments) == 2


def test_clip_ends_right_after_the_player_dies():
    k = base(1) + 600
    kills = [kill(k, 1, "ct1", "t1"), kill(k + 64, 1, "ct1", "t2"), kill(k + 64 + 40, 1, "t3", "ct1")]
    h = play(analyze(make_demo([("T", kills)])), "ct1", 1)
    assert h.segments[-1].end_tick == k + 64 + 40 + int(0.4 * 64)


def test_extra_windows_are_appended_and_merged():
    a = analyze(make_demo([ace_round(1)]))
    r = a.rounds[0]
    cfg = HighlightConfig()
    k = r.kills[0].tick
    apart = highlights._segments([k], "ct1", r, 64, cfg, a.total_ticks, windows=((k + 1500, k + 1700),))
    assert [(s.start_tick, s.end_tick) for s in apart] == [(k - 160, k + int(1.2 * 64)), (k + 1500, k + 1700)]
    touching = highlights._segments([k], "ct1", r, 64, cfg, a.total_ticks, windows=((k + 30, k + 300),))
    assert [(s.start_tick, s.end_tick) for s in touching] == [(k - 160, k + 300)]
    assert highlights._segments([], "ct1", r, 64, cfg, a.total_ticks, windows=((k, k + 200),))[0].end_tick == k + 200


# ----------------------------------------------------------------------------------- rules people asked for

def test_a_knife_kill_is_always_a_highlight_and_a_point_blank_noscope_is_not():
    r = base(1)
    filler = [kill(r + 3000 + i * 400, 1, CT[1 + i % 3], T[1 + i]) for i in range(4)]
    knife = analyze(make_demo([("CT", [kill(r, 1, "ct1", "t1", weapon="knife"), *filler])]))
    h = play(knife, "ct1", 1)
    assert h.kind == "special_kill" and "knife" in h.tags and h.score >= 30
    far = analyze(make_demo([("CT", [kill(r, 1, "ct1", "t1", weapon="awp", noscope=True, distance_m=20.0), *filler])]))
    assert play(far, "ct1", 1).score >= 24
    close = analyze(make_demo([("CT", [kill(r, 1, "ct1", "t1", weapon="awp", noscope=True, distance_m=2.0), *filler])]))
    assert all(h.player.steamid != "ct1" for h in close.highlights)


def test_one_kill_short_of_a_1v3_is_a_play_of_its_own():
    r = base(1)
    kills = [kill(r + i * 40, 1, "t1", CT[i + 1]) for i in range(4)]  # ct1 is left alone against 5... make it 1v3:
    kills = [kill(r, 1, "ct2", "t4"), kill(r + 10, 1, "ct3", "t5")] + [kill(r + 100 + i * 40, 1, "t1", CT[i + 1]) for i in range(4)]
    kills += [kill(r + 600, 1, "ct1", "t1"), kill(r + 700, 1, "ct1", "t2"), kill(r + 900, 1, "t3", "ct1")]
    a = analyze(make_demo([("T", kills)]))
    h = play(a, "ct1", 1)
    assert h.kind == "clutch_attempt" and "almost_1v3" in h.tags and "almost a 1v3" in h.title
    assert h.breakdown["outcome"] == 0.92


def test_the_cap_never_drops_a_clutch():
    cfg = HighlightConfig(max_highlights=1)
    r1 = base(1)
    double = ("CT", [kill(r1 + i * 30, 1, "ct1", T[i]) for i in range(3)] + [kill(r1 + 200, 1, "ct2", "t4"), kill(r1 + 230, 1, "ct2", "t5")])
    r2 = base(2)
    clutch = ("CT", [kill(r2 + i * 40, 2, "t1", CT[i + 1]) for i in range(4)]
              + [kill(r2 + 400 + i * 80, 2, "ct1", T[i]) for i in range(5)])
    demo = make_demo([double, clutch])
    from demoreel.analysis import stats

    _, _, _, rounds = stats.compute(demo)
    found = highlights.detect(rounds, 64, demo.total_ticks, "de_test", cfg)
    assert {h.kind for h in found} >= {"ace"} and len(found) >= 1
    assert all(h.kind in ("ace", "clutch") or i == 0 for i, h in enumerate(sorted(found, key=lambda h: h.kind not in ("ace", "clutch"))))
