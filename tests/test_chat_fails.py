"""Chat classification, fails and the replay from the victim's eyes (synthetic rounds)."""

from __future__ import annotations

from conftest import CT, ROUND_TICKS, T, ace_round, analyze, kill, make_demo

from demoreel.analysis import chat, fails, highlights, stats


def base(rnd: int) -> int:
    return (rnd - 1) * ROUND_TICKS + 2000


def kind_of(text: str) -> str:
    return chat.classify(text)[0]


# ------------------------------------------------------------------------------------------------ chat

def test_chat_lines_are_classified():
    assert kind_of("!unpause") == "command" and kind_of(".empate") == "command"
    assert kind_of("kkkkkkkkkk") == "laugh" and kind_of("k") == "laugh" and kind_of("xD") == "laugh"
    assert kind_of("ta com aimbot ai") == "whine" and kind_of("xiter") == "whine" and kind_of("TÁ DE GODMODE") == "whine"
    assert kind_of("jogou bosta") == "toxic" and kind_of("vssfffffffffffff pedro desgraçado") == "toxic"
    assert kind_of("vai toma no cu henrique") == "toxic" and kind_of("ez") == "toxic" and kind_of("gorila") == "toxic"
    assert kind_of("boa") == "praise" and kind_of("aceeeeee") == "praise"
    assert kind_of("gg") == "gg" and kind_of("ja volto") == "talk"
    both = chat.classify("ta com aimbot ai o lazarento")
    assert both[0] == "toxic" and {"toxic", "whine"} <= set(both[1])  # crying and swearing in one line
    assert "shouting" in chat.classify("CARA VIRO O SNIPER AMERICANO")[1]


def test_slurs_are_masked_and_the_rest_is_left_as_typed():
    assert chat.display_text("jogou bosta") == "jogou bosta"
    masked = chat.display_text("seu viado")
    assert masked.startswith("seu v") and "viado" not in masked and "*" in masked


def test_chat_is_linked_to_the_kill_it_reacts_to():
    r = base(1)
    demo = make_demo([("CT", [kill(r, 1, "ct1", "t1"), kill(r + 3000, 1, "ct2", "t2")])])
    demo.chat = [
        {"tick": r + 64 * 5, "name": "T1", "steamid": "t1", "text": "aff sorte"},  # the victim, 5 s later
        {"tick": r + 64 * 6, "name": "CT1", "steamid": "ct1", "text": "kkkkk"},  # the killer laughs
        {"tick": r + 64 * 8, "name": "T3", "steamid": "t3", "text": "!kit"},  # a command: never a reaction
        {"tick": r + 64 * 40, "name": "T4", "steamid": "t4", "text": "bora"},  # long after: reacts to nothing
    ]
    a = analyze(demo)
    lines = chat.annotate(demo.chat, a.rounds, 64)
    cry, laugh, cmd, late = lines
    assert (cry["kind"], cry["relation"], cry["kill_tick"], cry["round"]) == ("whine", "victim", r, 1)
    assert (laugh["kind"], laugh["relation"], laugh["kill_victim"]) == ("laugh", "killer", "t1")
    assert cmd["kind"] == "command" and cmd["kill_tick"] is None
    assert late["kill_tick"] is None
    got = chat.reactions(lines, [r])
    assert [m["name"] for m in got] == ["T1", "CT1"] and got[0]["relation"] == "victim"
    assert chat.reactions(lines, [r + 3000]) == []


# ----------------------------------------------------------------------------------------------- fails

def fails_of(demo):
    _, _, _, rounds = stats.compute(demo)
    return fails.detect(demo, rounds)


def test_emptying_a_magazine_at_someone_close_and_dying_is_a_whiff():
    r = base(1)
    demo = make_demo([("CT", [kill(r, 1, "ct1", "t1", distance_m=6.0)])])
    demo.shots = [{"tick": r - 100 + i * 8, "round": 1, "player": "t1", "weapon": "ak47"} for i in range(12)]
    (whiff,) = fails_of(demo)
    assert whiff.kind == "fail_whiff" and whiff.category == "fail" and whiff.player.steamid == "t1"
    assert whiff.segments[0].pov_steamid == "t1" and "12 shots" in whiff.title and whiff.score >= 40
    assert whiff.segments[0].start_tick < r - 100 and whiff.segments[0].end_tick > r
    assert whiff.facts == {"shots": 12, "weapon": "ak47", "weapon_name": "AK-47", "distance_m": 6.0,
                           "killer": {"steamid": "ct1", "name": "CT1"}}  # data, so the UI needs no English prose
    # he did hit somebody: not a whiff
    demo.damages = [{"tick": r - 40, "round": 1, "attacker": "t1", "victim": "ct1", "attacker_side": "T",
                     "victim_side": "CT", "damage": 27, "weapon": "ak47", "hitgroup": None, "is_friendly": False}]
    assert fails_of(demo) == []
    # far away: not a whiff either
    far = make_demo([("CT", [kill(r, 1, "ct1", "t1", distance_m=30.0)])])
    far.shots = demo.shots
    assert fails_of(far) == []


