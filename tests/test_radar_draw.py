"""2D replay: demo-data helpers, the re-toned map, and smoke tests that every piece draws in every layout.

Tests run offline with an empty data directory: no radar images and no game icons exist, so everything here
also exercises the fallbacks (bare plotting table instead of a map, text instead of icons)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import skia
from conftest import real_demos

from demoreel.analysis.schema import MatchAnalysis, PlayerRef, RoundSummary
from demoreel.brand import Brand, BrandColors
from demoreel.render import Clip, ClipSegment, RenderError, RenderJob, RenderOptions, maps
from demoreel.render.radar import RadarRenderer, art
from demoreel.render.radar.data import Area, Events, Flight, Hurt, SegmentData, Shot, Track, split_flights
from demoreel.render.radar.hud import BugState, Hud, PlateInfo, RosterRow
from demoreel.render.radar.layout import choose_layout
from demoreel.render.radar.theme import Theme, hand_icon, map_title
from demoreel.render.radar.world import Mark, Scene, Trace, World, blindness, run_starts

SIZES = [(640, 360), (360, 640), (480, 480)]
HOSTILE = ["twitch.tv/trovoadinha", "♿", "W" * 200, "😀🔥", "", "‮abc", "tem pao de queijo?", "ﷺ﷽ﷺ﷽", "a\nb"]


def ink(surface: skia.Surface, background: str = "#0d0f14") -> int:
    """How many pixels differ from the background."""
    px = surface.toarray(colorType=skia.kRGBA_8888_ColorType)[..., :3].astype(int)
    bg = np.array([int(background[i:i + 2], 16) for i in (1, 3, 5)])
    return int((np.abs(px - bg).sum(axis=-1) > 12).sum())


# ------------------------------------------------------------------ data helpers
def test_split_flights_keeps_only_the_moving_part():
    ticks = np.arange(100, 400)
    x = np.concatenate([np.linspace(0, 500, 60), np.full(240, 500.0)])  # flies for 60 ticks, then lies there
    flights = split_flights("smoke", ticks, x, np.zeros(300), np.zeros(300))
    assert len(flights) == 1
    assert flights[0].start == 100 and 157 <= flights[0].end <= 162 and flights[0].kind == "smoke"
    resting = split_flights("smoke", ticks, np.full(300, 5.0), np.zeros(300), np.zeros(300))
    assert resting == []  # already on the ground when we first see it
    assert split_flights("he", np.array([1]), np.array([0.0]), np.array([0.0]), np.array([0.0])) == []


def test_split_flights_splits_reused_entity_ids():
    ticks = np.concatenate([np.arange(100, 140), np.arange(900, 950)])  # the same id, two throws
    x = np.concatenate([np.linspace(0, 300, 40), np.linspace(1000, 1500, 50)])
    flights = split_flights("flash", ticks[::-1], x[::-1], np.zeros(90), np.zeros(90))  # order must not matter
    assert [(f.start, f.end) for f in flights] == [(100, 139), (900, 949)]
    assert flights[0].x[0] == pytest.approx(0) and flights[1].x[-1] == pytest.approx(1500)


def test_blindness_counts_down_from_the_props_value():
    flash = np.zeros(400, dtype=np.float32)
    flash[100:292] = 3.0  # blinded for 3 s at tick 100 (the prop holds the total until it ends)
    left, total = blindness(flash, 64)
    assert left[99] == 0 and left[100] == pytest.approx(3.0) and left[164] == pytest.approx(2.0)
    assert left[291] == pytest.approx(3 - 191 / 64) and left[292] == 0
    assert total[150] == 3.0 and total[0] == 0
    again = flash.copy()
    again[200:292] = 2.0  # flashed again while blind: the countdown restarts
    left2, _ = blindness(again, 64)
    assert left2[200] == pytest.approx(2.0) and left2[199] == pytest.approx(3 - 99 / 64)
    assert blindness(np.zeros(0, dtype=np.float32), 64)[0].shape == (0,)


def test_run_starts():
    active = np.array([0, 1, 1, 1, 0, 0, 1, 1, 0, 1], dtype=bool)
    assert run_starts(active).tolist() == [-1, 1, 1, 1, -1, -1, 6, 6, -1, 9]
    assert run_starts(np.ones(3, dtype=bool)).tolist() == [0, 0, 0]
    assert run_starts(np.zeros(3, dtype=bool)).tolist() == [-1, -1, -1]


def test_bomb_on_the_ground_until_picked_up():
    ev = Events(drops=[(100, 1.0, 2.0, 3.0), (500, 7.0, 8.0, 9.0)], pickups=[50, 300, 900])
    assert ev.bomb_on_ground(99, 0) is None
    assert ev.bomb_on_ground(200, 0) == (1.0, 2.0, 3.0)
    assert ev.bomb_on_ground(300, 0) is None and ev.bomb_on_ground(450, 0) is None  # someone carries it
    assert ev.bomb_on_ground(600, 0) == (7.0, 8.0, 9.0)
    assert ev.bomb_on_ground(600, since=550) is None  # dropped in an earlier round
    assert ev.bomb_on_ground(1000, 0) is None
    assert Events().bomb_on_ground(10, 0) is None


def test_hand_icon_and_map_title():
    assert hand_icon("AK-47") == "ak47" and hand_icon("M4A1-S") == "m4a1_silencer" and hand_icon("M4A4") == "m4a1"
    assert hand_icon("Smoke Grenade") == "smokegrenade" and hand_icon("High Explosive Grenade") == "hegrenade"
    assert hand_icon("C4 Explosive") == "c4" and hand_icon("Zeus x27") == "taser"
    assert {hand_icon(k) for k in ("M9 Bayonet", "Karambit", "Butterfly Knife", "Shadow Daggers")} == {"knife"}
    assert hand_icon("") is None and hand_icon("Plasma Rifle 9000") is None  # unknown: drawn as text
    assert map_title("de_nuke") == "Nuke" and map_title("cs_office") == "Office"
    assert map_title("de_ancient_night") == "Ancient Night" and map_title("workshop/123/de_cache") == "Cache"
    assert map_title("weird") == "Weird"


# ------------------------------------------------------------------ the map
def test_tone_radar_keeps_shape_and_puts_everything_on_the_brand_ramp():
    rng = np.random.default_rng(1)
    rgba = np.zeros((256, 256, 4), dtype=np.uint8)
    rgba[40:200, 40:200, :3] = rng.integers(0, 255, (160, 160, 3))  # a loud, colourful map
    rgba[40:200, 40:200, 3] = 255
    rgba[:, :2] = 255  # hairline frame on the border, as some radar images have
    colors = BrandColors()
    out = art.tone_radar(rgba, colors)
    assert out.shape == rgba.shape and out.dtype == np.uint8
    assert out[..., 3][:30, :].max() == 0 and out[:, :2, 3].max() == 0  # outside stays empty, the frame is gone
    assert (out[..., :3].max(axis=-1) <= out[..., 3]).all()  # premultiplied
    inside = out[60:180, 60:180, :3].astype(int)
    assert (inside.max(axis=-1) - inside.min(axis=-1)).mean() < 60  # far less saturated than the noise it was
    assert inside.mean() < 150  # and it sits on the dark side of the palette


def test_ground_texture_exists_without_a_radar_image(tmp_path):
    levels = art.map_texture(None, BrandColors())
    assert [im.width() for im in levels] == [2048, 1024, 512]
    assert all(im.isOpaque() for im in levels)
    assert art.map_texture(tmp_path / "missing.png", BrandColors()) is levels  # cached
    assert art.pick_level(levels, 0.3).width() == 512 and art.pick_level(levels, 0.9).width() == 1024
    assert art.pick_level(levels, 2.5).width() == 2048


# ------------------------------------------------------------------ HUD
@pytest.mark.parametrize("w, h", SIZES)
def test_hud_draws_every_piece_with_hostile_names(w, h):
    brand = Brand(locale="pt-BR")
    theme, layout = Theme(brand, w, h), choose_layout(w, h)
    hud = Hud(theme, layout)
    surface = skia.Surface(w, h)
    cv = surface.getCanvas()
    cv.clear(theme.col(brand.colors.background))
    left = [RosterRow(name=n, alive=i % 3 != 2, hp=100 - i * 17 if i % 3 != 2 else 0, weapon="AK-47", has_c4=i == 0,
                      has_kit=False, is_pov=i == 1, side="T") for i, n in enumerate(HOSTILE[:6])]
    right = [RosterRow(name=n, alive=True, hp=9, weapon="Unknown Gun", has_c4=False, has_kit=True, is_pov=False,
                       side="CT") for n in HOSTILE[4:]]
    hud.rosters(cv, left, right)
    hud.rosters(cv, left, right)  # second time from the row cache
    bug = BugState(team_left=HOSTILE[2], team_right=HOSTILE[1], side_left="T", side_right="CT", score_left=12,
                   score_right=30, alive_left=4, alive_right=5, total_left=6, total_right=5, clock="1:55",
                   tab="Round 31")
    hud.scorebug(cv, bug)
    hud.scorebug(cv, BugState(**{**bug.__dict__, "bomb": True, "clock": "7", "fuse": 0.2, "defuse": 0.6, "speed": 0.4}))
    hud.scorebug(cv, BugState(**{**bug.__dict__, "tab": "Bomba desarmada", "tab_side": "CT", "since_end": 0.2,
                                 "winner_left": True}))
    for name in HOSTILE:
        plate = PlateInfo(player=name, label="Clutch 1v3", detail="AWP + Desert Eagle  2 HS", kills_total=4)
        hud.nameplate(cv, plate, 1.0)
        hud.kill_counter(cv, plate, 2, 0.1, 5.0, 3.0)
        hud.kill_counter(cv, plate, 4, 0.3, 5.0, 3.0)
        hud.caption(cv, name)
        hud.card(cv, 0.5, 1.5, plate, "Round 7, Nuke", 1, 3)
    hud.nameplate(cv, PlateInfo("", "", "", 0), 1.0)  # nothing to say: nothing drawn, no error
    hud.letterbox(cv, 1.0)
    hud.logo(cv)
    assert ink(surface, brand.colors.background) > w * h * 0.05
    assert len(hud._rows) == len(set(left)) + len(set(right))


# ------------------------------------------------------------------ world
def make_track(pid: str, n: int, team: int, x0: float, y0: float, **over) -> Track:
    t = Track(pid=pid, name=pid, x=np.linspace(x0, x0 + 200, n).astype(np.float32),
              y=np.full(n, y0, dtype=np.float32), z=np.zeros(n, dtype=np.float32),
              yaw=np.linspace(0, 90, n).astype(np.float32), health=np.full(n, 100, dtype=np.int16),
              armor=np.zeros(n, dtype=np.int16), alive=np.ones(n, dtype=bool), present=np.ones(n, dtype=bool),
              team=np.full(n, team, dtype=np.int8), weapon=["AK-47"] * n, defuser=np.zeros(n, dtype=bool),
              flash=np.zeros(n, dtype=np.float32), defusing=np.zeros(n, dtype=bool), has_c4=np.zeros(n, dtype=bool))
    for k, v in over.items():
        setattr(t, k, v)
    return t


def make_round(**over) -> RoundSummary:
    base = dict(number=3, start_tick=0, freeze_end_tick=100, end_tick=5000, official_end_tick=5300, duration_s=70,
                winner_side="CT", winner_team="team_a", reason="bomb_defused", ct_team="team_a", t_team="team_b",
                score_team_a=2, score_team_b=1)
    base.update(over)
    return RoundSummary(**base)


@pytest.mark.parametrize("w, h", SIZES)
def test_world_draws_a_busy_frame_on_an_unknown_map(w, h):
    n = 400
    theme, layout = Theme(Brand(), w, h), choose_layout(w, h)
    hp = np.full(n, 100, dtype=np.int16)
    hp[200:] = 20
    flash = np.zeros(n, dtype=np.float32)
    flash[150:300] = 2.5
    defusing = np.zeros(n, dtype=bool)
    defusing[250:] = True
    dead = np.ones(n, dtype=bool)
    dead[220:] = False
    tracks = {
        "pov": make_track("pov", n, 3, 0, 0, health=hp, defusing=defusing, defuser=np.ones(n, dtype=bool)),
        "t1": make_track("t1", n, 2, 600, 300, flash=flash, has_c4=np.ones(n, dtype=bool)),
        "t2": make_track("t2", n, 2, -400, -500, alive=dead),
        "upstairs": make_track("upstairs", n, 3, 100, 100, z=np.full(n, 900, dtype=np.float32)),
        "ghost": make_track("ghost", n, 0, 0, 0, present=np.zeros(n, dtype=bool)),
    }
    data = SegmentData(1000, 1000 + n - 1, tracks)
    ev = Events(
        smokes=[Area(900, 2100, 300, 200, 0)], fires=[Area(1100, 1500, -200, 100, 0)],
        flashes=[Area(1295, 1323, 500, 300, 0)], hes=[Area(1290, 1324, 100, -300, 0)],
        shots=[Shot(1297, "pov", 150, 0, 0, 30.0), Shot(1299, "t1", 700, 300, 0, 200.0)], shot_ticks=[1297, 1299],
        hurts=[Hurt(1297, "pov", "t1", 40, 740, 300, 0)], hurt_ticks=[1297],
        flights=[Flight("molotov", np.arange(1250, 1320), np.linspace(0, 400, 70).astype(np.float32),
                        np.linspace(0, 200, 70).astype(np.float32), np.zeros(70, dtype=np.float32))],
        bomb_pos={1100: (50.0, 60.0, 0.0)}, drops=[(1010, 5.0, 5.0, 0.0)], pickups=[1050])
    info = maps.MapInfo("de_nowhere", -1500, 1500, 3.0, lower_below_z=500.0)
    rnd = make_round(start_tick=900, plant_tick=1100, defuse_tick=1380, planter=PlayerRef(steamid="t1", name="t1", side="T"),
                     defuser=PlayerRef(steamid="pov", name="pov", side="CT"))
    world = World(theme, layout, ev, "de_nowhere", 64)
    trace = Trace()
    trace.marks.append(Mark(tick=1220, x=400, y=600, lower=False, side="T", ax=500, ay=500, attacker_side="CT", pov=True,
                            number=1, frame=3))
    trace.marks.append(Mark(tick=1230, x=300, y=300, lower=True, side="CT", ax=None, ay=None, attacker_side=None,
                            pov=False, number=0, frame=4))
    left, total = blindness(flash, 64)
    route = np.column_stack([np.linspace(480, 540, n // 8), np.full(n // 8, 500.0)])
    route[10:14] = np.nan  # he was dead / gone for a moment: the line breaks
    lower = np.zeros(n // 8, dtype=bool)
    lower[30:] = True
    sc = Scene(seg=ClipSegment(1000, 1000 + n - 1, "pov"), data=data, rnd=rnd, info=info, tickrate=64, trace=trace,
               pov=tracks["pov"], flights=ev.flights, flash_left={"t1": left}, flash_total={"t1": total},
               busy_since={"pov": run_starts(defusing)}, route_xy=route, route_lower=lower)
    surface = skia.Surface(w, h)
    cv = surface.getCanvas()
    view = np.array([512.0, 512.0, 1.4])
    seen = set()
    for i, tick in enumerate((1001.0, 1080.5, 1150.0, 1299.5, 1310.0, 1399.0)):
        world.draw(cv, sc, tick, view, layout.focus, lower_mix=0.5 if i == 2 else float(i > 3), clip_frame=i * 4,
                   fps=30, slow=0.5 * (i % 2))
        assert ink(surface, theme.c.background) > 500
        seen.add(surface.toarray().tobytes()[:: 997])
    assert len(seen) == 6  # every frame is different
    assert len(trace.routes) >= 3 and not trace.empty  # broken where he was gone, split where he changed floor
    assert sc.sample(tracks["ghost"], 1100) is None and sc.sample(tracks["pov"], 5000)[4] == n - 1
    # an empty world still draws its ground
    empty = Scene(seg=ClipSegment(1000, 1010), data=SegmentData(1000, 1010, {}), rnd=None, info=info, tickrate=64,
                  trace=Trace(), pov=None, flights=[], flash_left={}, flash_total={}, busy_since={},
                  route_xy=np.zeros((0, 2)), route_lower=np.zeros(0, dtype=bool))
    World(theme, layout, Events(), "de_nowhere", 64).draw(cv, empty, 1005.0, view, layout.focus_full, 0.0, 0, 30)


# ------------------------------------------------------------------ renderer contract
def test_renderer_contract(tmp_path, sample_analysis):
    r = RadarRenderer()
    assert r.name == "radar" and "9:16" in r.description
    assert r.availability().ok
    job = RenderJob(demo_path=tmp_path / "gone.dem", analysis=sample_analysis, clips=[], out_path=tmp_path / "o.mp4",
                    work_dir=tmp_path)
    with pytest.raises(RenderError):
        r.render(job)


@pytest.mark.demo
@pytest.mark.parametrize("size, camera, pov", [((426, 240), "follow", True), ((240, 426), "follow", True),
                                               ((240, 240), "full", True), ((320, 180), "follow", False)])
def test_real_demo_renders_every_layout(tmp_path, size, camera, pov):
    demos = real_demos()
    if not demos:
        pytest.skip("no demo in demos/")
    cache = Path(tmp_path).parent / f"radar-analysis-{demos[0].stem}.json"
    if cache.exists():
        analysis = MatchAnalysis.model_validate_json(cache.read_text(encoding="utf-8"))
    else:
        from demoreel.analysis import analyze_demo

        analysis = analyze_demo(demos[0])
        cache.write_text(analysis.model_dump_json(), encoding="utf-8")
    h = next(x for x in analysis.highlights if x.kill_ticks)
    kill = h.kill_ticks[0]
    who = h.player.steamid if pov else None
    clips = [Clip(segments=[ClipSegment(kill - 48, kill + 16, who), ClipSegment(kill + 16, kill + 40, who, 0.5, "olha")],
                  title=h.title),
             Clip(segments=[ClipSegment(kill - 20, kill + 20, who)], title="again")]
    seen: list[float] = []
    job = RenderJob(demo_path=demos[0], analysis=analysis, clips=clips, out_path=tmp_path / "out.mp4", work_dir=tmp_path,
                    options=RenderOptions(width=size[0], height=size[1], fps=15, extra={"camera": camera, "zoom": 3}),
                    progress=lambda f, m: seen.append(f), thumbnail_path=tmp_path / "thumb.jpg")
    res = RadarRenderer().render(job)
    assert (res.width, res.height, res.fps) == (size[0] - size[0] % 2, size[1], 15)
    frames = res.details["frames"]
    assert res.duration_s == pytest.approx(frames / 15)
    body = 15 + round(24 / 64 / 0.5 * 15) + round(40 / 64 * 15)  # the three segments
    assert abs(frames - (body + 2 * int(1.5 * 15))) <= 3  # plus one title card per clip
    assert job.out_path.stat().st_size > 2000 and job.thumbnail_path.stat().st_size > 500
    assert seen and all(0 <= f <= 1 for f in seen) and seen == sorted(seen)


@pytest.mark.demo
def test_real_demo_render_can_be_cancelled(tmp_path):
    demos = real_demos()
    if not demos:
        pytest.skip("no demo in demos/")
    from demoreel.analysis import analyze_demo
    from demoreel.render.base import RenderCancelled

    cache = Path(tmp_path).parent / f"radar-analysis-{demos[0].stem}.json"
    analysis = (MatchAnalysis.model_validate_json(cache.read_text(encoding="utf-8")) if cache.exists()
                else analyze_demo(demos[0]))
    h = analysis.highlights[0]
    seg = h.segments[0]
    job = RenderJob(demo_path=demos[0], analysis=analysis, out_path=tmp_path / "out.mp4", work_dir=tmp_path,
                    clips=[Clip(segments=[ClipSegment(seg.start_tick, seg.start_tick + 400, seg.pov_steamid)])],
                    options=RenderOptions(width=320, height=180, fps=30), cancelled=lambda: True)
    with pytest.raises(RenderCancelled):
        RadarRenderer().render(job)
