"""The shared kill feed (render/killfeed.py): the row model, the animation window and the drawing."""

from __future__ import annotations

import numpy as np
import pytest
import skia

from demoreel.analysis.schema import KillEvent, PlayerRef
from demoreel.brand import Brand
from demoreel.render import icons, killfeed
from demoreel.render.killfeed import (
    ROW_H,
    Type,
    clean_name,
    draw_killfeed,
    row_emphasis,
    row_parts,
    visible_kills,
)


def ref(pid: str, side: str | None = "CT", name: str | None = None) -> PlayerRef:
    return PlayerRef(steamid=pid, name=name or pid.upper(), side=side)


def make_kill(tick: int = 1000, attacker: str | None = "a", victim: str = "v", **kw) -> KillEvent:
    base = dict(tick=tick, round=1, clock_s=10.0, attacker=ref(attacker, "CT") if attacker else None,
                victim=ref(victim, "T"), weapon="ak47", weapon_name="AK-47")
    base.update(kw)
    return KillEvent(**base)


def kinds(kill: KillEvent) -> list[str]:
    return [p.value if p.kind != "name" else f"name:{p.value}" for p in row_parts(kill)]


def painted(surface: skia.Surface) -> np.ndarray:
    """Boolean mask of the pixels that were drawn on a transparent surface."""
    return surface.toarray(colorType=skia.kRGBA_8888_ColorType)[..., 3] > 0


# ------------------------------------------------------------------ row model
def test_row_is_in_the_games_order():
    k = make_kill(weapon="awp", weapon_name="AWP", attacker_blind=True, attacker_airborne=True, noscope=True,
                  through_smoke=True, wallbang=True, headshot=True, flash_assist=True, assister=ref("s", "CT"))
    assert kinds(k) == ["blind", "name:A", "+", "flashbang_assist", "name:S", "airborne", "awp", "noscope", "smoke",
                        "wallbang", "headshot", "name:V"]


def test_plain_kill_is_attacker_weapon_victim_with_side_colours():
    parts = row_parts(make_kill())
    assert [(p.kind, p.value, p.side) for p in parts] == [("name", "A", "CT"), ("icon", "ak47", None),
                                                          ("name", "V", "T")]
    assert parts[1].label == "AK-47"  # what is written when the icon is missing


def test_noscope_only_counts_for_snipers():
    assert "noscope" not in kinds(make_kill(noscope=True))  # a rifle is always "unscoped"
    assert "noscope" in kinds(make_kill(weapon="ssg08", weapon_name="SSG 08", noscope=True))


def test_assist_without_flash_and_self_assist():
    assert kinds(make_kill(assister=ref("s"))) == ["name:A", "+", "name:S", "ak47", "name:V"]
    assert kinds(make_kill(assister=ref("a"))) == ["name:A", "ak47", "name:V"]


def test_deaths_without_a_killer():
    fall = make_kill(attacker=None, weapon="world", weapon_name="World", is_suicide=True)
    assert kinds(fall) == ["suicide", "name:V"]
    own_nade = KillEvent(tick=5, round=1, clock_s=1, attacker=ref("v", "T"), victim=ref("v", "T"), weapon="hegrenade",
                         weapon_name="HE Grenade", is_suicide=True)
    assert kinds(own_nade) == ["hegrenade", "name:V"]
    bomb = make_kill(attacker=None, weapon="planted_c4", weapon_name="C4")
    assert kinds(bomb) == ["planted_c4", "name:V"]


def test_knife_variants_share_one_icon():
    assert row_parts(make_kill(weapon="knife_karambit", weapon_name="Knife"))[1].value == "knife"


def test_emphasis_follows_the_watched_player():
    k = make_kill(assister=ref("s"))
    assert row_emphasis(k, None) is None and row_emphasis(k, "x") is None
    assert row_emphasis(k, "a") == "kill" and row_emphasis(k, "s") == "kill"
    assert row_emphasis(k, "v") == "death"


