"""Composition stage: brand kits, logo splitting, sound synthesis, bumpers, assembly, masters, retiming."""

from __future__ import annotations

import math
import re
import subprocess
import wave

import numpy as np
import pytest
import skia
from conftest import CT, ROUND_TICKS, T, ace_round, analyze, kill, make_demo
from PIL import Image, ImageDraw

from demoreel import service
from demoreel.analysis import chat as chat_mod
from demoreel.brand import Brand, BrandColors, BumperSpec, active_brand, load_brand, save_brand
from demoreel.compose import master as masters
from demoreel.compose import overlay, sfx
from demoreel.compose.assemble import Part, assemble, probe
from demoreel.compose.hud import ChatLine, ClipInfo, Hud, HudState
from demoreel.compose.logo import load_logo
from demoreel.compose.overlay import (
    Span,
    _audio_filter,
    _plan,
    clip_info,
    compose_body,
    kill_marks,
    quiet_stretches,
    time_of_tick,
    timeline,
)
from demoreel.compose.pipeline import finish, is_branded
from demoreel.config import get_settings
from demoreel.render import Clip, ClipSegment, RenderError, ffmpeg


def lavfi_clip(path, seconds=0.6, color="red", size="64x64", fps=10, audio=True):
    """A tiny real video made by ffmpeg itself (no fixtures on disk)."""
    args = [ffmpeg.ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"color=c={color}:s={size}:r={fps}:d={seconds}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:a", "aac", "-shortest"]
    args += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(args, check=True)
    return path


@pytest.fixture
def logo_file(tmp_path):
    """Flat two-colour mark on black: two cream shapes and one orange accent."""
    img = Image.new("RGB", (256, 256), (11, 10, 9))
    d = ImageDraw.Draw(img)
    d.rectangle([30, 60, 100, 200], fill=(235, 231, 222))
    d.rectangle([150, 60, 220, 200], fill=(235, 231, 222))
    d.polygon([(110, 200), (140, 200), (125, 150)], fill=(242, 124, 20))
    p = tmp_path / "logo.png"
    img.save(p)
    return p


@pytest.fixture
def brand(logo_file):
    b = Brand(slug="testbrand", name="Test Mix", tagline="toda sexta", logo="logo.png", logo_background="#0b0a09",
              colors=BrandColors(accent="#e08a3c"), locale="pt-BR",
              bumper=BumperSpec(intro_seconds=0.8, outro_seconds=0.8, cta="test.example"))
    save_brand(b)
    (b.dir / "logo.png").write_bytes(logo_file.read_bytes())
    yield load_brand("testbrand")
    import shutil

    shutil.rmtree(b.dir, ignore_errors=True)


# ------------------------------------------------------------------------------------------- brand kits

def test_default_identity_when_no_kit_is_configured():
    b = active_brand()
    assert b.slug == "demoreel" and not is_branded(b) and b.logo_path is None
    out = service.brand_out()
    assert out.logo_url is None and out.colors["accent"].startswith("#") and out.fonts["display"]


def test_kit_round_trip_and_logo_url(brand):
    assert brand.name == "Test Mix" and brand.logo_path is not None and is_branded(brand)
    assert active_brand().slug == "testbrand"  # the only kit present becomes the instance's identity
    assert service.brand_out().logo_url.endswith("/files/brands/testbrand/logo.png")
    assert load_brand("../../etc") is None and load_brand("nope") is None
    assert brand.fingerprint() == load_brand("testbrand").fingerprint()


def test_logo_cannot_point_outside_the_kit(brand):
    evil = brand.model_copy(update={"logo": "../../demoreel.db"})
    assert evil.logo_path is None


# ------------------------------------------------------------------------------------------- logo

def test_logo_is_split_into_coloured_pieces(logo_file):
    lg = load_logo(logo_file, "#0b0a09", size=256)
    mains = [p for p in lg.parts if not p.is_accent]
    accents = [p for p in lg.parts if p.is_accent]
    assert len(mains) == 2 and len(accents) == 1
    assert sorted(round(p.cx) for p in mains) == pytest.approx([65, 185], abs=3)
    assert accents[0].color[0] > 200 and accents[0].color[2] < 80  # orange
    assert lg.rgba[5, 5, 3] == 0 and lg.rgba[100, 60, 3] == 255  # background keyed out, shape opaque
    x0, y0, x1, y1 = lg.bbox
    assert (x0, y0) == pytest.approx((30, 60), abs=3) and (x1, y1) == pytest.approx((221, 201), abs=3)


def test_empty_logo_is_rejected(tmp_path):
    p = tmp_path / "blank.png"
    Image.new("RGB", (64, 64), (0, 0, 0)).save(p)
    with pytest.raises(ValueError):
        load_logo(p, "#000000", size=64)


# ------------------------------------------------------------------------------------------- sound

@pytest.mark.parametrize("make", [
    lambda: sfx.whoosh(0.2), lambda: sfx.impact(), lambda: sfx.riser(0.3), lambda: sfx.tick(),
    lambda: sfx.sweep_down(0.2), lambda: sfx.drone(0.4)])
def test_sound_effects_are_finite_stereo(make):
    a = make()
    assert a.ndim == 2 and a.shape[1] == 2 and a.dtype == np.float32
    assert np.isfinite(a).all() and 0.01 < np.abs(a).max() < 4.0


def test_sounds_are_deterministic_and_mix_writes_a_valid_wav(tmp_path):
    assert np.array_equal(sfx.whoosh(0.2, seed=3), sfx.whoosh(0.2, seed=3))
    mix = sfx.Mix(1.0)
    mix.add(sfx.impact(), 0.2)
    mix.add(sfx.tick(), 0.9)  # runs past the end: must be clipped, not crash
    out = mix.write(tmp_path / "m.wav")
    with wave.open(str(out)) as w:
        assert (w.getnchannels(), w.getframerate(), w.getnframes()) == (2, sfx.SR, sfx.SR)
    assert np.abs(mix.render(-1.5)).max() == pytest.approx(10 ** (-1.5 / 20), rel=1e-3)


# ------------------------------------------------------------------------------------------- assembly

def test_probe_and_assemble_with_transitions(tmp_path):
    a = lavfi_clip(tmp_path / "a.mp4", 0.6, "red")
    b = lavfi_clip(tmp_path / "b.mp4", 1.0, "blue", audio=False)  # no audio stream: silence is synthesised
    c = lavfi_clip(tmp_path / "c.mp4", 0.6, "green", size="32x32")  # different size: conformed
    info = probe(b)
    assert (info.width, info.height, info.has_audio) == (64, 64, False) and info.duration == pytest.approx(1.0, abs=0.15)
    out = tmp_path / "out.mp4"
    total = assemble([Part(a), Part(b, "fadewhite", 0.2), Part(c, "fade", 0.2)], out, width=64, height=64, fps=10,
                     loudness=None)
    got = probe(out)
    assert total == pytest.approx(0.6 + 1.0 + 0.6 - 0.4, abs=0.05)
    assert got.duration == pytest.approx(total, abs=0.25) and got.has_audio and (got.width, got.height) == (64, 64)
    with pytest.raises(RenderError):
        assemble([], out, width=64, height=64, fps=10)


