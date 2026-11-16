"""Actions file generation of the in-game renderer (pure logic, no game involved)."""

from __future__ import annotations

import json

import pytest

from demoreel.render.base import Clip, ClipSegment
from demoreel.render.ingame import actions
from demoreel.render.ingame.actions import Action, ActionOptions, CameraCut, RecordSequence

STEAM_A = "76561198000000001"
STEAM_B = "76561198000000002"
SLOTS = {STEAM_A: 6, STEAM_B: 11}


def cmds(group: list[Action]) -> list[tuple[int, str]]:
    return [(a.tick, a.cmd) for a in group]


def test_single_sequence_matches_the_csdm_command_order_and_tick_offsets():
    seq = RecordSequence(number=1, start_tick=10000, end_tick=10640, cameras=(CameraCut(10000, 6),))
    group = actions.sequence_actions(seq, ActionOptions(fps=30, tickrate=64), is_last=True)
    assert cmds(group) == [
        (96, "sv_cheats 1"),
        (96, "volume 1"),
        (96, "cl_hud_telemetry_frametime_show 0"),
        (96, "cl_hud_telemetry_net_misdelivery_show 0"),
        (96, "cl_hud_telemetry_ping_show 0"),
        (96, "cl_hud_telemetry_serverrecvmargin_graph_show 0"),
        (96, "cl_trueview_show_status 0"),
        (96, "r_show_build_info 0"),
        (96, "engine_no_focus_sleep 0"),  # demoreel addition: full speed with the window in the background
        (96, "cl_demo_predict 0"),
        (96, "cl_draw_only_deathnotices 1"),
        (96, "tv_listen_voice_indices 0"),
        (96, "tv_listen_voice_indices_h 0"),
        (9936, "spec_show_xray 0"),  # setup tick = start - round(tickrate)
        (9936, "mp_display_kill_assists 1"),
        (9936, "host_framerate 30"),
        (9996, "pause_playback"),  # start - 4
        (96, "demo_gototick 9935"),  # one tick before the setup tick
        (9992, "spec_mode 1"),  # demoreel addition: camera aimed 8 ticks early, before the pause
        (9992, "spec_player 6"),
        (10000, "spec_mode 1"),  # ...and again on the start tick, as the reference does
        (10000, "spec_player 6"),
        (10000, "startmovie 1-sequence"),
        (10640, "endmovie"),
        (10704, "quit"),  # end + 64
    ]


def test_spec_player_never_shares_a_tick_with_the_seek_and_follows_spec_mode():
    seq = RecordSequence(number=1, start_tick=5000, end_tick=5500, cameras=(CameraCut(5000, 3),))
    group = actions.sequence_actions(seq, ActionOptions(), is_last=True)
    seek = next(a for a in group if a.cmd.startswith("demo_gototick"))
    spec_mode_index = next(i for i, a in enumerate(group) if a.cmd == "spec_mode 1")
    spec_player = group[spec_mode_index + 1]
    assert spec_player.cmd == "spec_player 3"
    assert spec_player.tick > seek.tick
    assert spec_player.tick > int(seek.cmd.split()[1])  # executed after the playback landed on the target
    assert all(a.tick >= actions.MIN_ACTION_TICK for a in group)


def test_options_change_only_the_expected_commands():
    seq = RecordSequence(number=2, start_tick=4000, end_tick=4200)
    opts = ActionOptions(fps=60, tickrate=64, only_death_notices=False, show_xray=True, show_assists=False,
                         player_voices=True, true_view=True, extra_commands=("cl_drawhud 0", "  "))
    got = cmds(actions.sequence_actions(seq, opts, is_last=False))
    assert (96, "cl_demo_predict 1") in got
    assert (96, "cl_draw_only_deathnotices 0") in got
    assert (96, "tv_listen_voice_indices -1") in got and (96, "tv_listen_voice_indices_h -1") in got
    assert (3936, "spec_show_xray 1") in got
    assert (3936, "mp_display_kill_assists 0") in got
    assert (3936, "host_framerate 60") in got
    assert (3936, "cl_drawhud 0") in got
    assert got.index((3936, "cl_drawhud 0")) < got.index((3996, "pause_playback"))
    assert (4000, "startmovie 2-sequence") in got
    assert got[-1] == (4264, "go_to_next_sequence")
    assert not any(c.startswith(("spec_player", "spec_mode", "mirv_")) for _, c in got)  # no POV, no HLAE