@pytest.mark.parametrize("raw, expected", [
    ("  Juca\tFaisca \n", "Juca Faisca"), ("a\x00b\x07c", "a b c"), ("‮evil‬", "evil"), ("", "?"),
    (None, "?"), ("   ", "?"), ("♿", "♿"), ("Ca$tigo", "Ca$tigo"), ("x y", "x y"),
])
def test_clean_name(raw, expected):
    assert clean_name(raw) == expected


# ------------------------------------------------------------------ which rows, and their animation
def test_visible_rows_window_order_and_limit():
    kills = [make_kill(tick=64 * i, victim=f"v{i}") for i in range(1, 9)]  # one per second, seconds 1..8
    rows = visible_kills(list(reversed(kills)), tick=64 * 8.5, tickrate=64, max_rows=3, hold_s=6.0)
    assert [k.victim.steamid for k, _ in rows] == ["v6", "v7", "v8"]  # newest last, capped
    rows = visible_kills(kills, tick=64 * 8.5, tickrate=64, max_rows=10, hold_s=6.0)
    assert [k.victim.steamid for k, _ in rows] == ["v3", "v4", "v5", "v6", "v7", "v8"]  # older than 6 s: gone
    assert visible_kills(kills, tick=10, tickrate=64) == []  # nothing happened yet
    assert visible_kills(kills, tick=64 * 100, tickrate=64) == []
    assert visible_kills(kills, tick=64 * 3, tickrate=0) == []


def test_row_enters_holds_and_leaves():
    k = make_kill(tick=1000)

    def state(age_s: float):
        return visible_kills([k], 1000 + age_s * 64, 64, hold_s=6.0)[0][1]

    born, mid, dying = state(0.0), state(3.0), state(5.9)
    assert born.alpha == 0 and born.slide == 1 and born.height == 0
    assert mid.alpha == 1 and mid.slide == 0 and mid.height == 1
    assert 0 < dying.alpha < 1 and 0 < dying.height < 1
    alphas = [state(t).alpha for t in np.linspace(0, 0.3, 12)]
    assert alphas == sorted(alphas)  # the enter never flickers