def test_finish_without_a_kit_just_moves_the_body(tmp_path):
    body = tmp_path / "body.mp4"
    body.write_bytes(b"not really a video")
    out = tmp_path / "final.mp4"
    duration, details = finish(body, out, Brand(), width=64, height=64, fps=10, body_duration=3.5)
    assert duration == 3.5 and out.read_bytes() == b"not really a video" and not body.exists()
    assert details == {"brand": "demoreel", "intro": False, "outro": False, "music": False, "body_offset_s": 0.0}


def test_bumpers_are_generated_cached_and_wrapped_around_the_body(tmp_path, brand):
    from demoreel.compose.bumper import ensure_bumper

    intro = ensure_bumper(brand, "intro", 160, 90, 10)
    assert intro is not None and intro.is_file()
    info = probe(intro)
    assert info.has_audio and info.duration == pytest.approx(0.8, abs=0.2) and (info.width, info.height) == (160, 90)
    assert ensure_bumper(brand, "intro", 160, 90, 10) == intro  # cached, not re-rendered
    assert ensure_bumper(brand.model_copy(update={"bumper": BumperSpec(style="none")}), "intro", 160, 90, 10) is None

    body = lavfi_clip(tmp_path / "body.mp4", 1.0, "gray", size="160x90")
    out = tmp_path / "final.mp4"
    duration, details = finish(body, out, brand, width=160, height=90, fps=10, body_duration=1.0)
    assert details["intro"] and details["outro"] and not details["music"]
    assert duration == pytest.approx(0.8 + 1.0 + 0.8 - 0.44, abs=0.1) and probe(out).has_audio
    # opting out per render
    d2, det2 = finish(lavfi_clip(tmp_path / "b2.mp4", 1.0), tmp_path / "f2.mp4", brand, width=160, height=90, fps=10,
                      body_duration=1.0, intro=False, outro=False)
    assert d2 == 1.0 and not det2["intro"] and not det2["outro"]


# ------------------------------------------------------------------------------------------- masters

def seg(a, b, pov="ct1", speed=1.0):
    return ClipSegment(a, b, pov, speed)


def test_capture_frame_rate_follows_the_slowest_segment():
    assert masters.capture_fps_for([seg(0, 100)], 30) == 30
    assert masters.capture_fps_for([seg(0, 100), seg(100, 200, speed=0.5)], 30) == 60
    assert masters.capture_fps_for([seg(0, 100, speed=0.4)], 30) == 75
    assert masters.capture_fps_for([seg(0, 100, speed=0.01)], 30) == masters.MAX_CAPTURE_FPS
    assert masters.capture_fps_for([seg(0, 100, speed=2.0)], 30) == 30  # fast motion needs no extra frames


def test_capture_is_widened_by_handles_and_merged(sample_analysis):
    a = sample_analysis  # round 1 spans ticks 0..5400
    clips = [Clip(segments=[seg(2000, 2300), seg(2310, 2600)]), Clip(segments=[seg(2500, 2700, pov="t1")])]
    record, covered = masters.capture_clips(clips, a.rounds, 64.0, a.total_ticks)
    assert [(c.start_tick, c.end_tick, c.pov) for c in covered][0] == (2000 - 96, 2600 + 64, "ct1")  # merged + handles
    # Another player's play overlapping in time is its own take, recorded in full (both got kills in that fight).
    assert (covered[1].start_tick, covered[1].end_tick, covered[1].pov) == (2500 - 96, 2700 + 64, "t1")
    assert len(record) == 2 and all(len(c.segments) == 1 and c.segments[0].speed == 1.0 for c in record)
    # handles never cross into the previous round
    _rec, cov = masters.capture_clips([Clip(segments=[seg(6010, 6400)])], a.rounds, 64.0, a.total_ticks)
    assert cov[0].start_tick == 6000
    # the game cannot record a player's view in the first ticks of a demo, and renders are cut to what can exist
    _rec, cov = masters.capture_clips([Clip(segments=[seg(10, 400)])], a.rounds, 64.0, a.total_ticks)
    assert cov[0].start_tick == masters.FIRST_TICK
    cut = masters.recordable([Clip(segments=[seg(10, 400), seg(50, 60)])], a.total_ticks)
    assert [(x.start_tick, x.end_tick) for x in cut[0].segments] == [(masters.FIRST_TICK, 400)] == [(160, 400)]
    # ... and stay the same clips otherwise: composition edits by a segment's role and the clip's action ticks
    kept = masters.recordable([Clip(segments=[seg(10, 400), ClipSegment(300, 400, "t1", 0.5, "Replay · T1", "replay")],
                                    title="t", highlight_id="h_1", action_ticks=[120, 300])], a.total_ticks)[0]
    assert kept.action_ticks == [120, 300] and (kept.title, kept.highlight_id) == ("t", "h_1")
    assert [(x.role, x.caption, x.speed) for x in kept.segments] == [("play", None, 1.0), ("replay", "Replay · T1", 0.5)]


def test_larger_footage_serves_any_shape_and_smaller_only_as_fallback():
    match_id = "m_sizetest"
    video = get_settings().path("tmp", "size-master.mp4")
    video.write_bytes(b"x" * 10)
    masters.save_master(match_id, video, width=1280, height=720, fps=60,
                        spans=[{"start_tick": 1900, "end_tick": 2700, "frames": 750}],
                        covered=[masters.Covered(1900, 2700, "ct1")], extra={})
    try:
        want = {"width": 1920, "height": 1080, "extra": {}}
        assert masters.find_master(match_id, [seg(2000, 2300)], min_fps=30, **want) is None  # recorded too small
        assert masters.find_master(match_id, [seg(2000, 2300)], min_fps=30, allow_lower_fps=True, **want) is not None
        assert masters.find_master(match_id, [seg(2000, 2300)], min_fps=30, width=1280, height=720, extra={}) is not None
        assert masters.find_master(match_id, [seg(2000, 2300)], min_fps=30, width=640, height=360, extra={}) is not None
    finally:
        masters.delete_masters(match_id)


