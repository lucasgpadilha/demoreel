"""2D replay of clips that mix a fixed camera with a player's point of view.

A fixed camera travels through the pipeline as a ``cam:x,y,z,pitch,yaw`` point of view. The 2D replay cannot
film from it: it must not take it for the clip's player, and it must say that it shows the whole map there."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from conftest import real_demos

from demoreel.brand import brand_for
from demoreel.render import Clip, ClipSegment, RenderJob, RenderOptions
from demoreel.render.base import camera_view, is_camera_view, player_view
from demoreel.render.radar import RadarRenderer
from demoreel.render.radar.renderer import _Session, camera_notes

CAM = camera_view({"x": -324.4, "y": -8.2, "z": 506.0, "pitch": 38.2, "yaw": -121.3})


def test_camera_views_are_told_apart_from_players():
    assert is_camera_view(CAM) and not is_camera_view("76561190000000001") and not is_camera_view(None)
    assert player_view(CAM) is None and player_view("76561190000000001") == "76561190000000001"


def test_a_clip_that_opens_on_a_fixed_camera_still_marks_its_players_kills(sample_analysis):
    ace = next(h for h in sample_analysis.highlights if h.kind == "ace")
    first, last = ace.kill_ticks[0], ace.kill_ticks[-1]
    clip = Clip(segments=[ClipSegment(first - 300, first - 20, CAM), ClipSegment(first - 20, last + 40, "ct1")])
    host = SimpleNamespace(analysis=sample_analysis, brand=brand_for(None), tickrate=64.0, fps=30)
    plan = _Session._plan_clip(host, clip)  # planning reads no demo: no session needed
    assert plan.pov == "ct1"
    assert len(plan.kill_frames) == len(ace.kill_ticks) == plan.plate.kills_total
    assert all(f >= len(plan.segments[0].shown) for f in plan.kill_frames)  # all in his segment, none on the camera


def test_the_2d_replay_says_which_segments_it_cannot_film():
    player = ClipSegment(100, 400, "76561190000000001")
    assert camera_notes([Clip(segments=[player])]) == []
    one = camera_notes([Clip(segments=[ClipSegment(0, 100, CAM), player])])
    assert len(one) == 1 and one[0].startswith("Segment 0 is watched from a fixed camera")
    assert "whole map" in one[0] and "ingame" in one[0]
    reel = camera_notes([Clip(segments=[player]), Clip(segments=[ClipSegment(0, 100, CAM), player,
                                                                ClipSegment(400, 500, CAM)])])
    assert len(reel) == 1 and reel[0].startswith("Segments 0, 2 of clip 2 are watched")


@pytest.mark.demo
def test_real_demo_mixed_clip_renders_with_a_warning(tmp_path):
    demos = real_demos()
    if not demos:
        pytest.skip("no demo in demos/")
    from demoreel.analysis import analyze_demo

    analysis = analyze_demo(demos[0])
    h = next(x for x in analysis.highlights if x.kill_ticks)
    kill = h.kill_ticks[0]
    clip = Clip(segments=[ClipSegment(kill - 64, kill - 16, CAM), ClipSegment(kill - 16, kill + 32, h.player.steamid)])
    job = RenderJob(demo_path=demos[0], analysis=analysis, clips=[clip], out_path=tmp_path / "out.mp4",
                    work_dir=tmp_path, options=RenderOptions(width=320, height=180, fps=15))
    res = RadarRenderer().render(job)
    assert job.out_path.stat().st_size > 2000
    assert res.details["warnings"] and "Segment 0" in res.details["warnings"][0]
