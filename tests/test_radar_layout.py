"""2D replay: layout selection per aspect ratio, the frame/tick timeline with hit-stops, the follow camera."""

from __future__ import annotations

import numpy as np
import pytest

from demoreel.render.radar import camera as cam
from demoreel.render.radar.layout import ROW_PITCH, choose_layout, layout_kind
from demoreel.render.radar.timeline import (
    CATCH_UP_S,
    HIT_STOP_S,
    first_frame_at,
    frame_ticks,
    segment_frames,
)


# ------------------------------------------------------------------ layout
@pytest.mark.parametrize("w, h, kind, scale", [
    (1280, 720, "wide", 1.0), (1920, 1080, "wide", 1.5), (1080, 1920, "tall", 2.0), (720, 1280, "tall", 720 / 540),
    (1080, 1080, "square", 1.5), (720, 720, "square", 1.0), (960, 720, "wide", 1.0), (1080, 1350, "square", 1.5),
    (2560, 1080, "wide", 1.5),
])
def test_layout_is_chosen_from_the_frame(w, h, kind, scale):
    assert layout_kind(w, h) == kind
    L = choose_layout(w, h)
    assert L.kind == kind and L.s == pytest.approx(scale)
    for r in (L.focus, L.focus_full):
        assert r.w > 0 and r.h > 0 and r.x >= 0 and r.y >= 0 and r.right <= w and r.bottom <= h
    assert L.focus_full.h >= L.focus.h
    assert L.roster_left_x == 0 and L.roster_right_x + L.roster_w == pytest.approx(w)  # they hang on the edges


def test_tall_keeps_text_out_of_the_social_apps_ui():
    L = choose_layout(1080, 1920)
    top, bottom = 220, 1920 - 340
    assert L.scorebug_y >= top and L.feed_y >= top and L.logo_y >= top
    assert L.roster_bottom is not None and L.roster_bottom <= bottom
    assert L.roster_top(5) == pytest.approx(L.roster_bottom - 5 * ROW_PITCH * L.s + 2 * L.s)
    assert L.plate_bottom <= L.roster_top(5) and L.counter_y < L.roster_top(5)
    assert L.focus.y > L.scorebug_y and L.focus.bottom < L.plate_bottom  # the action stays clear of both
    assert 2 * L.roster_w <= 1080  # two columns side by side


def test_wide_puts_the_action_between_the_rosters():
    L = choose_layout(1280, 720)
    assert L.focus.x >= L.roster_w and L.focus.right <= L.roster_right_x
    assert L.roster_bottom is None
    top = L.roster_top(5)
    assert top > L.scorebug_y + 76 * L.s and top + 5 * ROW_PITCH * L.s < L.plate_bottom - 90 * L.s
    assert L.feed_x > L.scorebug_cx + 300 * L.s  # the feed sits beside the scorebug, not under it
    assert choose_layout(960, 720).feed_y > 76  # ... unless the frame is too narrow for both


def test_square_stacks_rosters_at_the_bottom():
    L = choose_layout(1080, 1080)
    assert L.roster_bottom is not None and L.roster_bottom <= 1080
    assert L.focus.bottom < L.roster_top(5)
    assert L.feed_y > L.scorebug_y + 40 * L.s


# ------------------------------------------------------------------ timeline
def test_frame_count_and_linear_clock():
    assert segment_frames(0, 640, 1.0, 64, 30) == 300
    assert segment_frames(0, 640, 0.5, 64, 30) == 600
    assert segment_frames(0, 641, 1.0, 64, 30) == 301  # rounds up: the last tick is shown
    assert segment_frames(100, 100, 1.0, 64, 30) == 1 and segment_frames(100, 90, 1.0, 64, 30) == 1
    shown, linear = frame_ticks(1000, 1640, 1.0, 64, 30)
    assert len(shown) == 300 and np.array_equal(shown, linear)
    assert shown[0] == 1000 and shown[-1] <= 1640 and np.all(np.diff(shown) > 0)
    assert np.diff(linear)[0] == pytest.approx(64 / 30)


def test_hit_stop_holds_the_kill_then_pays_the_time_back():
    fps, tr, kill = 30, 64, 1320
    shown, linear = frame_ticks(1000, 1640, 1.0, tr, fps, [kill])
    assert len(shown) == len(linear) == 300  # the clip keeps its length
    assert np.all(np.diff(shown) >= 0) and shown[0] == 1000
    f0 = first_frame_at(shown, kill)
    held = shown[f0:f0 + int(HIT_STOP_S * fps)]
    assert held.max() - held.min() < 2.5  # the map stands still on the impact...
    assert np.all(linear[f0:f0 + 3] > shown[f0:f0 + 3] - 1e-9)  # ...while the HUD clock keeps running
    back = f0 + int((HIT_STOP_S + CATCH_UP_S) * fps) + 2
    assert np.allclose(shown[back:], linear[back:], atol=1e-6)  # and is back on the linear clock
    assert np.allclose(shown[:f0 - 1], linear[:f0 - 1])  # nothing moves before the kill
    assert np.max(linear - shown) <= HIT_STOP_S * tr + 1e-6