def test_a_flash_that_gets_a_team_mate_killed_blames_the_thrower_and_shows_the_victim():
    r = base(1)
    demo = make_demo([("CT", [kill(r + 100, 1, "ct1", "t2")])])
    demo.blinds = [{"tick": r, "round": 1, "attacker": "t1", "victim": "t2", "duration": 3.5,
                    "attacker_side": "T", "victim_side": "T"}]
    (f,) = fails_of(demo)
    assert f.kind == "fail_team_flash" and f.player.steamid == "t1" and f.segments[0].pov_steamid == "t2"
    assert f.segments[0].caption == "Flash: T1" and f.featured
    assert f.facts["thrower"]["steamid"] == "t1" and f.facts["blinded"]["name"] == "T2" and f.facts["blind_s"] == 3.5
    demo.blinds[0]["attacker"] = "t2"  # his own flash
    assert fails_of(demo)[0].kind == "fail_self_flash"
    demo.blinds[0].update(attacker="ct3", attacker_side="CT")  # an enemy flash is just a good flash
    assert fails_of(demo) == []


def test_team_kills_own_grenades_and_knife_out_deaths():
    r = base(2)  # round 1 is the pistol round, where running with the knife out is normal
    tk = kill(r, 2, "ct1", "ct2")
    nade = kill(r + 500, 2, "t1", "t1", weapon="hegrenade")
    knife_out = kill(r + 900, 2, "ct3", "t2", victim_weapon="knife", distance_m=4.0)
    found = {f.kind: f for f in fails_of(make_demo([ace_round(1), ("CT", [tk, nade, knife_out])]))}
    assert set(found) == {"fail_teamkill", "fail_own_grenade", "fail_knife_out"}
    assert found["fail_teamkill"].player.steamid == "ct1" and found["fail_own_grenade"].player.steamid == "t1"
    assert found["fail_own_grenade"].score > found["fail_teamkill"].score > found["fail_knife_out"].score
    featured = [f for f in found.values() if f.featured]
    assert len(featured) == 2 and {f.featured_rank for f in featured} == {1, 2}  # the two funniest get POV videos
    # a restart "kills" everybody on one tick: that is not a fail
    restart = [kill(r, 1, p, p, weapon="world") for p in CT[:3]]
    assert fails_of(make_demo([("CT", restart)])) == []


# ---------------------------------------------------------------------------------------------- replay

def test_only_plays_of_the_match_get_a_replay_and_at_most_three():
    a = analyze(make_demo([ace_round(n) for n in range(1, 6)]))  # five aces: every one is tier S
    with_replay = [h for h in a.highlights if any(s.role == "replay" for s in h.segments)]
    assert len(with_replay) == highlights.HighlightConfig().replays_per_match == 3
    r = base(1)
    plain = analyze(make_demo([("CT", [kill(r + i * 80, 1, "ct1", T[i]) for i in range(3)]
                               + [kill(r + 400, 1, "ct2", "t4"), kill(r + 480, 1, "ct2", "t5")])]))
    assert all(s.role == "play" for h in plain.highlights for s in h.segments)  # a 3K is not a play of the match


def test_the_replay_shows_the_kill_people_will_want_to_see_again():
    r = base(1)
    kills = [kill(r + i * 80, 1, "ct1", T[i], wallbang=(i == 1)) for i in range(5)]
    a = analyze(make_demo([("CT", kills)]))
    replay = a.highlights[0].segments[-1]
    assert replay.role == "replay" and replay.pov_steamid == "t2"  # the wallbang, not the last kill
    assert replay.start_tick == r + 80 - 3 * 64 and replay.end_tick == r + 80 + 32 and replay.speed == 0.5
    assert a.highlights[0].duration_s > 10  # slow motion counts double
