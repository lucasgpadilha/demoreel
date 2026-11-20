"""Insights across matches (nights, rivalries, profiles, trophies, chat rankings) on synthetic matches."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import anyio
import pytest
from conftest import ROUND_TICKS, analyze, kill, make_demo
from fastapi.testclient import TestClient

from demoreel import insights, service
from demoreel.analysis.schema import HighlightCandidate, HighlightSegment, MatchAnalysis, PlayerRef
from demoreel.config import get_settings
from demoreel.db import Highlight, Match, init_db, session_scope

BRT = timezone(timedelta(hours=-3))
FRIDAY_LATE = datetime(2021, 3, 6, 2, 0, tzinfo=UTC)  # Friday 23:00 in Brazil
SATURDAY_EARLY = datetime(2021, 3, 6, 4, 30, tzinfo=UTC)  # 01:30, still Friday's night
NEXT_FRIDAY = datetime(2021, 3, 13, 0, 0, tzinfo=UTC)  # Friday 12th, 21:00
WINDOW = {"since": "2021-03-01T00:00:00Z", "until": "2021-04-01T00:00:00Z"}  # nothing else in the test database


def base(rnd: int) -> int:
    return (rnd - 1) * ROUND_TICKS + 2000


def wipe(rnd: int, attacker: str, victims: list[str], start: int = 0, **first) -> list[dict]:
    """``attacker`` kills ``victims`` one after the other; ``first`` flags go on the first kill."""
    return [kill(base(rnd) + start + i * 100, rnd, attacker, v, **(first if i == 0 else {})) for i, v in enumerate(victims)]


def night_demo():
    """Seven rounds built so that every trophy has one obvious owner:

    ct1 opens rounds 1-3 with two kills each (6 in a row, 3 headshots), then dies four rounds running, twice
    with the knife out; t1, t2, t3 take four each in rounds 4-6, leaving ct5 alone and useless every time;
    round 7 ct2 takes four and t5 wins the 1v5. ct3 shoots ct4 in round 1; ct3, ct4, ct5 and t4 never get a kill.
    """
    r1 = [kill(base(1), 1, "ct1", "t1", headshot=True), kill(base(1) + 100, 1, "ct1", "t2", headshot=True),
          kill(base(1) + 200, 1, "ct3", "ct4")]
    r2 = [kill(base(2), 2, "ct1", "t1", headshot=True), kill(base(2) + 100, 2, "ct1", "t2")]
    r3 = [kill(base(3), 3, "ct1", "t1", weapon="knife"), kill(base(3) + 100, 3, "ct1", "t2", attacker_health=7)]
    r4 = wipe(4, "t1", ["ct1", "ct2", "ct3", "ct4"], attacker_health=5, victim_weapon="knife")
    r4[1]["attacker_health"] = 9
    r5 = wipe(5, "t2", ["ct1", "ct2", "ct3", "ct4"], victim_weapon="knife")
    r6 = wipe(6, "t3", ["ct1", "ct2", "ct3", "ct4"])
    r7 = wipe(7, "ct2", ["t1", "t2", "t3", "t4"]) + wipe(7, "t5", ["ct1", "ct2", "ct3", "ct4", "ct5"], start=1000)
    return make_demo([("CT", r1), ("CT", r2), ("CT", r3), ("T", r4), ("T", r5), ("T", r6), ("T", r7)])


def whiff(steamid: str, rnd: int) -> HighlightCandidate:
    seg = HighlightSegment(start_tick=base(rnd), end_tick=base(rnd) + 640, pov_steamid=steamid)
    return HighlightCandidate(kind="fail_whiff", title=f"{steamid} — 12 shots, zero hits", description="", round=rnd,
                              player=PlayerRef(steamid=steamid, name=steamid.upper()), score=40.0, segments=[seg],
                              start_tick=seg.start_tick, end_tick=seg.end_tick, duration_s=10.0, category="fail")


CHAT = [  # as stored by an analysis from before the chat model: no kind, no round
    {"tick": 100, "name": "CT2", "steamid": "ct2", "text": "!ready"},
    {"tick": 300, "name": "CT2", "steamid": "ct2", "text": "VAMOS TIME AGORA"},
    {"tick": 500, "name": "CT1", "steamid": "ct1", "text": "seu mongol"},
    {"tick": base(4) + 100, "name": "CT1", "steamid": "ct1", "text": "hack fdp"},  # ct1 has just died to t1
    {"tick": base(4) + 200, "name": "CT1", "steamid": "ct1", "text": "que sorte"},
    {"tick": base(4) + 300, "name": "T1", "steamid": "t1", "text": "kkkkkk"},
    {"tick": base(7) + 1500, "name": "T5", "steamid": "t5", "text": "gg"},
    {"tick": base(7) + 1600, "name": "Zed", "steamid": "BOT:Zed", "text": "lixo"},
    {"tick": base(7) + 1700, "name": "Console", "steamid": None, "text": "hack"},
]


@pytest.fixture(scope="module")
def night_analysis() -> MatchAnalysis:
    a = analyze(night_demo())
    a.fails = [whiff("t4", 2), whiff("t4", 5)]
    a.chat = [dict(row) for row in CHAT]
    return a


def with_bot(a: MatchAnalysis, steamid: str = "t5") -> MatchAnalysis:
    """The same match with one player replaced by a bot."""
    return MatchAnalysis.model_validate_json(a.model_dump_json().replace(f'"{steamid}"', '"BOT:Zed"'))


def entries(a: MatchAnalysis, *more: MatchAnalysis) -> list[insights.Entry]:
    whens = (FRIDAY_LATE, SATURDAY_EARLY, NEXT_FRIDAY)
    return [insights.entry(f"m{i + 1}", x, whens[i], BRT) for i, x in enumerate((a, *more))]


def trophy(out, key: str):
    return next((t for t in out.trophies if t.key == key), None)


# ------------------------------------------------------------------------------------------- nights

def test_a_night_runs_from_noon_to_noon_local_time():
    assert insights.night_of(FRIDAY_LATE, BRT) == date(2021, 3, 5)
    assert insights.night_of(SATURDAY_EARLY, BRT) == date(2021, 3, 5)  # after midnight: still Friday's night
    assert insights.night_of(datetime(2021, 3, 6, 15, 30, tzinfo=UTC), BRT) == date(2021, 3, 6)  # 12:30: a new one
    assert insights.night_of(datetime(2021, 3, 6, 14, 30), BRT) == date(2021, 3, 5)  # naive = UTC, 11:30 local


def test_nights_group_matches_and_leave_bots_out(night_analysis):
    out = insights.nights_of(entries(night_analysis, night_analysis, with_bot(night_analysis)))
    assert [(n.date, n.matches, n.rounds) for n in out] == [("2021-03-12", ["m3"], 7), ("2021-03-05", ["m1", "m2"], 14)]
    assert out[0].players == 9 and out[1].players == 10  # the bot of m3 is nobody
    assert out[1].maps == ["de_test", "de_test"] and out[1].first_played_at == FRIDAY_LATE


# ------------------------------------------------------------------------------------------- rivalries

def test_rivalries_count_kills_both_ways_with_openings_and_humiliations(night_analysis):
    out = insights.rivalries_of(entries(night_analysis, night_analysis))
    assert out.min_duels == 4  # two kills per match in the scope
    top = out.pairs[0]
    assert (top.a.steamid, top.b.steamid, top.a_kills, top.b_kills) == ("ct1", "t1", 6, 2)
    assert (top.duels, top.diff, top.imbalance, top.matches) == (8, 4, 0.5, 2)
    assert (top.a_openings, top.b_openings) == (6, 2)  # ct1 opened rounds 1-3 on t1, t1 opened round 4 on ct1
    assert (top.a_humiliations, top.b_humiliations) == (2, 0)  # the knife of round 3
    assert top.a.name == "CT1"
    # Pairs under the minimum are left out, and a team kill is not a duel.
    assert all(p.duels >= 4 for p in out.pairs)
    everything = insights.rivalries_of(entries(night_analysis), min_duels=1)
    assert not any({p.a.steamid, p.b.steamid} == {"ct3", "ct4"} for p in everything.pairs)
    assert everything.total_pairs == len(everything.pairs) > len(out.pairs)


def test_nemesis_and_favourite_victim(night_analysis):
    out = insights.rivalries_of(entries(night_analysis, night_analysis))
    people = {p.player.steamid: p for p in out.players}
    assert len(people) == 10
    ct1, t1 = people["ct1"], people["t1"]
    assert ct1.victim.player.steamid == "t1" and (ct1.victim.kills, ct1.victim.deaths, ct1.victim.diff) == (6, 2, 4)
    assert ct1.nemesis is None  # t3 and t5 beat him 2-0, under the minimum of 4 duels
    assert t1.nemesis.player.steamid == "ct1" and t1.nemesis.diff == -4 and t1.nemesis.matches == 2
    assert (ct1.kills, ct1.deaths) == (12, 8)
    loose = {p.player.steamid: p for p in insights.rivalries_of(entries(night_analysis), min_duels=1).players}
    assert loose["ct1"].nemesis.player.steamid == "t3"  # 0-1 against t3 and t5: the lower SteamID settles it


def test_rivalries_of_one_player_sorting_and_errors(night_analysis):
    es = entries(night_analysis, night_analysis)
    mine = insights.rivalries_of(es, player="t5", min_duels=1)
    assert mine.pairs and all("t5" in (p.a.steamid, p.b.steamid) for p in mine.pairs)
    assert [p.player.steamid for p in mine.players] == ["t5"]
    by_imbalance = insights.rivalries_of(es, min_duels=1, sort="imbalance", limit=3)
    assert len(by_imbalance.pairs) == 3 and by_imbalance.pairs[0].imbalance == 1.0
    assert by_imbalance.total_pairs > 3
    with pytest.raises(service.NotFound) as err:
        insights.rivalries_of(es, player="nobody")
    assert "CT1 (ct1)" in err.value.message
    with pytest.raises(service.ServiceError) as err:
        insights.rivalries_of(es, sort="luck")
    assert "duels, diff, imbalance" in err.value.message


def test_bots_are_in_no_rivalry(night_analysis):
    out = insights.rivalries_of(entries(with_bot(night_analysis)), min_duels=1)
    ids = {x for p in out.pairs for x in (p.a.steamid, p.b.steamid)} | {p.player.steamid for p in out.players}
    assert len(out.players) == 9 and not any(i.startswith("BOT:") for i in ids)
    with pytest.raises(service.NotFound):
        insights.rivalries_of(entries(with_bot(night_analysis)), player="BOT:Zed")


# ------------------------------------------------------------------------------------------- trophies

def test_trophies_of_a_match(night_analysis):
    out = insights.trophies_of(entries(night_analysis))
    assert out.kind == "match" and out.scope.matches == ["m1"] and out.scope.rounds == 7
    best = max(night_analysis.players, key=lambda p: p.rating)
    assert trophy(out, "mvp").player.steamid == best.steamid and trophy(out, "mvp").value == best.rating

    streak = trophy(out, "kill_streak")
    assert (streak.player.steamid, streak.value, streak.match_id, streak.round) == ("ct1", 6, "m1", 1)
    assert "rounds 1–3" in streak.detail and "in a row" in streak.detail_en
    assert [(r.player.steamid, r.value) for r in streak.runners_up][0] == ("t5", 5)

    drought = trophy(out, "drought")  # ct3, ct4, ct5 and t4 all went seven rounds: the lower SteamID takes it
    assert (drought.player.steamid, drought.value) == ("ct3", 7)
    assert [r.player.steamid for r in drought.runners_up] == ["ct4", "ct5"]

    assert (trophy(out, "entry").player.steamid, trophy(out, "entry").value) == ("ct1", 3)
    clutch = trophy(out, "clutch_king")
    assert (clutch.player.steamid, clutch.value) == ("t5", 1) and "1v5" in clutch.detail
    low = trophy(out, "low_hp")  # t1 killed on 5 and 9 HP; ct1's single kill on 7 HP is not enough
    assert (low.player.steamid, low.value) == ("t1", 2) and "5" in low.detail and not low.runners_up
    fire = trophy(out, "friendly_fire")
    assert (fire.player.steamid, fire.value) == ("ct3", 1) and "CT4" in fire.detail
    assert (trophy(out, "knife_out").player.steamid, trophy(out, "knife_out").value) == ("ct1", 2)
    assert (trophy(out, "baiter").player.steamid, trophy(out, "baiter").value) == ("ct5", 4)
    assert (trophy(out, "whiff").player.steamid, trophy(out, "whiff").value) == ("t4", 2)
    assert trophy(out, "kill_streak").title_pt == "Imparável" and trophy(out, "baiter").title_en == "Baiter"


def test_no_trophy_under_its_minimum(night_analysis):
    out = insights.trophies_of(entries(night_analysis))
    # Nobody reached 10 kills (headshots), nobody flashed a team-mate, no shots or money in a synthetic demo.
    for key in ("headshot_machine", "team_flasher", "sharp_aim", "economist"):
        assert trophy(out, key) is None
    assert {t.key for t in out.trophies} <= set(insights.TROPHIES)
    bare = analyze(make_demo([("CT", [kill(base(1), 1, "ct1", "t1")])]))
    assert [t.key for t in insights.trophies_of(entries(bare)).trophies] == ["mvp"]


def test_trophies_of_a_night_add_up_and_are_deterministic(night_analysis):
    es = entries(night_analysis, night_analysis)
    out = insights.trophies_of(es)
    assert out.kind == "night" and out.scope.nights == ["2021-03-05"] and out.scope.rounds == 14
    assert trophy(out, "entry").value == 6 and trophy(out, "baiter").value == 8 and trophy(out, "whiff").value == 4
    streak = trophy(out, "kill_streak")  # a streak does not carry over to the next match; the first one stays
    assert (streak.value, streak.match_id) == (6, "m1")
    head = trophy(out, "headshot_machine")  # 12 kills now: over the minimum
    assert (head.player.steamid, head.value) == ("ct1", 50.0) and "6 de 12" in head.detail
    again = insights.trophies_of(list(reversed(es)))  # the order the matches come in changes nothing
    assert again.trophies == out.trophies


def test_a_bot_wins_nothing(night_analysis):
    out = insights.trophies_of(entries(with_bot(night_analysis)))
    assert trophy(out, "clutch_king") is None  # the 1v5 was the bot's
    assert not any(t.player.steamid.startswith("BOT:") for t in out.trophies)
    assert not any(r.player.steamid.startswith("BOT:") for t in out.trophies for r in t.runners_up)


# ------------------------------------------------------------------------------------------- chat

def test_chat_rankings_annotate_old_chat_on_the_fly(night_analysis):
    assert "kind" not in night_analysis.chat[0]
    out = insights.chat_rankings_of(entries(night_analysis))
    rows = {p.player.steamid: p for p in out.players}
    assert set(rows) == {"ct1", "ct2", "t1", "t5"} and out.messages == 6  # no bot, no console, no command
    ct1 = rows["ct1"]
    assert (ct1.messages, ct1.whine, ct1.toxic, ct1.death_cries, ct1.rounds) == (3, 2, 2, 2, 7)
    assert ct1.whine_per_round == round(2 / 7, 3) and ct1.cried_about.steamid == "t1" and ct1.cried_about_count == 2
    assert (rows["ct2"].messages, rows["ct2"].commands, rows["ct2"].shouting) == (1, 1, 1)
    assert rows["t1"].laugh == 1 and rows["t5"].nice == 1

    pods = {p.key: p for p in out.podiums}
    assert list(pods) == ["toxic", "whine", "death_cry", "talker", "laugh", "nice", "shouting"]
    assert pods["whine"].title_pt == "Chorão" and pods["toxic"].title_pt == "Boca suja"
    cry = pods["death_cry"].entries[0]
    assert (cry.player.steamid, cry.count, cry.per_round) == ("ct1", 2, round(2 / 7, 3))
    assert [x.text for x in cry.examples] == ["que sorte", "hack fdp"]  # the longer line first
    assert cry.examples[0].about == "T1" and cry.examples[0].round == 4 and cry.examples[0].match_id == "m1"
    swearing = [x.text for x in pods["toxic"].entries[0].examples]
    assert swearing[0] == "hack fdp" and swearing[1].startswith("seu m") and "mongol" not in swearing[1]  # masked
    assert pods["laugh"].entries[0].player.steamid == "t1" and pods["nice"].entries[0].player.steamid == "t5"
    assert "not moderation" in out.note


def test_chat_already_annotated_is_used_as_stored(night_analysis):
    a = night_analysis.model_copy(deep=True)
    a.chat = [{"tick": 9, "name": "T2", "steamid": "t2", "text": "bom dia", "round": 1, "kind": "whine",
               "tags": ["whine"], "kill_tick": None, "kill_attacker": None, "kill_victim": None, "relation": None}]
    out = insights.chat_rankings_of(entries(a))
    assert [(p.player.steamid, p.whine) for p in out.players] == [("t2", 1)]  # the word list would say "talk"


def test_chat_podium_needs_two_lines_over_several_matches(night_analysis):
    quiet = night_analysis.model_copy(deep=True)
    quiet.chat = []
    out = insights.chat_rankings_of(entries(night_analysis, quiet))
    pods = {p.key: p for p in out.podiums}
    assert [e.player.steamid for e in pods["whine"].entries] == ["ct1"]
    assert pods["laugh"].entries == [] and pods["nice"].entries == []  # one line is not a podium over a night
    assert {p.player.steamid: p.rounds for p in out.players}["ct1"] == 14
    talker = pods["talker"].entries  # the others typed a single line each
    assert [(e.player.steamid, e.count, e.per_round) for e in talker] == [("ct1", 3, round(3 / 14, 3))]


def test_talker_podium_counts_every_line_but_commands(night_analysis):
    out = insights.chat_rankings_of(entries(night_analysis))
    talker = next(p for p in out.podiums if p.key == "talker")
    assert (talker.title_pt, talker.title_en) == ("Tagarela", "Chatterbox")
    # ct1 typed three lines; ct2, t1 and t5 one each in seven rounds: the SteamID decides, and ct2's "!ready"
    # is a command, not a line.
    assert [(e.player.steamid, e.count) for e in talker.entries] == [("ct1", 3), ("ct2", 1), ("t1", 1)]
    assert talker.entries[0].per_round == round(3 / 7, 3) and all(e.examples == [] for e in talker.entries)
    rows = {p.player.steamid: p for p in out.players}
    assert rows["ct1"].messages_per_round == round(3 / 7, 3) and rows["ct2"].messages_per_round == round(1 / 7, 3)


def test_talker_podium_ties_go_to_the_higher_rate_per_round(night_analysis):
    a = night_analysis.model_copy(deep=True)
    said = ("bom dia", "bora", "to indo", "voltei")
    # Two lines each: a spectator who played no round (rate 0), and t3 who played all seven.
    a.chat = [{"tick": 100 + i, "name": sid.upper(), "steamid": sid, "text": said[i]}
              for i, sid in enumerate(("caster", "t3", "caster", "t3"))]
    a.chat += [{"tick": 900 + i, "name": "T2", "steamid": "t2", "text": text} for i, text in enumerate(("!gg", ".r", "oi"))]
    out = insights.chat_rankings_of(entries(a))
    talker = next(p for p in out.podiums if p.key == "talker")
    assert [(e.player.steamid, e.count, e.per_round) for e in talker.entries] == [
        ("t3", 2, round(2 / 7, 3)), ("caster", 2, 0.0), ("t2", 1, round(1 / 7, 3))]
    assert {p.player.steamid: p.messages_per_round for p in out.players}["caster"] == 0.0
    # Plain talk is nobody's crying or swearing: the other podiums stay empty.
    assert all(not p.entries for p in out.podiums if p.key != "talker")


# ------------------------------------------------------------------------------------------- profile

def test_player_profile_adds_up_matches(night_analysis):
    es = entries(night_analysis, night_analysis, with_bot(night_analysis, "ct1"))  # he is not in the third match
    out = insights.profile_of(es, "ct1")
    one = next(p for p in night_analysis.players if p.steamid == "ct1")
    assert (out.matches, out.wins, out.losses, out.draws, out.rounds) == (2, 0, 2, 0, 14)
    assert (out.kills, out.deaths, out.assists, out.kd_diff, out.kd_ratio) == (12, 8, 0, 4, 1.5)
    assert (out.headshot_kills, out.headshot_pct) == (6, 50.0)
    assert out.rating == one.rating and out.kast == one.kast  # two identical matches: the weighted mean is the same
    assert (out.opening_kills, out.opening_deaths) == (6, 6) and out.multi_kills == {"2k": 6, "3k": 0, "4k": 0, "5k": 0}
    assert (out.humiliation_kills, out.humiliation_deaths) == (2, 0)
    # by weapon: the same kills as the scoreboard, headshots per weapon, shares adding up to 100 %
    ws = out.weapon_stats
    assert sum(w.kills for w in ws) == out.kills and sum(w.headshots for w in ws) == out.headshot_kills
    assert [w.kills for w in ws] == sorted((w.kills for w in ws), reverse=True)
    assert sum(w.share_pct for w in ws) == pytest.approx(100.0, abs=0.2)
    assert all(w.headshot_pct == round(100.0 * w.headshots / w.kills, 1) for w in ws)
    assert {w.weapon: w.kills for w in ws if w.weapon in out.weapons} == out.weapons
    assert out.victim.player.steamid == "t1" and out.nemesis is None
    assert out.scope.matches == ["m1", "m2"] and out.player.name == "CT1"
    plays = insights.profile_of(es, "t5").best_plays  # the 1v5 of each match; no ids without a database
    assert len(plays) == 3 and plays[0].highlight_id is None and plays[0].kind in ("ace", "clutch")
    assert [p.score for p in plays] == sorted((p.score for p in plays), reverse=True)
    assert out.chat.death_cries == 4 and out.chat.rounds == 14
    assert [(h.match_id, h.score, h.won, h.night) for h in out.history] == [("m2", "3-4", False, "2021-03-05"),
                                                                           ("m1", "3-4", False, "2021-03-05")]
    whiffer = insights.profile_of(es, "t4")
    assert whiffer.fails == 6 and whiffer.fails_by_kind == {"fail_whiff": 6} and len(whiffer.worst_fails) == 3
    assert (whiffer.wins, whiffer.matches, whiffer.kills) == (3, 3, 0)


def test_unknown_player_is_a_clear_error(night_analysis):
    es = entries(night_analysis, with_bot(night_analysis))
    for who in ("76561190000000000", "BOT:Zed"):
        with pytest.raises(service.NotFound) as err:
            insights.profile_of(es, who)
        assert "2 matches" in err.value.message and "CT1 (ct1)" in err.value.message and "BOT" not in \
            err.value.message.split("Players:")[1]
    assert insights.find_player(es, "ct1") == "ct1" and insights.find_player(es, "Ct5") == "ct5"
    with pytest.raises(service.ServiceError) as err:
        insights.find_player(es, "ct")
    assert "ambiguous" in err.value.message
    with pytest.raises(service.NotFound):
        insights.find_player(es, "zed")  # a bot's name finds nobody


def test_players_of_lists_humans(night_analysis):
    rows = insights.players_of(entries(night_analysis, with_bot(night_analysis)))
    assert len(rows) == 10 and rows[-1].player.steamid == "t5" and rows[-1].matches == 1  # played as a human once
    assert rows[0].matches == 2 and rows[0].rounds == 14


# ------------------------------------------------------------------------------------------- weapons

def test_weapon_rankings_turn_the_profiles_around(night_analysis):
    es = entries(night_analysis, night_analysis, with_bot(night_analysis, "ct1"))  # a bot plays for ct1 in the third
    out = insights.weapon_rankings_of(es)
    assert [(w.weapon, w.kills, w.headshots, w.headshot_pct, w.total_players) for w in out.weapons] == [
        ("AK-47", 73, 6, 8.2, 6), ("Knife", 2, 0, 0.0, 1)]  # most kills first; the bot's six kills are nobody's
    ak, knife = out.weapons
    # most kills first, then headshots, then name; `matches` is what he played, whatever the weapon
    assert [(p.player.steamid, p.matches, p.kills, p.headshots, p.share_pct) for p in ak.players] == [
        ("t5", 3, 15, 0, 100.0), ("ct2", 3, 12, 0, 100.0), ("t1", 3, 12, 0, 100.0), ("t2", 3, 12, 0, 100.0),
        ("t3", 3, 12, 0, 100.0), ("ct1", 2, 10, 6, 83.3)]
    assert [(p.player.name, p.kills, p.headshot_pct, p.share_pct) for p in knife.players] == [("CT1", 2, 0.0, 16.7)]
    assert out.scope.matches == ["m1", "m2", "m3"]
    # every row is the row of that player's profile
    for w in out.weapons:
        for p in w.players:
            mine = next(x for x in insights.profile_of(es, p.player.steamid).weapon_stats if x.weapon == w.weapon)
            assert (mine.kills, mine.headshots, mine.headshot_pct, mine.share_pct) == (
                p.kills, p.headshots, p.headshot_pct, p.share_pct)
    # team kills are not kills: ct3 shot ct4 in round 1 of every match and is in no ranking
    assert not any(p.player.steamid in ("ct3", "BOT:Zed") for w in out.weapons for p in w.players)


def test_one_weapon_by_either_name_a_limit_and_an_error_that_lists_the_weapons(night_analysis):
    es = entries(night_analysis, night_analysis)
    for name in ("AK-47", "ak-47", "ak47", "weapon_ak47"):
        (ak,) = insights.weapon_rankings_of(es, weapon=name, limit=2).weapons
        assert (ak.weapon, ak.total_players) == ("AK-47", 6)
        assert [p.player.steamid for p in ak.players] == ["ct1", "t5"]  # 10 kills each, ct1 has the headshots
    with pytest.raises(service.NotFound) as err:
        insights.weapon_rankings_of(es, weapon="awp")
    assert "'awp'" in str(err.value) and "Weapons with kills: AK-47, Knife." in str(err.value)
    assert insights.weapon_rankings_of([]).weapons == []


# ------------------------------------------------------------------------------------------- database, REST, MCP

def seed(a: MatchAnalysis, played_at: datetime | None) -> str:
    settings = get_settings()
    with session_scope() as s:
        m = Match(sha256=f"insights-{played_at}-{len(a.chat)}-{id(a)}", filename="synthetic.dem", size_bytes=72,
                  demo_path="demos/x.dem", status="ready", map=a.map, tickrate=64, total_ticks=a.total_ticks,
                  rounds=len(a.rounds), team_a_name="A", team_b_name="B", team_a_score=a.team_a.score,
                  team_b_score=a.team_b.score, played_at=played_at.replace(tzinfo=None) if played_at else None)
        s.add(m)
        s.flush()
        m.analysis_path = f"matches/{m.id}/analysis.json"
        settings.path("matches", m.id, "analysis.json").write_text(a.model_dump_json(), encoding="utf-8")
        for c in [*a.highlights, *a.fails]:
            h = Highlight(match_id=m.id, kind=c.kind, title=c.title, description=c.description, origin="auto")
            service.apply_candidate(h, c)
            s.add(h)
        return m.id


@pytest.fixture(scope="module")
def season(night_analysis):
    """Three matches in the database: two on the night of Friday the 5th, one (with a bot) a week later."""
    init_db()
    settings = get_settings()
    before = settings.timezone
    settings.timezone = "America/Sao_Paulo"
    ids = [seed(night_analysis, FRIDAY_LATE), seed(night_analysis, SATURDAY_EARLY),
           seed(with_bot(night_analysis), NEXT_FRIDAY)]
    yield ids
    settings.timezone = before


@pytest.fixture(scope="module")
def client(season):
    from demoreel.api import create_app

    with TestClient(create_app()) as c:
        yield c


MARCH = insights.Scope(since=datetime(2021, 3, 1, tzinfo=UTC), until=datetime(2021, 4, 1, tzinfo=UTC))


def test_scopes_pick_matches_by_night_window_and_id(season):
    m1, m2, m3 = season
    with session_scope() as s:
        assert [e.match_id for e in insights.load_entries(s, MARCH)] == [m1, m2, m3]
        assert [e.match_id for e in insights.load_entries(s, insights.Scope(night=date(2021, 3, 5)))] == [m1, m2]
        assert [e.match_id for e in insights.load_entries(s, insights.Scope(match_id=m3))] == [m3]
        assert [e.match_id for e in insights.load_entries(s, insights.Scope(since=NEXT_FRIDAY, until=MARCH.until))] == [m3]
        assert insights.load_entries(s, insights.Scope(until=datetime(2020, 1, 1))) == []  # an empty window is fine
        assert [(n.date, n.matches) for n in insights.nights(s, MARCH)] == [("2021-03-12", [m3]), ("2021-03-05", [m1, m2])]
        with pytest.raises(service.NotFound) as err:
            insights.load_entries(s, insights.Scope(night=date(2021, 3, 6), since=MARCH.since, until=MARCH.until))
        assert "2021-03-06" in err.value.message and "2021-03-12, 2021-03-05" in err.value.message
        with pytest.raises(service.NotFound):
            insights.load_entries(s, insights.Scope(match_id="m_nope"))
        with pytest.raises(service.ServiceError) as err:
            insights.load_entries(s, insights.Scope(match_id=m3, night=date(2021, 3, 5)))
        assert "outside" in err.value.message


def test_trophies_default_to_the_latest_night(season):
    with session_scope() as s:
        out = insights.trophies(s, insights.Scope(until=MARCH.until))
        assert out.kind == "night" and out.scope.night == "2021-03-12" and out.scope.matches == [season[2]]
        assert insights.trophies(s, insights.Scope(match_id=season[0])).kind == "match"
        with pytest.raises(service.NotFound) as err:
            insights.trophies(s, insights.Scope(until=datetime(2020, 1, 1)))
        assert "match_id" in err.value.message and "night=YYYY-MM-DD" in err.value.message


def test_profile_from_the_database_carries_highlight_ids(season):
    with session_scope() as s:
        out = insights.player_profile(s, "t5", insights.Scope(night=date(2021, 3, 5)))
        assert out.matches == 2 and len(out.best_plays) == 2 and all(p.highlight_id for p in out.best_plays)
        assert service.get_highlight(s, out.best_plays[0].highlight_id).player_steamid == "t5"
        fails = insights.player_profile(s, "t4", MARCH).worst_fails
        assert len(fails) == 3 and all(p.highlight_id and p.kind == "fail_whiff" for p in fails)
        assert insights.resolve_player(s, "ct1", MARCH) == "ct1"


def test_insight_endpoints(client, season):
    m1, m2, m3 = season
    nights = client.get("/api/v1/insights/nights", params=WINDOW).json()
    assert [(n["date"], n["matches"], n["players"], n["rounds"]) for n in nights] == \
           [("2021-03-12", [m3], 9, 7), ("2021-03-05", [m1, m2], 10, 14)]

    riv = client.get("/api/v1/insights/rivalries", params={"night": "2021-03-05"}).json()
    assert riv["min_duels"] == 4 and riv["scope"]["matches"] == [m1, m2] and riv["scope"]["night"] == "2021-03-05"
    assert (riv["pairs"][0]["a"]["steamid"], riv["pairs"][0]["a_kills"], riv["pairs"][0]["b_kills"]) == ("ct1", 6, 2)
    one = client.get("/api/v1/insights/rivalries", params={"match_id": m1, "player": "t5", "min_duels": 1, "limit": 2}).json()
    assert len(one["pairs"]) == 2 and one["total_pairs"] == 5 and [p["player"]["steamid"] for p in one["players"]] == ["t5"]
    assert client.get("/api/v1/insights/rivalries", params={"sort": "luck"}).status_code == 422

    people = client.get("/api/v1/insights/players", params=WINDOW).json()
    assert len(people) == 10 and not any(p["player"]["steamid"].startswith("BOT:") for p in people)
    profile = client.get("/api/v1/insights/players/ct1", params=WINDOW).json()
    assert profile["matches"] == 3 and profile["kills"] == 18 and profile["history"][0]["match_id"] == m3
    clutcher = client.get("/api/v1/insights/players/t5", params={"night": "2021-03-05"}).json()
    assert clutcher["clutches_by_size"] == {"1v5": 2} and clutcher["best_plays"][0]["highlight_id"]
    assert profile["victim"]["player"]["steamid"] == "t1" and profile["chat"]["death_cries"] == 6

    guns = client.get("/api/v1/insights/weapons", params={"night": "2021-03-05"}).json()
    assert [(w["weapon"], w["kills"]) for w in guns["weapons"]] == [("AK-47", 52), ("Knife", 2)]
    assert guns["scope"]["matches"] == [m1, m2] and len(guns["weapons"][0]["players"]) == 6
    one = client.get("/api/v1/insights/weapons", params={"weapon": "knife", "limit": 1, **WINDOW}).json()
    (knife,) = one["weapons"]
    assert knife["players"] == [{"player": {"steamid": "ct1", "name": "CT1"}, "matches": 3, "kills": 3,
                                 "headshots": 0, "headshot_pct": 0.0, "share_pct": 16.7}]

    night = client.get("/api/v1/insights/trophies", params={"night": "2021-03-05"}).json()
    assert night["kind"] == "night" and {t["key"]: t["player"]["steamid"] for t in night["trophies"]}["baiter"] == "ct5"
    match = client.get("/api/v1/insights/trophies", params={"match_id": m3}).json()
    keys = {t["key"]: t for t in match["trophies"]}
    assert match["kind"] == "match" and "clutch_king" not in keys and keys["kill_streak"]["title_pt"] == "Imparável"

    chat = client.get("/api/v1/insights/chat", params={"night": "2021-03-05"}).json()
    cry = next(p for p in chat["podiums"] if p["key"] == "death_cry")["entries"][0]
    assert (cry["player"]["steamid"], cry["count"]) == ("ct1", 4) and cry["examples"][0]["text"] == "que sorte"
    assert chat["messages"] == 12
    assert [p["key"] for p in chat["podiums"]] == ["toxic", "whine", "death_cry", "talker", "laugh", "nice", "shouting"]
    talker = next(p for p in chat["podiums"] if p["key"] == "talker")["entries"]
    assert (talker[0]["player"]["steamid"], talker[0]["count"], talker[0]["examples"]) == ("ct1", 6, [])
    assert next(p for p in chat["players"] if p["player"]["steamid"] == "ct1")["messages_per_round"] == round(6 / 14, 3)


def test_insight_endpoint_errors_say_what_is_valid(client, season):
    r = client.get("/api/v1/insights/trophies", params={"night": "2021-01-01", **WINDOW})
    assert r.status_code == 404 and "2021-03-12, 2021-03-05" in r.json()["detail"]
    r = client.get("/api/v1/insights/players/76561190000000000", params=WINDOW)
    assert r.status_code == 404 and "CT1 (ct1)" in r.json()["detail"]
    r = client.get("/api/v1/insights/weapons", params={"weapon": "bazooka", **WINDOW})
    assert r.status_code == 404 and "Weapons with kills: AK-47, Knife." in r.json()["detail"]
    assert client.get("/api/v1/insights/weapons", params={"limit": 0}).status_code == 422
    assert client.get("/api/v1/insights/rivalries", params={"match_id": "m_nope"}).status_code == 404
    assert client.get("/api/v1/insights/chat", params={"night": "05/03/2021"}).status_code == 422
    assert client.get("/api/v1/insights/rivalries", params={"until": "2020-01-01T00:00:00Z"}).json()["pairs"] == []


def test_mcp_tools_mirror_the_service(season):
    from mcp.server.mcpserver.exceptions import ToolError

    from demoreel.mcp_server import build_server

    server = build_server()

    def call(name: str, **args):
        result = anyio.run(server.call_tool, name, args)
        data = result.structured_content
        return data["result"] if set(data) == {"result"} else data

    names = {t.name for t in anyio.run(server.list_tools)}
    assert {"list_nights", "get_rivalries", "get_player_profile", "get_trophies", "get_chat_rankings",
            "get_weapon_rankings"} <= names
    assert any(n["date"] == "2021-03-05" and n["matches"] == season[:2] for n in call("list_nights"))

    riv = call("get_rivalries", player="CT1", night="2021-03-05")  # by name
    assert riv["pairs"][0]["score"] == "6-2" and riv["players"][0]["victim"] == "T1 (6-2)" and riv["scope"]["matches"] == 2
    profile = call("get_player_profile", player="t5", match_id=season[0])
    assert profile["kills"] == 5 and profile["best_plays"][0]["highlight_id"] and profile["scope"]["match_ids"] == [season[0]]
    trophies = call("get_trophies", match_id=season[0])
    assert trophies["kind"] == "match" and any(t["key"] == "baiter" and t["player"] == "CT5" for t in trophies["trophies"])
    chat = call("get_chat_rankings", night="2021-03-05")
    assert chat["podiums"]["death_cry"]["entries"][0]["examples"][0] == "que sorte (after dying to T1)"
    assert "not moderation" in chat["note"]
    assert chat["podiums"]["talker"]["entries"][0]["player"] == "CT1" and list(chat["podiums"])[0] == "toxic"

    guns = call("get_weapon_rankings", night="2021-03-05", limit=1)
    assert [(w["weapon"], w["kills"], w["total_players"]) for w in guns["weapons"]] == [("AK-47", 52, 6), ("Knife", 2, 1)]
    assert guns["weapons"][0]["players"] == [{"player": "CT1", "steamid": "ct1", "kills": 10, "headshots": 6,
                                              "share_pct": 83.3, "matches": 2}]
    assert call("get_weapon_rankings", weapon="knife", match_id=season[0])["weapons"][0]["players"][0]["kills"] == 1

    for name, args, fragment in (("get_player_profile", {"player": "nobody", "night": "2021-03-05"}, "No player matches"),
                                 ("get_trophies", {"night": "5 de março"}, "YYYY-MM-DD"),
                                 ("get_weapon_rankings", {"weapon": "bazooka", "night": "2021-03-05"}, "Weapons with kills"),
                                 ("get_rivalries", {"night": "2021-01-01", "since": WINDOW["since"]}, "Nights with matches")):
        with pytest.raises(ToolError) as err:  # the SDK's error carries the service message
            call(name, **args)
        assert fragment in str(err.value)