def test_master_reuse_rules(sample_analysis):
    match_id = "m_mastertest"
    video = get_settings().path("tmp", "fake-master.mp4")
    video.write_bytes(b"x" * 10)
    m = masters.save_master(match_id, video, width=1280, height=720, fps=30,
                            spans=[{"start_tick": 1900, "end_tick": 2700, "frames": 375}],
                            covered=[masters.Covered(1900, 2700, "ct1")], extra={})
    try:
        kw = {"width": 1280, "height": 720, "extra": {}}
        assert masters.find_master(match_id, [seg(2000, 2300)], min_fps=30, **kw).id == m.id  # tighter cut: reuse
        assert masters.find_master(match_id, [seg(1900, 2700)], min_fps=30, **kw) is not None
        assert masters.find_master(match_id, [seg(1800, 2300)], min_fps=30, **kw) is None  # starts before the footage
        assert masters.find_master(match_id, [seg(2000, 2300, pov="t1")], min_fps=30, **kw) is None  # other POV
        assert masters.find_master(match_id, [seg(2000, 2300)], min_fps=60, **kw) is None  # needs more frames…
        assert masters.find_master(match_id, [seg(2000, 2300)], min_fps=60, allow_lower_fps=True, **kw) is not None
        assert masters.find_master(match_id, [seg(2000, 2300)], min_fps=30, width=1920, height=1080, extra={}) is None
        assert masters.find_master(match_id, [seg(2000, 2300)], min_fps=30, width=1280, height=720,
                                   extra={"ingame_xray": True}) is None  # different picture
        assert m.as_spans() == [Span(0.0, 1900, 2700, "ct1")]  # with the point of view it was recorded from
        # X-ray "auto": footage recorded without outlines still serves a play with ordinary kills, but a play
        # with a kill through smoke needs footage recorded in auto mode.
        from types import SimpleNamespace as NS

        plain = [NS(tick=2100, attacker=NS(steamid="ct1"), through_smoke=False, wallbang=False)]
        smoke = [NS(tick=2100, attacker=NS(steamid="ct1"), through_smoke=True, wallbang=False)]
        m.xray = "off"
        m.path.with_suffix(".json").write_text(__import__("json").dumps(__import__("dataclasses").asdict(m)),
                                               encoding="utf-8")
        assert masters.find_master(match_id, [seg(2000, 2300)], min_fps=30, kills=plain, **kw) is not None
        assert masters.find_master(match_id, [seg(2000, 2300)], min_fps=30, kills=smoke, **kw) is None
        assert masters.find_master(match_id, [seg(2400, 2600)], min_fps=30, kills=smoke, **kw) is not None  # elsewhere
        off = {**kw, "extra": {"ingame_xray": False}}
        assert masters.find_master(match_id, [seg(2000, 2300)], min_fps=30, kills=smoke, **off) is not None
    finally:
        masters.delete_masters(match_id)
    assert masters.list_masters(match_id) == []


# ------------------------------------------------------------------------------------------- retiming

def test_shots_follow_segment_speed_and_flag_time_jumps():
    spans = [Span(0.0, 1000, 1640), Span(10.0, 5000, 5320)]
    clips = [Clip(segments=[seg(1000, 1320), seg(1320, 1640, speed=0.5), seg(5000, 5320)])]
    shots = _plan(clips, spans, tickrate=64.0, fps=30)
    assert [s.out_frames for s in shots] == [150, 300, 150]  # 5 s, 5 s at half speed = 10 s, 5 s
    assert [s.jump for s in shots] == [False, False, True]
    assert shots[1].src_t0 == pytest.approx(5.0) and shots[2].src_t0 == pytest.approx(10.0)
    with pytest.raises(RenderError):
        _plan([Clip(segments=[seg(3000, 3100)])], spans, tickrate=64.0, fps=30)


def test_a_segment_is_cut_from_the_footage_of_its_own_point_of_view():
    # The victim's take for the replay covers the same ticks as the killer's. Takes are stored in tick order, so
    # looking the footage up by tick alone showed the replay from the killer's eyes again (in slow motion) and,
    # the other way round, a play from the victim's.
    play, again = seg(2096, 2400), ClipSegment(2000, 2224, "t1", 0.5, "Replay · T1", "replay")
    clip = Clip(segments=[play, again])
    # the replay starts before the play: the victim's take comes first and the killer's starts right at its start
    spans = [Span(0.0, 1904, 2288, "t1"), Span(6.0, 2000, 2464, "ct1")]
    shots = _plan([clip], spans, tickrate=64.0, fps=30)
    assert [s.src_t0 for s in shots] == [pytest.approx(6.0 + 96 / 64), pytest.approx(96 / 64)]
    # ... and the usual order: the killer's take starts first, the victim's inside it
    late = ClipSegment(2200, 2424, "t1", 0.5, "Replay · T1", "replay")
    spans = [Span(0.0, 2000, 2496, "ct1"), Span(7.75, 2104, 2488, "t1"), Span(13.75, 2150, 2600, "ct2")]
    shots = _plan([Clip(segments=[seg(2110, 2400), late])], spans, tickrate=64.0, fps=30)
    assert [s.src_t0 for s in shots] == [pytest.approx(110 / 64), pytest.approx(7.75 + 96 / 64)]
    # footage that does not say whose view it is keeps working by ticks
    spans = [Span(0.0, 2000, 2496)]
    assert _plan([Clip(segments=[seg(2110, 2400), late])], spans, tickrate=64.0, fps=30)[1].src_t0 == pytest.approx(200 / 64)


def test_a_master_tells_whose_view_each_take_is():
    match_id = "m_povtest"
    video = get_settings().path("tmp", "pov-master.mp4")
    video.write_bytes(b"x" * 10)
    takes = [{"start_tick": 1904, "end_tick": 2288, "frames": 180}, {"start_tick": 2000, "end_tick": 2464, "frames": 218}]
    try:
        m = masters.save_master(match_id, video, width=1280, height=720, fps=30, spans=takes,
                                covered=[masters.Covered(1904, 2288, "t1"), masters.Covered(2000, 2464, "ct1")], extra={})
        assert m.as_spans() == [Span(0.0, 1904, 2288, "t1"), Span(6.0, 2000, 2464, "ct1")]
        # a take the recorder skipped: the remaining one is matched by its ticks
        m.spans = takes[1:]
        assert m.as_spans() == [Span(0.0, 2000, 2464, "ct1")]
        # the same ticks from two views and one take missing: unknown rather than a guess
        m.covered = [masters.Covered(2000, 2464, "t1"), masters.Covered(2000, 2464, "ct1")]
        assert m.as_spans() == [Span(0.0, 2000, 2464, None)]
    finally:
        masters.delete_masters(match_id)


def test_hud_texts_come_from_the_analysis(sample_analysis):
    # round 1 of the sample: ct1 kills all five terrorists
    kills = [k.tick for k in sample_analysis.rounds[0].kills]
    clip = Clip(segments=[seg(kills[0] - 100, kills[-1] + 100)], title="x")
    info = clip_info(clip, sample_analysis, "pt-BR")
    assert info.player == "CT1" and info.label == "ACE" and info.kill_ticks == kills
    assert info.round_text == "Round 1" and (info.score_left, info.score_right) == ("0", "0")
    assert "AK-47" in info.detail and "3 HS" in info.detail
    # a cut that only shows two of the kills is described as what it shows
    short = clip_info(Clip(segments=[seg(kills[0] - 10, kills[1] + 10)]), sample_analysis, "en")
    assert short.label == "2 kills" and len(short.kill_ticks) == 2


# ------------------------------------------------------------------------------------------- renderer choice