def test_multi_segment_clips_are_numbered_in_video_order_and_recorded_by_start_tick():
    clips = [
        Clip(segments=[ClipSegment(20000, 20500, STEAM_A)], title="late round first in the reel"),
        Clip(segments=[ClipSegment(8000, 8400, STEAM_B), ClipSegment(9000, 9300, STEAM_B)]),
    ]
    plan = actions.plan_sequences(clips, SLOTS, max_tick=100000)
    assert [(s.number, s.start_tick, s.end_tick) for s in plan.sequences] == [
        (1, 20000, 20500), (2, 8000, 8400), (3, 9000, 9300)]
    assert [s.cameras for s in plan.sequences] == [
        (CameraCut(20000, 6),), (CameraCut(8000, 11),), (CameraCut(9000, 11),)]
    assert plan.warnings == []

    groups = actions.build_actions(plan.sequences, ActionOptions(max_tick=100000))
    starts = [next(a.cmd for a in g if a.cmd.startswith("startmovie")) for g in groups]
    assert starts == ["startmovie 2-sequence", "startmovie 3-sequence", "startmovie 1-sequence"]
    assert [g[-1].cmd for g in groups] == ["go_to_next_sequence", "go_to_next_sequence", "quit"]
    assert [g[-1].tick for g in groups] == [8464, 9364, 20564]
    # every sequence seeks by itself: the plugin restarts the demo from tick 0 between sequences
    assert all(sum(a.cmd.startswith("demo_gototick") for a in g) == 1 for g in groups)
    assert all(sum(a.cmd == "pause_playback" for a in g) == 1 for g in groups)


def test_contiguous_segments_of_a_clip_become_one_sequence_with_a_camera_cut():
    clip = Clip(segments=[ClipSegment(5000, 5300, STEAM_A), ClipSegment(5300, 5600, STEAM_B),
                          ClipSegment(5601, 5800, STEAM_B)])
    other = Clip(segments=[ClipSegment(5800, 6000, STEAM_A)])  # contiguous but another clip: not merged
    plan = actions.plan_sequences([clip, other], SLOTS)
    assert [(s.number, s.start_tick, s.end_tick) for s in plan.sequences] == [(1, 5000, 5800), (2, 5800, 6000)]
    assert plan.sequences[0].cameras == (CameraCut(5000, 6), CameraCut(5300, 11))

    group = actions.sequence_actions(plan.sequences[0], ActionOptions(), is_last=False)
    got = cmds(group)
    assert got.index((5000, "spec_player 6")) < got.index((5300, "spec_mode 1")) < got.index(
        (5300, "spec_player 11")) < got.index((5000, "startmovie 1-sequence"))
    assert sum(c.startswith("startmovie") for _, c in got) == 1

    unmerged = actions.plan_sequences([clip], SLOTS, merge_contiguous=False)
    assert len(unmerged.sequences) == 3


def test_a_take_from_the_start_of_the_demo_begins_late_enough_to_aim_the_camera():
    # Nothing runs before tick 96. A take that started there had the seek and spec_player on the same tick, and
    # the game ignores spec_player on the tick of a seek: the round was filmed from some other player.
    for first in (0, 10, 96, 100, 159):
        plan = actions.plan_sequences([Clip(segments=[ClipSegment(first, 400, STEAM_A)])], SLOTS)
        (seq,) = plan.sequences
        assert (seq.start_tick, seq.end_tick) == (actions.FIRST_RECORD_TICK, 400) == (160, 400)
        got = cmds(actions.sequence_actions(seq, ActionOptions(), is_last=True))
        assert (96, "demo_gototick 96") in got
        assert (96, "host_framerate 30") in got
        assert (160, "startmovie 1-sequence") in got
        assert min(t for t, _ in got) == 96
        seek = [t for t, c in got if c.startswith("demo_gototick")]
        aim = [t for t, c in got if c.startswith("spec_player")]
        assert aim == [152, 160] and min(aim) > max(seek)
    # a little later nothing changes
    (seq,) = actions.plan_sequences([Clip(segments=[ClipSegment(200, 400, STEAM_A)])], SLOTS).sequences
    assert seq.start_tick == 200


