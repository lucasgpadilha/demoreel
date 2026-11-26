"""Test setup: every test session gets its own throwaway data directory and database."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="demoreel-test-")
os.environ["DEMOREEL_DATA_DIR"] = _TMP
os.environ["DEMOREEL_OFFLINE"] = "1"  # never download radar images from tests
os.environ["DEMOREEL_AUTO_RENDER_TOP"] = "0"
os.environ["DEMOREEL_ICONS_FROM_GAME"] = "0"  # never read a CS2 install from tests
os.environ["DEMOREEL_ALLOWED_HOSTS"] = '["testserver"]'  # the host name FastAPI's TestClient sends
os.environ.pop("DEMOREEL_API_KEY", None)

import pytest  # noqa: E402

from demoreel.analysis import highlights, stats  # noqa: E402
from demoreel.config import Settings  # noqa: E402

# the developer's own .env (publish target, renderers kept on the mirror…) must not change what the tests see
Settings.model_config["env_file"] = None
from demoreel.analysis.parser import ParsedDemo, RoundInfo  # noqa: E402
from demoreel.analysis.schema import MatchAnalysis  # noqa: E402

CT = ["ct1", "ct2", "ct3", "ct4", "ct5"]
T = ["t1", "t2", "t3", "t4", "t5"]
TICKRATE = 64
ROUND_TICKS = 6000


def kill(tick: int, rnd: int, attacker: str | None, victim: str, **flags) -> dict:
    sides = {**{p: "CT" for p in CT}, **{p: "T" for p in T}}
    base = {
        "tick": tick, "round": rnd, "attacker": attacker, "attacker_side": sides.get(attacker),
        "victim": victim, "victim_side": sides[victim], "assister": None, "assister_side": None,
        "weapon": "ak47", "headshot": False, "wallbang": False, "noscope": False, "through_smoke": False,
        "attacker_blind": False, "attacker_airborne": False, "flash_assist": False, "distance_m": 12.0,
        "is_suicide": attacker is None or attacker == victim,
        "is_teamkill": attacker is not None and attacker != victim and sides.get(attacker) == sides[victim],
        "after_round_end": False, "attacker_place": None, "victim_place": None,
        "attacker_pos": [0, 0, 0], "victim_pos": [0, 0, 0],
    }
    base.update(flags)
    if base["assister"]:
        base["assister_side"] = sides[base["assister"]]
    return base


def make_demo(rounds: list[tuple[str, list[dict]]], damages: list[dict] | None = None) -> ParsedDemo:
    """rounds = [(winner_side, [kill dicts]), ...]; ticks of round n live in [(n-1)*6000, n*6000)."""
    infos = []
    all_kills = []
    for i, (winner, kills) in enumerate(rounds):
        base = i * ROUND_TICKS
        infos.append(RoundInfo(
            number=i + 1, start_tick=base, freeze_end_tick=base + 640, end_tick=base + 5000,
            official_end_tick=base + 5400, winner_side=winner, reason="t_killed" if winner == "CT" else "ct_killed",
            sides={**{p: "CT" for p in CT}, **{p: "T" for p in T}},
            equip={**{p: 4500 for p in CT}, **{p: 4500 for p in T}},
        ))
        all_kills.extend(kills)
    return ParsedDemo(
        path=Path("synthetic.dem"), header={"map_name": "de_test"}, map_name="de_test", tickrate=TICKRATE,
        total_ticks=len(rounds) * ROUND_TICKS, rounds=infos,
        names={p: p.upper() for p in CT + T}, kills=sorted(all_kills, key=lambda k: k["tick"]),
        damages=damages or [], blinds=[], grenades=[], shots=[], chat=[], final_props={},
    )


def analyze(demo: ParsedDemo) -> MatchAnalysis:
    team_a, team_b, players, rounds = stats.compute(demo)
    found = highlights.detect(rounds, demo.tickrate, demo.total_ticks, demo.map_name)
    return MatchAnalysis(map=demo.map_name, tickrate=demo.tickrate, total_ticks=demo.total_ticks,
                         duration_s=demo.total_ticks / demo.tickrate, rounds_played=len(rounds), team_a=team_a,
                         team_b=team_b, players=players, rounds=rounds, highlights=found)


def ace_round(rnd: int = 1) -> tuple[str, list[dict]]:
    """ct1 kills the whole T side in ~5 seconds."""
    base = (rnd - 1) * ROUND_TICKS + 2000
    return "CT", [kill(base + i * 80, rnd, "ct1", t, headshot=i % 2 == 0) for i, t in enumerate(T)]


@pytest.fixture
def sample_demo() -> ParsedDemo:
    r2_base = ROUND_TICKS + 2000
    round2 = ("T", [
        kill(r2_base, 2, "t1", "ct1"),            # opening kill by t1
        kill(r2_base + 100, 2, "ct2", "t1"),      # ct2 trades ct1 within 5s
        kill(r2_base + 500, 2, "t2", "ct2"),      # > 5s after ct2's kill: not a trade
        kill(r2_base + 600, 2, "t2", "ct3"),
        kill(r2_base + 700, 2, "t2", "ct4"),      # ct5 is now alone: 1v4
        kill(r2_base + 2500, 2, "t3", "ct5"),
    ])
    return make_demo([ace_round(1), round2])


@pytest.fixture
def sample_analysis(sample_demo) -> MatchAnalysis:
    return analyze(sample_demo)


def real_demos() -> list[Path]:
    root = Path(__file__).resolve().parent.parent / "demos"
    return sorted(root.glob("*.dem")) if root.is_dir() else []