def test_stored_footage_makes_ingame_renders_possible_without_the_game(sample_analysis):
    from demoreel.db import Highlight, Match, init_db, session_scope
    from demoreel.schemas import RenderOptionsIn

    init_db()
    m = Match(id="m_plantest", sha256="x", filename="x.dem", size_bytes=1, demo_path="demos/x.dem")
    h = Highlight(id="h_1", match_id=m.id, kind="ace", title="t",
                  segments=[{"start_tick": 2000, "end_tick": 2300, "pov_steamid": "ct1", "speed": 1.0}])
    opts = RenderOptionsIn()

    def plan(highlight, renderer):  # (renderer, kind of worker, the one remote worker that can do it)
        with session_scope() as s:
            return service._plan_renderer(s, m, [highlight], renderer, opts)

    assert plan(h, "auto") == ("radar", "cpu", None)  # nothing recorded, no game
    with pytest.raises(service.Conflict):
        plan(h, "ingame")
    video = get_settings().path("tmp", "plan-master.mp4")
    video.write_bytes(b"x" * 10)
    masters.save_master(m.id, video, width=1280, height=720, fps=30,
                        spans=[{"start_tick": 1900, "end_tick": 2400, "frames": 234}],
                        covered=[masters.Covered(1900, 2400, "ct1")], extra={})
    try:
        assert plan(h, "auto") == ("ingame", "cpu", None)
        assert plan(h, "ingame") == ("ingame", "cpu", None)
        assert plan(h, "radar") == ("radar", "cpu", None)
        slow = Highlight(id="h_2", match_id=m.id, kind="ace", title="t",
                         segments=[{"start_tick": 2000, "end_tick": 2300, "pov_steamid": "ct1", "speed": 0.5}])
        assert plan(slow, "ingame") == ("ingame", "cpu", None)  # lower-fps footage accepted
        with pytest.raises(service.ServiceError):
            plan(h, "vhs")
    finally:
        masters.delete_masters(m.id)


# ------------------------------------------------------------------------------------------- shapes

def test_layout_follows_the_shape_of_the_frame():
    from demoreel.compose.overlay import Layout

    land = Layout.choose(1920, 1080)
    assert land.kind == "landscape" and land.window == (0, 0, 1920, 1080) and land.hud_scale == 1.5
    tall = Layout.choose(1080, 1920)
    x, y, w, h = tall.window
    assert tall.kind == "portrait" and (w, h) == (1080, 1350) and x == 0 and 200 < y < 300 and y + h < 1920
    assert "crop=" in tall.vf and "scale=1080:1350" in tall.vf  # a 4:5 window cut from the centre of the master
    assert Layout.choose(1080, 1080).kind == "square"


def test_format_names_and_sizes():
    from demoreel.schemas import RenderOptionsIn

    assert service.output_size(RenderOptionsIn(format="9:16")) == (1080, 1920)
    assert service.output_size(RenderOptionsIn(format="1:1")) == (1080, 1080)
    assert service.output_size(RenderOptionsIn()) == (get_settings().render_width, get_settings().render_height)
    assert service.output_size(RenderOptionsIn(format="9:16", width=720, height=1280)) == (720, 1280)  # explicit wins
    assert service.format_of(1080, 1920) == "9:16" and service.format_of(1280, 720) == "16:9"
    assert service.format_of(1080, 1080) == "1:1" and service.format_of(None, None, {"format": "9:16"}) == "9:16"