def test_the_seek_and_the_camera_are_never_on_the_same_tick():
    for start in (*range(0, 400, 7), 5000):
        (seq,) = actions.plan_sequences([Clip(segments=[ClipSegment(start, start + 600, STEAM_A)])], SLOTS).sequences
        got = cmds(actions.sequence_actions(seq, ActionOptions(), is_last=True))
        seek = {t for t, c in got if c.startswith("demo_gototick")}
        assert not seek & {t for t, c in got if c.startswith("spec_player")}, start


def test_a_take_that_plays_past_the_end_of_the_match_is_the_last_of_its_game_launch():
    # The end-of-match screen stays up for the rest of the game session: the take recorded after one that reached
    # the end of the match showed the winners' line-up from its first frame instead of the last kill.
    def take(n, a, b):
        return actions.RecordSequence(n, a, b)

    early, mid = take(1, 5000, 5400), take(2, 70000, 70400)
    victim, killer = take(4, 77514, 77898), take(3, 77610, 77990)  # the last kill, at tick 77802, ends the match
    batches = actions.recording_batches([killer, early, victim, mid], 77802)
    assert batches == [[early, mid, victim], [killer]]
    # every launch closes the game itself
    for batch in batches:
        groups = actions.build_actions(batch, ActionOptions(max_tick=77998))
        assert [g[-1].cmd for g in groups] == ["go_to_next_sequence"] * (len(batch) - 1) + ["quit"]
    # the take's last action runs 64 ticks after it stops recording: that is what must stay before the end
    assert actions.recording_batches([take(1, 77000, 77737), take(2, 77100, 77700)], 77802) == [
        [take(1, 77000, 77737), take(2, 77100, 77700)]]
    assert len(actions.recording_batches([take(1, 77000, 77738), take(2, 77100, 77700)], 77802)) == 2
    # nothing reaches the end, or the end is unknown: one launch, in recording order
    assert actions.recording_batches([mid, early], 77802) == [[early, mid]]
    assert actions.recording_batches([killer, victim], None) == [[victim, killer]]
    assert actions.recording_batches([], 77802) == []


def test_segments_are_clamped_to_the_end_of_the_demo_so_quit_always_runs():
    plan = actions.plan_sequences([Clip(segments=[ClipSegment(9000, 10050, STEAM_A)])], SLOTS, max_tick=10000)
    (seq,) = plan.sequences
    assert seq.end_tick == 10000 - actions.END_OF_DEMO_MARGIN_TICKS
    group = actions.sequence_actions(seq, ActionOptions(max_tick=10000), is_last=True)
    end = next(a for a in group if a.cmd == "endmovie")
    quit_ = group[-1]
    assert quit_.cmd == "quit"
    assert end.tick < quit_.tick <= 10000 - 2


def test_unusable_segments_and_unknown_players_produce_warnings_not_errors():
    clips = [Clip(segments=[
        ClipSegment(500, 500, STEAM_A),  # empty
        ClipSegment(20, 60, STEAM_A),  # entirely before the first tick a take can start at
        ClipSegment(99990, 100200, STEAM_A),  # past the end of the demo
        ClipSegment(3000, 3500, "BOT:Hank"),  # no slot
        ClipSegment(4000, 4500, None),  # free camera
        ClipSegment(6000, 6500, STEAM_A, speed=0.5),
    ])]
    plan = actions.plan_sequences(clips, SLOTS, max_tick=100000)
    assert [(s.start_tick, s.end_tick, s.cameras) for s in plan.sequences] == [
        (3000, 3500, ()), (4000, 4500, ()), (6000, 6500, (CameraCut(6000, 6),))]
    text = " ".join(plan.warnings)
    assert text.count("skipped") == 3
    assert "BOT:Hank" in text
    assert "speed is ignored" in text


def test_slot_lookup_can_be_a_callable_receiving_the_segment_tick():
    seen: list[tuple[str, int]] = []

    def lookup(steamid: str, tick: int) -> int | None:
        seen.append((steamid, tick))
        return 9

    plan = actions.plan_sequences([Clip(segments=[ClipSegment(700, 900, STEAM_A)])], lookup)
    assert plan.sequences[0].cameras == (CameraCut(700, 9),)
    assert seen == [(STEAM_A, 700)]


