"""Entry point: .dem file -> :class:`MatchAnalysis`."""

from __future__ import annotations

from pathlib import Path

from . import chat, fails, highlights, stats
from .parser import parse_demo
from .schema import MatchAnalysis


def analyze_demo(path: str | Path, highlight_config: highlights.HighlightConfig | None = None) -> MatchAnalysis:
    demo = parse_demo(path)
    if not demo.rounds:
        raise ValueError("No completed rounds found — is this a valid CS2 match demo?")
    team_a, team_b, players, rounds = stats.compute(demo)
    found = highlights.detect(rounds, demo.tickrate, demo.total_ticks, demo.map_name, highlight_config)
    return MatchAnalysis(
        map=demo.map_name,
        server_name=demo.header.get("server_name"),
        demo_source=demo.header.get("client_name"),
        tickrate=demo.tickrate,
        total_ticks=demo.total_ticks,
        duration_s=round(demo.total_ticks / demo.tickrate, 1),
        rounds_played=len(rounds),
        rounds_in_match=(team_a.score + team_b.score) or None,
        team_a=team_a,
        team_b=team_b,
        players=players,
        rounds=rounds,
        highlights=found,
        fails=fails.detect(demo, rounds),
        chat=chat.annotate(demo.chat, rounds, demo.tickrate),
        warnings=demo.warnings,
    )