def test_vertical_bumper_fits_the_frame(brand):
    from demoreel.compose.bumper import _Scene

    tall = _Scene(brand, "intro", 270, 480, 10)
    wide = _Scene(brand, "intro", 480, 270, 10)
    assert tall.s == pytest.approx(270 / 720) and wide.s == pytest.approx(270 / 720)
    assert tall.logo_px <= 270 * 0.6 + 1e-6  # the mark is sized by the width when the frame is tall
    frame = tall.frame(tall.n_frames // 2)
    assert frame.shape == (480, 270, 4) and frame[..., 3].min() == 255


def test_a_slow_motion_play_does_not_raise_the_frame_rate_of_the_whole_batch():
    from demoreel.compose import master as masters
    from demoreel.render.base import Clip, ClipSegment

    normal = [Clip(segments=[ClipSegment(1000 * i, 1000 * i + 500, "a", 1.0, None)], highlight_id=f"h{i}") for i in (1, 2, 3)]
    slow = Clip(segments=[ClipSegment(9000, 9300, "a", 1.0, None), ClipSegment(9300, 9400, "a", 0.4, None)], highlight_id="s")
    many = [Clip(segments=[ClipSegment(1000 * i, 1000 * i + 900, "a", 1.0, None)], highlight_id=f"h{i}") for i in range(1, 9)]
    groups = masters.capture_groups([*many, slow], 30, 30)
    assert list(groups) == [30, 75]
    assert len(groups[30]) == 8 and [c.highlight_id for c in groups[75]] == ["s"]
    # a few short plays are cheaper to record with extra frames than to open the game a second time for
    few = masters.capture_groups([*normal[:2], slow], 30, 30)
    assert list(few) == [75] and {c.highlight_id for c in few[75]} == {"h1", "h2", "s"}
    # ... and from a higher floor the step to the slow-motion rate is small enough to share one session too
    assert list(masters.capture_groups([*many, slow], 30, 60)) == [75]
    assert masters.capture_groups([], 30, 30) == {}


# ------------------------------------------------------------------------------------------- editing
# One play is used throughout. Round 2: four CTs die, then ct1 kills all five terrorists - 6 s, 2 s, 7 s and
# 1 s apart - and wins the round alone. The clip shows it in a single 21 s segment.

TR = 64
K0 = ROUND_TICKS + 2400
PLAY_KILLS = [K0, K0 + 6 * TR, K0 + 8 * TR, K0 + 15 * TR, K0 + 16 * TR]
PLAY = (K0 - 3 * TR, PLAY_KILLS[-1] + 2 * TR)
ACTIONS = [K0, K0 + 200, *PLAY_KILLS[1:]]  # what he did: the kills, and a burst 3.1 s after the first one
STEP = TR / 30  # ticks per frame in real time


@pytest.fixture
def play_analysis():
    alone = ("CT", [kill(ROUND_TICKS + 2000 + i * 40, 2, "t1", CT[i + 1]) for i in range(4)]
             + [kill(t, 2, "ct1", T[i]) for i, t in enumerate(PLAY_KILLS)])
    return analyze(make_demo([ace_round(1), alone]))


def play_clip(*more, actions=True):
    return Clip(segments=[seg(*PLAY), *more], action_ticks=list(ACTIONS) if actions else [])


def replay_seg():
    """The last kill again, from the victim's eyes, at half speed (what the analysis adds to plays of tier S)."""
    return ClipSegment(PLAY_KILLS[-1] - 3 * TR, PLAY_KILLS[-1] + TR // 2, "t5", 0.5, "Replay · T5", "replay")


def test_only_quiet_stretches_between_kills_are_sped_up():
    # 6 s between kills 1 and 2: 0.6 s after the kill and 1 s before the next stay, and so does the burst with
    # its window; before it 2.1 s are left (3x), after it 1.3 s (too short). 2 s and 1 s gaps are one exchange.
    # 7 s between kills 3 and 4: 5.4 s of nothing -> 4x.
    assert quiet_stretches(*PLAY, PLAY_KILLS, ACTIONS, TR) == [
        (K0 + 39, K0 + 174, 3.0), (K0 + 8 * TR + 39, K0 + 14 * TR, 4.0)]
    assert quiet_stretches(*PLAY, PLAY_KILLS, [], TR) == []  # nothing is known about what he did: hands off
    assert quiet_stretches(*PLAY, PLAY_KILLS[:1], ACTIONS, TR) == []  # lead-in and post-roll are never touched
    assert quiet_stretches(0, 2000, [500, 500 + int(3.05 * TR)], [500], TR) == []
    assert quiet_stretches(0, 2000, [500, 500 + int(3.2 * TR)], [500], TR) == [(539, 640, 3.0)]
    assert quiet_stretches(0, 2000, [500, 1500], list(range(500, 1501, 100)), TR) == []  # shooting all the way


def test_speed_ramp_shortens_the_wait_and_leaves_the_action_alone(play_analysis):
    plain = timeline([play_clip()], play_analysis, fps=30, ramp=False, freeze=False)
    tl = timeline([play_clip()], play_analysis, fps=30, freeze=False)
    assert plain.frames == 630 and plain.ramped() == 0
    assert tl.ramped() == pytest.approx((135 / TR - 25 / 30) + (345 / TR - 45 / 30), abs=0.08)  # about 5.2 s
    assert tl.duration == pytest.approx(plain.duration - tl.ramped(), abs=0.04)
    shot = tl.shots[0]
    assert [p.kind for p in shot.pieces] == ["play", "fast", "play", "fast", "play"]
    step = np.diff(shot.ticks)
    assert step.min() >= STEP - 1e-6 and 3.5 * STEP < step.max() < 4.3 * STEP  # never backwards, up to 4x
    for k in PLAY_KILLS:  # real time from 1 s before every kill to 0.6 s after it
        near = (shot.ticks[:-1] >= k - TR) & (shot.ticks[:-1] <= k + 0.6 * TR - STEP)
        assert near.sum() > 40 and np.allclose(step[near], STEP)
    assert step[shot.pieces[0].frames] < 1.5 * STEP  # the speed glides up, it does not step
    # the sound follows piece by piece and fills exactly the frames of the picture
    graph, _ = _audio_filter(tl.shots, TR, True, 30)
    assert graph.count("[0:a]atrim") == 5 and graph.count("atempo=2.0") == 2 and "concat=n=5" in graph
    assert sum(float(x) for x in re.findall(r"atrim=0:([\d.]+)", graph)) == pytest.approx(tl.duration, abs=0.01)
    # nothing to go by, slow motion, a replay: no ramp
    assert timeline([play_clip(actions=False)], play_analysis, freeze=False).frames == 630
    slow = Clip(segments=[seg(*PLAY, speed=0.5)], action_ticks=list(ACTIONS))
    assert timeline([slow], play_analysis, freeze=False).frames == 1260
    again = Clip(segments=[ClipSegment(*PLAY, "ct1", 1.0, None, "replay")], action_ticks=list(ACTIONS))
    assert timeline([again], play_analysis).frames == 630


def test_kill_marks_say_when_each_kill_is_on_screen(play_analysis):
    clips = [play_clip()]
    tl = timeline(clips, play_analysis, fps=30)
    marks = tl.marks()
    assert [m["tick"] for m in marks] == PLAY_KILLS and marks == kill_marks(clips, play_analysis, fps=30)
    ticks = np.concatenate([sh.ticks for sh in tl.shots])
    for m in marks:  # the frame at the mark shows the kill, the frame before it does not yet
        t = tl.time_of_tick(m["tick"])
        frame = math.ceil(t * 30 - 1e-6)
        assert ticks[frame] >= m["tick"] - 1e-6 > ticks[frame - 1] and m["t"] == pytest.approx(t, abs=0.001)
    assert marks[0]["t"] == pytest.approx(3.0, abs=0.001)  # nothing changes before the first kill
    assert marks[1]["t"] < 3.0 + 6.0 - 1.0  # the 6 s wait got shorter
    assert time_of_tick(clips, PLAY_KILLS[3], TR, analysis=play_analysis) == pytest.approx(marks[3]["t"], abs=0.001)
    assert time_of_tick(clips, PLAY[1] + 500, TR, analysis=play_analysis) is None
    # without the edits (videos made before them) time is linear, which is also what a call without the
    # analysis assumes
    linear = kill_marks(clips, play_analysis, fps=30, ramp=False, freeze=False)
    assert [m["t"] for m in linear] == pytest.approx([(k - PLAY[0]) / TR for k in PLAY_KILLS], abs=0.001)
    assert time_of_tick(clips, PLAY_KILLS[3], TR) == pytest.approx(linear[3]["t"], abs=0.001)
    # a reel: the second clip's marks come after the first clip
    both = kill_marks([play_clip(), Clip(segments=[seg(1900, 2500)])], play_analysis, fps=30)
    assert len(both) == 10 and both[5]["t"] > tl.duration and both[5]["tick"] == 2000


def test_freeze_frame_holds_the_decisive_kill(play_analysis):
    tl = timeline([play_clip()], play_analysis, fps=30, ramp=False)
    first, n = tl.freeze_of(0)
    last = PLAY_KILLS[-1]
    ticks = tl.shots[0].ticks
    assert n == round(overlay.FREEZE_S * 30) and tl.frames == 630 + n
    assert np.all(ticks[first:first + n + 1] == ticks[first]) and last < ticks[first] <= last + 0.1 * TR
    shown = tl.time_of_tick(last)
    assert first / 30 - 0.1 <= shown <= first / 30  # the label lands as the picture stops
    assert tl.time_of_tick(last + TR) - shown == pytest.approx(1.0 + n / 30, abs=0.04)
    assert tl.time_of_tick(PLAY_KILLS[1] + TR) - tl.time_of_tick(PLAY_KILLS[1]) == pytest.approx(1.0, abs=0.01)
    # the game sound makes room: silence under the hold, a short fade on each side
    graph, _ = _audio_filter(tl.shots, TR, True, 30)
    assert graph.count("anullsrc") == 1 and "afade=t=out" in graph and "afade=t=in" in graph
    assert sum(float(x) for x in re.findall(r"atrim=0:([\d.]+)", graph)) == pytest.approx(tl.duration, abs=0.01)
    # the only kill of a clip is its decisive kill; no kills, or switched off: no freeze
    one = Clip(segments=[seg(K0 - 2 * TR, K0 + 2 * TR)])
    assert timeline([one], play_analysis).freeze_of(0) is not None
    assert timeline([one], play_analysis, freeze=False).freeze_of(0) is None
    assert timeline([Clip(segments=[seg(K0 - 3 * TR, K0 - TR)])], play_analysis).freeze_of(0) is None
    # 60 fps masters and outputs are the same thing with more frames
    assert timeline([play_clip()], play_analysis, fps=60, ramp=False).freeze_of(0)[1] == round(overlay.FREEZE_S * 60)


def test_a_replay_is_not_part_of_the_play(play_analysis):
    clip = play_clip(replay_seg())
    info = clip_info(clip, play_analysis, "pt-BR")
    assert info.player == "CT1" and info.kill_ticks == PLAY_KILLS and info.label == "ACE"
    tl = timeline([clip], play_analysis, fps=30)
    play, again = tl.shots
    assert (play.role, again.role, again.jump) == ("play", "replay", True)
    assert again.out_frames == 210 and [p.kind for p in again.pieces] == ["play"]  # 3.5 s at half speed, untouched
    # the kill is marked (and held) where the play shows it, once
    assert len(tl.marks()) == 5 and tl.marks()[-1]["t"] < play.out_frames / 30 > tl.freeze_of(0)[0] / 30
    assert time_of_tick([clip], PLAY_KILLS[-1], TR) == pytest.approx((PLAY_KILLS[-1] - PLAY[0]) / TR)
    assert overlay._replay_caption("Replay · T5", overlay._words("pt-BR")) == "Visão de T5"
    assert overlay._replay_caption("Replay · T5", overlay._words("en")) == "T5's view"
    assert overlay._replay_caption("1v5", overlay._words("en")) == "1v5"


INFO = ClipInfo(player="CT1", label="Clutch 1v3", detail="AK-47", round_text="Round 2", score_left="4",
                score_right="3", team_left="Time A", team_right="Time B", kill_ticks=[100, 200, 300])


def hud_pixels(hud: Hud, info: ClipInfo, **state) -> np.ndarray:
    """What the HUD draws for this state: RGBA on a transparent frame."""
    surface = skia.Surface(hud.w, hud.h)
    canvas = surface.getCanvas()
    canvas.clear(skia.ColorTRANSPARENT)
    hud.draw(canvas, HudState(clip=info, **{"t_clip": 6.0, "t_left": 3.0, "kills_done": 0, "t_since_kill": None, **state}))
    return surface.toarray(colorType=skia.kRGBA_8888_ColorType)


def test_replay_hud_is_a_badge_a_caption_and_the_bars():
    hud = Hud(Brand(), 1280, 720)
    play = hud_pixels(hud, INFO, t_clip=1.0, kills_done=1, t_since_kill=0.3)[..., 3] > 0
    again = hud_pixels(hud, INFO, t_clip=1.0, kills_done=3, t_since_kill=0.3, replay=True, slow=1.0,
                       caption="Visão de T5")[..., 3] > 0
    nameplate, counter, score = np.s_[544:650, 0:330], np.s_[612:672, 500:780], np.s_[46:72, 200:640]
    assert play[nameplate].any() and play[counter].any() and play[score].any()
    assert not again[nameplate].any() and not again[counter].any() and not again[score].any()
    assert again[60:90, 30:200].any() and not again[45:58].any()  # the badge, where the score was, under the bar
    assert again[:40].all() and again[-40:].all()  # letterbox
    assert again[565:600, 560:720].any() and not again[100:540].any()  # the caption, and nothing else


def test_caption_and_the_final_label_do_not_overlap():
    for w, h, scale in ((1280, 720, None), (1080, 1350, 1.5)):
        hud = Hud(Brand(), w, h, scale=scale)
        for since in (0.0, 0.03, 0.5):  # while the label lands (drawn larger) and once it has settled
            state = {"kills_done": 3, "t_since_kill": since, "freeze_s": 0.35}
            without = hud_pixels(hud, INFO, **state)
            captioned = hud_pixels(hud, INFO, caption="1v3 no bomb B", **state)
            caption_rows = np.flatnonzero((captioned != without).any(axis=(1, 2)))
            label_rows = np.flatnonzero(without[h // 2:, :, 3].any(axis=1)) + h // 2
            assert len(caption_rows) and len(label_rows) and caption_rows.max() < label_rows.min()
    # a clip with nothing in that lane (one kill, no label) keeps its caption on the usual one
    bare = ClipInfo(player="CT1", label="", detail="", round_text="", kill_ticks=[100])
    state = HudState(clip=bare, t_clip=6, t_left=3, kills_done=1, t_since_kill=0.5, caption="x")
    assert hud._caption_rect(state)[2] > hud._caption_rect(HudState(**{**state.__dict__, "clip": INFO}))[2]
    # while the nameplate is on screen, a caption that would sit on it waits; one that fits beside it does not
    early = {"t_clip": 1.0, "kills_done": 0}
    assert np.array_equal(hud_pixels(hud, INFO, caption="uma legenda bem comprida aqui", **early), hud_pixels(hud, INFO, **early))
    wide = Hud(Brand(), 1280, 720)
    assert not np.array_equal(hud_pixels(wide, INFO, caption="1v3", **early), hud_pixels(wide, INFO, **early))


def test_final_label_lands_on_the_decisive_kill_and_stays_through_the_freeze():
    hud = Hud(Brand(), 1280, 720)
    lane = np.s_[560:640, 440:840]  # above the kill counter

    def label(**state):
        return hud_pixels(hud, INFO, **state)[lane][..., 3].any()

    assert not label(kills_done=2, t_since_kill=2.0)
    assert label(kills_done=3, t_since_kill=0.05, freeze_s=0.35) and label(kills_done=3, t_since_kill=1.0, freeze_s=0.35)
    assert not label(kills_done=3, t_since_kill=1.0 + 0.35, freeze_s=0.0)  # without a freeze it leaves sooner
    assert not label(kills_done=3, t_since_kill=2.0, freeze_s=0.35)
    single = ClipInfo(player="CT1", label="Clutch 1v1", detail="", round_text="", kill_ticks=[100])
    assert hud_pixels(hud, single, kills_done=1, t_since_kill=0.2, freeze_s=0.35)[lane][..., 3].any()  # one kill: no counter, still the label


def test_nameplate_says_what_is_at_stake(play_analysis, sample_analysis):
    hud = Hud(Brand(), 1280, 720)
    info = clip_info(play_clip(), play_analysis, "pt-BR")
    assert info.context == "1v5 · 1 : 0" and "AK-47" in info.detail  # alone against five, one round up
    assert hud._nameplate_box(info)[1:3] == ("1v5", "1 : 0")  # the label ("ACE") waits for the last kill
    # a team on twelve: match point, with the player's own team first
    r13 = 12 * ROUND_TICKS + 2000
    last = ("CT", [kill(r13, 13, "ct1", "t1"), kill(r13 + 100, 13, "ct1", "t2"), kill(r13 + 150, 13, "t3", "ct2")])
    match = analyze(make_demo([*(ace_round(n) for n in range(1, 13)), last]))
    mine = clip_info(Clip(segments=[seg(r13 - 200, r13 + 300)]), match, "pt-BR")
    assert mine.context == "Match point · 12 : 0" and mine.label == "2 kills"
    assert clip_info(Clip(segments=[seg(r13 - 200, r13 + 300, pov="t3")]), match, "en").context == "Match point · 0 : 12"
    assert clip_info(Clip(segments=[seg(1900, 2500)]), match, "en").context == "Pistol round · 0 : 0"
    assert clip_info(Clip(segments=[seg(1900, 2500)]), match, "pt-BR").context == "Pistol · 0 : 0"
    # nothing at stake: what the play is, and the weapon, as before
    r2 = ROUND_TICKS + 2000
    plain = clip_info(Clip(segments=[seg(r2 + 400, r2 + 800, pov="t2")]), sample_analysis, "pt-BR")
    assert plain.context == "" and plain.detail and hud._nameplate_box(plain)[1:3] == ("3 kills", plain.detail)


def test_names_with_symbols_the_brand_font_lacks_get_real_glyphs():
    symbols, nick = "⑯✓☆", "⑯ ✓ ☆ ranzinza"  # platform badges in front of a nick: Barlow has none of them
    mgr = skia.FontMgr()
    if any(mgr.matchFamilyStyleCharacter("", skia.FontStyle.Normal(), ["en"], ord(ch)) is None for ch in symbols):
        pytest.skip("no font on this machine has ⑯ ✓ ☆")
    hud = Hud(Brand(), 1280, 720)
    for font in (hud.f_name, hud.f_small, hud.f_caption):  # the nameplate, the teams in the corner, a caption
        assert all(font.unicharToGlyph(ord(ch)) == 0 for ch in symbols)  # the premise: drawn as is, three empty boxes
        runs, width = hud._runs(nick, font)
        assert "".join(t for t, _, _ in runs) == nick and width == pytest.approx(sum(w for _, _, w in runs))
        assert all(f.unicharToGlyph(ord(ch)) != 0 for t, f, _ in runs for ch in t if not ch.isspace())
        assert runs[-1][1] is font and runs[-1][0].endswith("ranzinza")  # the nick itself stays in the brand's font
        assert hud._runs("ranzinza", font) == ((("ranzinza", font, font.measureText("ranzinza")),), font.measureText("ranzinza"))
    # on screen: three different drawings wherever a name goes, not the same box three times
    places = {
        "nameplate": lambda ch: hud_pixels(hud, ClipInfo(**{**INFO.__dict__, "player": ch}), t_clip=1.0)[544:608],
        "team": lambda ch: hud_pixels(hud, ClipInfo(**{**INFO.__dict__, "team_left": ch}))[26:72],
        "caption": lambda ch: hud_pixels(hud, INFO, caption=ch)[500:620],
    }
    for place, draw in places.items():
        frames = [draw(ch) for ch in symbols]
        assert all(f[..., 3].any() for f in frames) and len({f.tobytes() for f in frames}) == len(symbols), place
    # what is measured is what is drawn: the plate is as wide as its name, and a long one is cut to fit
    surface = skia.Surface(600, 120)
    surface.getCanvas().clear(skia.ColorTRANSPARENT)
    width = hud._text(surface.getCanvas(), nick, 10, 80, hud.f_name, skia.ColorWHITE, shadow=False)
    cols = np.flatnonzero(surface.toarray(colorType=skia.kRGBA_8888_ColorType)[..., 3].any(axis=0))
    assert width == hud._width(nick, hud.f_name) and 10 + width - 6 < cols.max() < 10 + width + 2
    assert hud._nameplate_box(ClipInfo(player=nick, label="", detail="", round_text=""))[3] == pytest.approx(width + 44)
    cut = hud._fit("☆ ✓ " * 40, hud.f_name, 300)
    assert cut.endswith("…") and 200 < hud._width(cut, hud.f_name) <= 300
    assert hud._nameplate_box(ClipInfo(player="⑯ ✓ ☆ " * 30, label="", detail="", round_text=""))[3] <= 1280 * 0.42 + 44


def test_vertical_push_in_eases_in_before_the_kill_and_out_after_it():
    env = overlay._punch_env
    assert env(-0.2) == 0 and 0 < env(-0.1) < env(-0.03) < 1 and env(0) == 1 and env(0.1) == 1
    assert 1 > env(0.2) > env(0.4) > 0 and env(0.5) == 0 and env(3) == 0
    # the picture keeps its size and its centre, and gets larger around it
    frame = np.zeros((40, 60, 4), dtype=np.uint8)
    frame[15:25, 25:35] = 255
    pushed = np.frombuffer(overlay._pushed_in(frame.tobytes(), 60, 40, 1.5), dtype=np.uint8).reshape(40, 60, 4)
    assert (pushed[..., 0] > 128).sum() == pytest.approx(100 * 1.5 ** 2, rel=0.1)
    assert pushed[20, 30, 0] == 255 and pushed[2, 2, 0] == 0


def test_chat_reactions_come_from_the_analysis_and_can_be_switched_off(play_analysis):
    from demoreel.schemas import RenderOptionsIn

    a = play_analysis
    last = PLAY_KILLS[-1]
    a.chat = chat_mod.annotate([
        {"tick": 100, "steamid": "t2", "name": "T2", "text": "bora"},  # long before the play
        {"tick": last + 3 * TR, "steamid": "t5", "name": "T5", "text": "q isso hack"},  # the last victim
        {"tick": last + 4 * TR, "steamid": "ct4", "name": "CT4", "text": "alguem tem drop"},  # a bystander's small talk
        {"tick": last + 5 * TR, "steamid": "ct2", "name": "CT2", "text": "kkkkkkk"},
        {"tick": last + 6 * TR, "steamid": "ct3", "name": "CT3", "text": "!rank"},
        {"tick": last + 7 * TR, "steamid": "ct1", "name": "CT1", "text": "quietinho"}], a.rounds, TR)  # the player himself
    info = clip_info(play_clip(), a, "pt-BR")
    assert [(c.name, c.text, c.kind) for c in info.chat] == [
        ("T5", "q isso hack", "whine"), ("CT2", "kkkkkkk", "laugh"), ("CT1", "quietinho", "talk")]
    assert clip_info(play_clip(), a, "pt-BR", chat=False).chat == []
    # the kit decides, a render can overrule it; neither is part of anything cached
    assert Brand().chat is True and Brand(chat=False).fingerprint() == Brand().fingerprint()
    assert RenderOptionsIn().model_dump(exclude_none=True) == {"extra": {}}
    assert RenderOptionsIn(ramp=False, chat=False).model_dump(exclude_none=True) == {"ramp": False, "chat": False, "extra": {}}


@pytest.mark.parametrize(("w", "h", "scale"), [(1280, 720, None), (1080, 1350, 1.5), (1080, 1080, 1.2)])
def test_chat_stack_keeps_clear_of_the_crosshair_and_the_kill_counter(w, h, scale):
    hud = Hud(Brand(), w, h, scale=scale)
    s = hud.s
    info = ClipInfo(**{**INFO.__dict__, "chat": [
        ChatLine("T5", "q isso hack " * 8, "whine"), ChatLine("CT2 com um nome que nao acaba mais nunca", "kkkkkkk", "laugh"),
        ChatLine("ÐΞΛŦĦ ♛", "boa 👏", "praise")]})
    state = {"kills_done": 3, "t_since_kill": 1.0, "freeze_s": 0.35, "t_left": 1.0}
    quiet = hud_pixels(hud, info, **state)
    assert np.array_equal(hud_pixels(hud, info, t_chat=0.0, **state), quiet)

    def box(t_chat):
        changed = (hud_pixels(hud, info, t_chat=t_chat, **state) != quiet).any(axis=2)
        rows, cols = np.flatnonzero(changed.any(axis=1)), np.flatnonzero(changed.any(axis=0))
        return rows[0], rows[-1], cols[0], cols[-1]

    top, bottom, left, right = box(3.0)
    one = box(0.3)
    assert one[1] - one[0] < 50 * s < 110 * s < bottom - top  # one line first, then the three of them
    assert bottom < h - 145 * s  # above the lane of the label and the kill counter
    assert left >= 20 * s and right < w - 20 * s  # inside the picture (the window, in a vertical video)
    assert right < w / 2 - 40 * s or top > h / 2 + 40 * s  # beside the crosshair or below it, never on it
    # with a caption on screen the stack moves up, out of its way
    captioned = (hud_pixels(hud, info, t_chat=3.0, caption="x", **state) != hud_pixels(hud, info, caption="x", **state))
    assert np.flatnonzero(captioned.any(axis=(1, 2)))[-1] < bottom - 40 * s


def lavfi_master(path, seconds, size="320x180"):
    """Footage with something to measure: a white box on the crosshair and a small one that keeps crossing the top."""
    subprocess.run([
        ffmpeg.ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"color=c=black:s={size}:r=30:d={seconds}",
        "-f", "lavfi", "-i", f"color=c=white:s=40x40:r=30:d={seconds}",
        "-f", "lavfi", "-i", f"color=c=white:s=10x10:r=30:d={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-filter_complex", "[0][1]overlay=x=140:y=70[a];[a][2]overlay=x='110+mod(t*40,90)':y=20[v]",
        "-map", "[v]", "-map", "3:a", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


def gray_frame(path, t, width, height):
    raw = subprocess.run([ffmpeg.ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", str(path),
                          "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True, check=True)
    return np.frombuffer(raw.stdout, dtype=np.uint8).reshape(height, width).astype(int)


def test_composed_body_has_the_ramp_the_freeze_the_push_in_and_the_marks(tmp_path, play_analysis):
    clip = play_clip()
    spans = [Span(0.0, PLAY[0] - TR, PLAY[1] + TR)]  # the footage has a second of handle on each side
    master = lavfi_master(tmp_path / "master.mp4", (PLAY[1] - PLAY[0]) / TR + 2)
    out = tmp_path / "body.mp4"
    body = compose_body(master, out, spans=spans, clips=[clip], analysis=play_analysis, brand=Brand(locale="pt-BR"),
                        src_fps=30, fps=30, width=180, height=320, hud=False)
    tl = body.timeline
    assert len(body.marks) == 5 and body.marks == kill_marks([clip], play_analysis, fps=30, spans=spans)
    assert body.duration == tl.duration and 15.5 < body.duration < 17  # 21 s - 5.2 s of ramp + the freeze
    got = probe(out)
    assert got.has_audio and (got.width, got.height) == (180, 320) and got.duration == pytest.approx(body.duration, abs=0.15)
    _x, top, _w, win_h = overlay.Layout.choose(180, 320).window

    def window(t):
        return gray_frame(out, t, 180, 320)[top:top + win_h]

    # push-in: the box on the crosshair is larger on a kill than a second before it
    at = body.marks[1]["t"]
    assert (window(at + 0.05)[60:] > 128).sum() > 1.15 * (window(at - 1.0)[60:] > 128).sum()
    # freeze-frame: the box crossing the top stands still, then moves on
    first, n = tl.freeze_of(0)
    a, b, c = (window((f + 0.03) / 30) for f in (first + 4, first + n - 1, first + n + 10))
    assert np.abs(a - b)[:60].mean() < 1 < np.abs(b - c)[:60].mean()
    # the legacy entry point still returns just the duration
    assert overlay.retime_and_overlay(master, tmp_path / "again.mp4", spans=spans, clips=[clip], analysis=play_analysis,
                                      brand=Brand(), src_fps=30, fps=30, width=64, height=36, hud=False, ramp=False,
                                      freeze=False) == pytest.approx(21.0)


@pytest.mark.parametrize(("width", "height"), [(320, 180), (180, 180)])
def test_composed_body_with_hud_chat_caption_and_replay(tmp_path, play_analysis, width, height):
    a = play_analysis
    a.chat = chat_mod.annotate([{"tick": PLAY_KILLS[-1] + 3 * TR, "steamid": "t5", "name": "T5 ♛", "text": "q isso"}],
                               a.rounds, TR)
    clip = play_clip(replay_seg())
    clip.segments[0].caption = "1v5"
    spans = [Span(0.0, PLAY[0] - TR, PLAY[1] + TR)]
    master = lavfi_master(tmp_path / "master.mp4", (PLAY[1] - PLAY[0]) / TR + 2)
    out = tmp_path / "body.mp4"
    body = compose_body(master, out, spans=spans, clips=[clip], analysis=a, brand=Brand(locale="pt-BR"), src_fps=30,
                        fps=30, width=width, height=height)
    play, again = body.timeline.shots
    assert again.role == "replay" and len(body.marks) == 5
    assert probe(out).duration == pytest.approx(body.duration, abs=0.15) and probe(out).has_audio
    # two seconds into the replay: letterboxed (it is slow motion), and nothing of the play's HUD in the bars
    replay_frame = gray_frame(out, (play.out_frames + 60) / 30, width, height)
    bar = int(height * 0.05)
    assert replay_frame[:bar, width // 2:].max() < 40 and replay_frame[-bar:].max() < 40


def test_a_replay_from_earlier_in_the_footage_plays_instead_of_freezing(tmp_path, play_analysis):
    # Footage is stored in tick order, so the replay of a kill sits before the end of the play in the master:
    # the picture has to go back to it. (It used to hold the last frame of the play for the whole replay.)
    clip = play_clip(replay_seg())
    spans = [Span(0.0, PLAY[0] - TR, PLAY[1] + TR)]
    master = lavfi_master(tmp_path / "master.mp4", (PLAY[1] - PLAY[0]) / TR + 2)
    out = tmp_path / "body.mp4"
    body = compose_body(master, out, spans=spans, clips=[clip], analysis=play_analysis, brand=Brand(locale="pt-BR"),
                        src_fps=30, fps=30, width=320, height=180, hud=False)
    play, again = body.timeline.shots
    assert again.role == "replay" and again.src_t0 == pytest.approx(17.0)  # the play itself ends at second 22

    def box_x(t):  # where the small box that crosses the top is (it moves 40 px per second of footage)
        rows = gray_frame(out, t, 320, 180)[22:28]
        return float(np.flatnonzero((rows > 128).all(axis=0)).mean())

    start = play.out_frames / 30
    # footage second 17.25 and 17.75 (half speed): x = 110 + (t * 40) % 90, plus half the box
    assert box_x(start + 0.5) == pytest.approx(175, abs=4) and box_x(start + 1.5) == pytest.approx(195, abs=4)