def test_build_segment_actions_and_json_shape():
    segments = [ClipSegment(3000, 3200, STEAM_A), ClipSegment(1000, 1200, STEAM_B)]
    groups = actions.build_segment_actions(segments, SLOTS, ActionOptions(fps=30, tickrate=64))
    assert len(groups) == 2
    data = json.loads(actions.to_json(groups))
    assert isinstance(data, list) and set(data[0]) == {"actions"}
    assert all(set(a) == {"tick", "cmd"} for seq in data for a in seq["actions"])
    assert all(isinstance(a["tick"], int) and isinstance(a["cmd"], str) for seq in data for a in seq["actions"])
    flat = [a["cmd"] for seq in data for a in seq["actions"]]
    assert flat.count("quit") == 1 and flat[-1] == "quit"
    assert "startmovie 2-sequence" in [a["cmd"] for a in data[0]["actions"]]  # tick 1000 is recorded first


def test_expected_frames_and_names():
    seqs = [RecordSequence(1, 1000, 1640), RecordSequence(2, 5000, 5032)]
    assert seqs[0].name == "1-sequence"
    assert actions.expected_frames(seqs, fps=30, tickrate=64) == 300 + 15


def test_recording_systems():
    assert actions.recording_commands("startmovie", "7-sequence", 60) == (
        "host_framerate 60", "startmovie 7-sequence", "endmovie")
    assert actions.recording_commands("hlae", "7-sequence", 60) == (
        "mirv_streams record fps 60", "mirv_streams record start", "mirv_streams record end")
    with pytest.raises(ValueError, match="obs"):
        actions.recording_commands("obs", "7-sequence", 60)


def test_hlae_sequence_pipes_the_take_into_ffmpeg_and_never_uses_startmovie():
    seq = RecordSequence(number=2, start_tick=4000, end_tick=4200)
    opts = ActionOptions(recording_system="hlae", fps=30, hlae_output_dir=r"C:\data dir\tmp\hlae",
                         hlae_ffmpeg_args="-c:v libx264 -crf 17 -pix_fmt yuv420p")
    acts = actions.sequence_actions(seq, opts, is_last=True)
    cmds = [a.cmd for a in acts]
    assert "mirv_streams record screen enabled 1" in cmds and not any(c.startswith(("startmovie", "host_framerate")) for c in cmds)
    setup = [a.cmd for a in acts if a.tick == 4000 - 64]
    assert setup[:4] == [
        "mirv_streams record startMovieWav 1",
        'mirv_streams record name "C:/data dir/tmp/hlae/2-sequence"',
        'mirv_streams settings add ffmpeg demoreel2 "-c:v libx264 -crf 17 -pix_fmt yuv420p '
        r'{QUOTE}C:/data dir/tmp/hlae/2-sequence\\video.mp4{QUOTE}"',
        "mirv_streams record screen settings demoreel2",
    ]
    assert "mirv_streams record fps 30" in setup
    assert [(a.tick, a.cmd) for a in acts if a.cmd.startswith("mirv_streams record st") and "Wav" not in a.cmd] == [
        (4000, "mirv_streams record start")]
    assert (4200, "mirv_streams record end") in [(a.tick, a.cmd) for a in acts]
    with pytest.raises(ValueError, match="output folder"):
        actions.sequence_actions(seq, ActionOptions(recording_system="hlae"), is_last=True)


def test_frame_format_changes_only_the_startmovie_command():
    seq = RecordSequence(number=1, start_tick=4000, end_tick=4200)
    tga = cmds(actions.sequence_actions(seq, ActionOptions(frame_format="tga"), is_last=True))
    jpg = cmds(actions.sequence_actions(seq, ActionOptions(frame_format="jpg"), is_last=True))
    assert (4000, "startmovie 1-sequence") in tga
    assert (4000, "startmovie 1-sequence jpg wav") in jpg  # audio must be requested once a format is named
    assert [c for c in tga if not c[1].startswith("startmovie")] == [c for c in jpg if not c[1].startswith("startmovie")]
    with pytest.raises(ValueError):
        actions.sequence_actions(seq, ActionOptions(frame_format="png"), is_last=True)