def test_hit_stop_edge_cases():
    plain, _ = frame_ticks(1000, 1640, 1.0, 64, 30)
    for impacts in ([900], [1000], [1640], [5000], []):  # outside or on the borders: ignored
        assert np.array_equal(frame_ticks(1000, 1640, 1.0, 64, 30, impacts)[0], plain)
    slow, slow_linear = frame_ticks(1000, 1128, 0.4, 64, 30, [1064])
    assert np.array_equal(slow, slow_linear)  # slow motion is already slow
    burst, linear = frame_ticks(1000, 1640, 1.0, 64, 30, [1300, 1310, 1320])  # three kills in a quarter second
    assert np.all(np.diff(burst) >= 0) and burst[-1] <= 1640 and np.allclose(burst[-30:], linear[-30:], atol=1e-6)
    late, _ = frame_ticks(1000, 1640, 1.0, 64, 30, [1636])  # a kill on the last frames never runs past the end
    assert late[-1] <= 1640 and np.all(np.diff(late) >= 0)
    one, _ = frame_ticks(50, 50, 1.0, 64, 30, [50])
    assert len(one) == 1 and one[0] == 50


def test_first_frame_at():
    ticks = np.array([10.0, 12.0, 14.0, 16.0])
    assert [first_frame_at(ticks, t) for t in (0, 10, 11, 16, 99)] == [0, 0, 1, 3, 3]


# ------------------------------------------------------------------ camera
def on_screen(view, point, focus_w, focus_h):
    """Offset of a radar point from the focus centre, in screen px."""
    return (point[0] - view[0]) * view[2], (point[1] - view[1]) * view[2]


def test_fit_and_full_view():
    assert cam.fit_scale(800, 600, fill=1.0) == pytest.approx(600 / 1024)
    v = cam.full_view(800, 600)
    assert v[0] == v[1] == 512 and v[2] * 1024 <= 600


def test_follow_keeps_the_player_centred_when_alone():
    n = 90
    pov = np.column_stack([np.linspace(300, 420, n), np.full(n, 500.0)])
    views = cam.follow_path(pov, [], 800, 600, k_max=2.0, margin=60, fps=30)
    assert views.shape == (n, 3)
    assert np.allclose(views[:, 2], 2.0)  # nothing else to show: as close as allowed
    for i in (10, 45, 80):
        dx, dy = on_screen(views[i], pov[i], 800, 600)
        assert abs(dx) < 40 and abs(dy) < 1
    assert np.all(np.abs(np.diff(views[:, 0])) < 3)  # eased, no jumps


def test_follow_widens_for_the_fight_and_comes_back():
    n = 240
    pov = np.tile([400.0, 500.0], (n, 1))
    enemy = np.full((n, 2), np.nan)
    enemy[90:150] = [800.0, 500.0]  # he fights someone 400 radar px away for two seconds
    views = cam.follow_path(pov, [enemy], 800, 600, k_max=2.0, margin=60, fps=30)
    mid = views[120]
    assert mid[2] < 1.7
    for p in (pov[120], enemy[120]):
        dx, dy = on_screen(mid, p, 800, 600)
        assert abs(dx) <= 400 and abs(dy) <= 300  # both inside the focus rectangle
    assert views[60][2] < 2.0  # it starts widening before the enemy matters
    assert views[0][2] == pytest.approx(2.0, abs=0.02) and views[-1][2] == pytest.approx(2.0, abs=0.02)
    assert np.all(views[:, 2] >= cam.fit_scale(800, 600) - 1e-9)


def test_follow_survives_missing_data():
    n = 30
    nothing = np.full((n, 2), np.nan)
    assert np.allclose(cam.follow_path(nothing, [], 800, 600, k_max=2, margin=50, fps=30), cam.full_view(800, 600))
    pov = nothing.copy()
    pov[10:20] = [300.0, 300.0]  # known only in the middle (dead before and after): the camera holds there
    views = cam.follow_path(pov, [nothing], 800, 600, k_max=2, margin=50, fps=30)
    assert np.isfinite(views).all() and np.allclose(views[:, :2], 300.0)
    assert cam.follow_path(np.zeros((0, 2)), [], 800, 600, k_max=2, margin=50, fps=30).shape == (1, 3)


def test_frame_box_blend_smooth_majority():
    v = cam.frame_box(np.array([100.0, 100.0]), np.array([300.0, 200.0]), 800, 600, 50, 0.5, 4.0)
    assert v[0] == 200 and v[1] == 150 and v[2] == pytest.approx(min(800 / 300, 600 / 200))
    assert cam.frame_box(np.array([0.0, 0.0]), np.array([0.0, 0.0]), 800, 600, 0, 0.5, 4.0)[2] == 4.0  # clamped
    a, b = np.array([0.0, 0.0, 1.0]), np.array([100.0, 50.0, 4.0])
    assert np.allclose(cam.blend_views(a, b, 0), a) and np.allclose(cam.blend_views(a, b, 1), b)
    assert cam.blend_views(a, b, 0.5)[2] == pytest.approx(2.0)  # zoom moves geometrically
    assert cam.ease_in_out(-1) == 0 and cam.ease_in_out(2) == 1 and cam.ease_in_out(0.5) == pytest.approx(0.5)
    flat = np.full((50, 2), 7.0)
    assert np.allclose(cam.smooth(flat, 6.0), 7.0)  # a constant stays constant, also at the ends
    step = np.concatenate([np.zeros(40), np.ones(40)])
    s = cam.smooth(step, 5.0)
    assert s[0] == pytest.approx(0, abs=1e-3) and s[-1] == pytest.approx(1, abs=1e-3) and np.all(np.diff(s) >= -1e-12)
    flicker = np.array([0] * 20 + [1, 1] + [0] * 20 + [1] * 30, dtype=bool)
    clean = cam.majority(flicker, 5)
    assert not clean[:40].any() and clean[-20:].all()  # two frames on a staircase do not switch the floor