# ------------------------------------------------------------------ drawing
@pytest.fixture
def no_icons(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMOREEL_ICONS_DIR", str(tmp_path / "none"))
    monkeypatch.setattr(icons, "_extracted", True)  # do not fill the folder from a CS2 install on this machine
    monkeypatch.setattr(killfeed, "_icons_checked", (-1e9, False))
    icons.icon.cache_clear()
    yield
    icons.icon.cache_clear()


@pytest.fixture
def fake_icons(tmp_path, monkeypatch):
    d = tmp_path / "icons"
    d.mkdir()
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="32" viewBox="0 0 {w} 32"><rect width="{w}" height="32" fill="#fff"/></svg>'
    (d / "ak47.svg").write_text(svg.format(w=96), encoding="utf-8")
    (d / "icon_headshot.svg").write_text(svg.format(w=32), encoding="utf-8")
    monkeypatch.setenv("DEMOREEL_ICONS_DIR", str(d))
    monkeypatch.setattr(killfeed, "_icons_checked", (-1e9, False))
    icons.icon.cache_clear()
    yield
    icons.icon.cache_clear()


def draw(kills, tick, *, w=640, h=240, **kw):
    surface = skia.Surface(w, h)
    surface.getCanvas().clear(skia.ColorTRANSPARENT)
    opts = dict(brand=Brand(), scale=1.0, x=w - 10.0, y=10.0)
    opts.update(kw)
    height = draw_killfeed(surface.getCanvas(), kills, tick, 64, **opts)
    return height, painted(surface)


def test_draws_text_rows_without_icons(no_icons):
    assert not icons.available()
    kills = [make_kill(tick=1000, headshot=True), make_kill(tick=1100, victim="w", through_smoke=True)]
    height, mask = draw(kills, 1200)
    assert height == pytest.approx(2 * ROW_H + 4)
    cols = np.nonzero(mask.any(axis=0))[0]
    rows = np.nonzero(mask.any(axis=1))[0]
    assert cols.max() <= 640 - 10 + 2 and cols.min() > 300  # hangs on the right edge given
    assert 8 <= rows.min() <= 10 and rows.max() <= 10 + height + 3


def test_left_alignment_starts_at_x(no_icons):
    _, mask = draw([make_kill(tick=1000)], 1200, x=20.0, align="left")
    cols = np.nonzero(mask.any(axis=0))[0]
    assert 17 <= cols.min() <= 20 and cols.max() < 300


def test_draws_with_icons_and_marks_the_watched_player(fake_icons):
    assert icons.available()
    k = make_kill(tick=1000, headshot=True)
    _, plain = draw([k], 1200)
    _, mine = draw([k], 1200, pov_steamid="a")
    assert plain.sum() > 0 and mine.sum() > 0
    surface = skia.Surface(640, 60)
    surface.getCanvas().clear(skia.ColorTRANSPARENT)
    draw_killfeed(surface.getCanvas(), [k], 1200, 64, brand=Brand(), scale=1.0, x=630, y=10, pov_steamid="a")
    px = surface.toarray(colorType=skia.kRGBA_8888_ColorType)
    cols = np.nonzero((px[..., 3] > 0).any(axis=0))[0]
    r, g, b, _ = (int(v) for v in px[10 + 13, cols.min() + 1])  # the outline, at mid height of the row's left edge
    assert r > 150 and r > 2 * g and r > 2 * b  # red, like the local player's kills in the game


def test_nothing_to_draw_returns_zero(no_icons):
    assert draw([], 1000)[0] == 0
    assert draw([make_kill(tick=1000)], 500)[0] == 0  # the kill has not happened yet
    assert draw([make_kill(tick=1000)], 1200, max_rows=0)[0] == 0
    assert draw([make_kill(tick=1000)], 1200, opacity=0.0)[0] == 0


def test_stack_is_capped_and_scales(no_icons):
    kills = [make_kill(tick=1000 + i, victim=f"v{i}") for i in range(9)]
    h1, _ = draw(kills, 1300, max_rows=5)
    h2, _ = draw(kills, 1300, max_rows=5, scale=2.0, w=1280, h=480, x=1270.0)
    assert h1 == pytest.approx(5 * ROW_H + 4 * 4)
    assert h2 == pytest.approx(2 * h1)


@pytest.mark.parametrize("name", [
    "😀🔥💀👑", "♿", "W" * 300, "ｆｕｌｌｗｉｄｔｈ", "‮‭", "a\nb\tc", "ﷺ﷽", "<svg onload=x>", "",
    "👨‍👩‍👧‍👦 family", "한국어 이름", "عربى", "$$$ 100% {} %s \\n",
])
def test_hostile_names_never_break_or_overflow(no_icons, name):
    k = KillEvent(tick=1000, round=1, clock_s=1, attacker=ref("a", "CT", name), victim=ref("v", "T", name),
                  assister=ref("s", "CT", name), weapon="ak47", weapon_name="AK-47")
    height, mask = draw([k], 1200, w=900, x=890.0)
    assert height == pytest.approx(ROW_H)
    cols = np.nonzero(mask.any(axis=0))[0]
    assert cols.max() <= 892 and cols.min() >= 0
    assert cols.max() - cols.min() < 3 * 170 + 200  # every name is cut at max_name_width


def test_type_fit_and_fallback_fonts():
    ty = Type()
    assert ty.fit("short", "name", 18, 500) == "short"
    cut = ty.fit("a very long nickname that cannot fit", "name", 18, 80)
    assert cut.endswith("…") and ty.measure(cut, "name", 18) <= 80
    assert ty.fit("x" * 50, "name", 18, 1) == "…"
    runs, width = ty.runs("ab♿cd", "name", 18)
    assert "".join(t for t, _, _ in runs) == "ab♿cd" and width > 0
    assert ty.measure("", "name", 18) == 0