# --- x-ray around kills through smoke / walls -------------------------------------------------------------

def _kill(tick, steamid, *, smoke=False, wall=False):
    from types import SimpleNamespace

    return SimpleNamespace(tick=tick, attacker=SimpleNamespace(steamid=steamid), through_smoke=smoke, wallbang=wall)


def test_xray_mode_defaults_to_auto():
    from demoreel.render.ingame import xray

    assert xray.mode({}) == "auto" and xray.mode(None) == "auto" and xray.mode({"ingame_xray": "auto"}) == "auto"
    assert xray.mode({"ingame_xray": True}) == "on" and xray.mode({"ingame_xray": "yes"}) == "on"
    assert xray.mode({"ingame_xray": False}) == "off" and xray.mode({"ingame_xray": "never"}) == "off"


def test_xray_windows_only_for_smoke_and_wallbang_kills_of_that_player():
    from demoreel.render.ingame import xray

    kills = [_kill(1000, "A"), _kill(2000, "A", smoke=True), _kill(2100, "A", wall=True), _kill(5000, "B", wall=True),
             _kill(9000, None and "x", smoke=True)]
    kills[-1].attacker = None  # world kill
    by_pov = xray.windows(kills, 64)
    assert by_pov == {"A": [(2000 - 192, 2100 + 64)], "B": [(5000 - 192, 5000 + 64)]}  # overlapping ranges merge
    seg = ClipSegment(1900, 2050, "A", 1.0, None)
    assert xray.within(by_pov, "A", 1900, 2050) == [(1900, 2050)]
    assert xray.needed(kills, [seg], 64) and not xray.needed(kills, [ClipSegment(900, 1200, "A", 1.0, None)], 64)
    assert not xray.needed(kills, [ClipSegment(1900, 2050, "B", 1.0, None)], 64)  # somebody else's kill
    assert not xray.needed(None, [seg], 64)


def test_sequence_switches_xray_on_and_off_inside_the_take():
    from demoreel.render.ingame import actions as A

    clips = [Clip(segments=[ClipSegment(1000, 3000, "A", 1.0, None)])]
    plan = A.plan_sequences(clips, {"A": 3})
    seqs = A.with_xray(plan.sequences, clips, {"A": [(1808, 2064)], "B": [(1200, 1400)]})
    assert seqs[0].xray == ((1808, 2064),)
    acts = A.sequence_actions(seqs[0], A.ActionOptions(), is_last=True)
    xr = [(a.tick, a.cmd) for a in acts if a.cmd.startswith("spec_show_xray")]
    assert xr == [(1000 - 64, "spec_show_xray 0"), (1808, "spec_show_xray 1"), (2064, "spec_show_xray 0")]


def test_xray_range_open_at_the_start_is_switched_on_on_the_start_tick_and_left_on_at_the_end():
    from demoreel.render.ingame import actions as A

    clips = [Clip(segments=[ClipSegment(1000, 1100, "A", 1.0, None)])]
    seqs = A.with_xray(A.plan_sequences(clips, {"A": 3}).sequences, clips, {"A": [(900, 1300)]})
    acts = A.sequence_actions(seqs[0], A.ActionOptions(), is_last=True)
    xr = [(a.tick, a.cmd) for a in acts if a.cmd.startswith("spec_show_xray")]
    assert xr == [(1000 - 64, "spec_show_xray 0"), (1000, "spec_show_xray 1")]
    on = next(i for i, a in enumerate(acts) if a.cmd == "spec_show_xray 1")
    start = next(i for i, a in enumerate(acts) if a.tick == 1000 and "startmovie" in a.cmd)
    assert on < start  # same tick: outlines first, then the recording starts


def test_xray_always_on_ignores_the_ranges():
    from demoreel.render.ingame import actions as A

    clips = [Clip(segments=[ClipSegment(1000, 3000, "A", 1.0, None)])]
    seqs = A.with_xray(A.plan_sequences(clips, {"A": 3}).sequences, clips, {"A": [(1808, 2064)]})
    acts = A.sequence_actions(seqs[0], A.ActionOptions(show_xray=True), is_last=True)
    assert [a.cmd for a in acts if a.cmd.startswith("spec_show_xray")] == ["spec_show_xray 1"]
