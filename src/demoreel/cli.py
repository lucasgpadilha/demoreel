"""Command line interface: ``demoreel --help``."""

from __future__ import annotations

import json
import logging
import multiprocessing
import sys
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(no_args_is_help=True, add_completion=False,
                  help="CS2 demo analysis and automatic highlight videos (REST API + MCP server).")


def _utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # player names are full of unicode
        except (AttributeError, ValueError):
            pass


def _capabilities(extra: list[str] | None = None) -> list[str]:
    from .config import get_settings

    caps = ["cpu"]
    if get_settings().ingame_enabled:
        caps.append("ingame")
    for c in extra or []:
        if c not in caps:
            caps.append(c)
    return caps


@app.command()
def serve(
    host: Annotated[str | None, typer.Option(help="Bind address (default: DEMOREEL_HOST or 127.0.0.1).")] = None,
    port: Annotated[int | None, typer.Option(help="Port (default: DEMOREEL_PORT or 8000).")] = None,
    workers: Annotated[int | None, typer.Option(help="Worker processes to start alongside the API. 0 = API only.")] = None,
) -> None:
    """Start the API, the web UI, the MCP endpoint and (by default) one background worker."""
    import os

    import uvicorn

    if host:
        os.environ["DEMOREEL_HOST"] = host
    if port:
        os.environ["DEMOREEL_PORT"] = str(port)
    from .config import get_settings

    settings = get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from .db import init_db
    from .jobs import requeue_stale, worker_process_main

    init_db()
    requeue_stale()
    n = settings.workers if workers is None else workers
    procs = []
    for _ in range(max(0, n)):
        p = multiprocessing.get_context("spawn").Process(target=worker_process_main, args=(_capabilities(),), daemon=True)
        p.start()
        procs.append(p)
    typer.echo(f"demoreel  ->  {settings.base_url}   (docs: /docs · MCP: /mcp · workers: {len(procs)})")
    try:
        uvicorn.run("demoreel.api:create_app", factory=True, host=settings.host, port=settings.port, log_level="info")
    finally:
        for p in procs:
            p.terminate()


@app.command()
def worker(
    capability: Annotated[list[str] | None, typer.Option(
        "--capability", "-c", help="Extra capability, e.g. `-c ingame` on a machine with CS2.")] = None,
) -> None:
    """Run a standalone worker (use it to scale out, or on a gaming PC for in-game recording)."""
    from .jobs import worker_process_main

    worker_process_main(_capabilities(capability))


@app.command()
def analyze(
    demo: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="Path to a .dem file.")],
    json_out: Annotated[Path | None, typer.Option("--json", help="Write the full analysis JSON here.")] = None,
    top: Annotated[int, typer.Option(help="How many highlights to list.")] = 10,
) -> None:
    """Analyze a demo locally and print the scoreboard and highlights (no server needed)."""
    _utf8()
    from .analysis import analyze_demo

    a = analyze_demo(demo)
    if json_out:
        json_out.write_text(a.model_dump_json(indent=2), encoding="utf-8")
    typer.echo(f"{a.map} · {a.rounds_played} rounds · {a.duration_s / 60:.0f} min")
    typer.echo(f"{a.team_a.name} {a.team_a.score} : {a.team_b.score} {a.team_b.name}\n")
    typer.echo(f"{'player':24} {'K':>3} {'D':>3} {'A':>3} {'ADR':>6} {'HS%':>5} {'KAST':>5} {'RTG':>5}")
    for t in (a.team_a, a.team_b):
        typer.echo(f"— {t.name}")
        for p in (x for x in a.players if x.team == t.key):
            typer.echo(f"{p.name[:24]:24} {p.kills:3} {p.deaths:3} {p.assists:3} {p.adr:6.1f} {p.headshot_pct:5.1f} "
                       f"{p.kast:5.1f} {p.rating:5.2f}")
    typer.echo(f"\nTop highlights ({len(a.highlights)} found):")
    for h in a.highlights[:top]:
        typer.echo(f"  {h.score:6.1f}  R{h.round:<2} {h.duration_s:5.1f}s  {h.title}")
    for w in a.warnings:
        typer.echo(f"warning: {w}", err=True)


@app.command()
def render(
    demo: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    out: Annotated[Path, typer.Option("--out", "-o", help="Output .mp4")] = Path("highlights.mp4"),
    top: Annotated[int, typer.Option(help="Render the N best highlights as one reel.")] = 3,
    renderer: Annotated[str, typer.Option(help="radar | ingame")] = "radar",
    only: Annotated[int | None, typer.Option(
        help="Render just the Nth best highlight (1 = best) instead of a reel. See the ranking with `analyze`.")] = None,
    width: int = 1280, height: int = 720, fps: int = 30,
) -> None:
    """Analyze a demo and render its best highlights straight to a file (no server needed)."""
    _utf8()
    import shutil
    import tempfile

    from .analysis import analyze_demo
    from .render import Clip, ClipSegment, RenderError, RenderJob, RenderOptions, get_renderer

    a = analyze_demo(demo)
    if only is not None:
        if not 1 <= only <= len(a.highlights):
            typer.echo(f"--only must be between 1 and {len(a.highlights)} for this demo.", err=True)
            raise typer.Exit(1)
        picks = [a.highlights[only - 1]]
    else:
        picks = sorted(a.highlights[:top], key=lambda h: h.start_tick)
    if not picks:
        typer.echo("No highlights found in this demo.", err=True)
        raise typer.Exit(1)
    for h in picks:
        typer.echo(f"  R{h.round:<2} {h.duration_s:5.1f}s  {h.title}")
    clips = [Clip(segments=[ClipSegment(s.start_tick, s.end_tick, s.pov_steamid, s.speed, s.caption) for s in h.segments],
                  title=h.title, subtitle=h.description) for h in picks]
    out = out.resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    # Scratch space next to the output: same drive as the result, and never inside a virtualised temp folder.
    work = Path(tempfile.mkdtemp(prefix=".demoreel-work-", dir=out.parent))
    try:
        job = RenderJob(demo_path=demo.resolve(), analysis=a, clips=clips, out_path=out, work_dir=work,
                        options=RenderOptions(width=width, height=height, fps=fps),
                        thumbnail_path=out.with_suffix(".jpg"),
                        progress=lambda f, m: typer.echo(f"\r{f * 100:5.1f}%  {m}   ", nl=False))
        res = get_renderer(renderer).render(job)
    except RenderError as exc:
        typer.echo(f"\nRender failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    finally:
        shutil.rmtree(work, ignore_errors=True)
    typer.echo(f"\n{out}  ({res.duration_s:.1f}s, {res.width}x{res.height}@{res.fps})")
    for w in res.details.get("warnings") or []:
        typer.echo(f"warning: {w}", err=True)


@app.command()
def rescore(match_id: str = typer.Argument(None, help="Match id; omit to re-score every analyzed match.")) -> None:
    """Re-run highlight scoring and curation on analyzed matches (demos are not read again)."""
    _utf8()
    from . import service
    from .db import Match, init_db, session_scope

    init_db()
    with session_scope() as s:
        ids = [match_id] if match_id else [m.id for m in s.query(Match).filter(Match.status == "ready").all()]
    for mid in ids:
        try:
            with session_scope() as s:
                out = service.rescore_match(s, mid)
            typer.echo(f"{mid}: {out['highlights']} highlights, {out['featured']} featured "
                       f"(+{out['added']} / -{out['removed']})")
        except service.ServiceError as exc:
            typer.echo(f"{mid}: skipped ({exc.message})")


@app.command()
def publish(
    target: Annotated[str | None, typer.Option(help="Mirror as '[user@]host:/absolute/path/to/data' "
                                                    "(default: DEMOREEL_PUBLISH_TARGET).")] = None,
    restart: Annotated[str | None, typer.Option(help="Command run on the mirror after the database is replaced "
                                                     "(default: DEMOREEL_PUBLISH_RESTART).")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Only say what would be sent and removed.")] = False,
    prune: Annotated[bool, typer.Option(help="Remove videos from the mirror that no longer exist here.")] = True,
) -> None:
    """Copy the database, analyses, finished videos and brand kits to a read-only mirror over SSH."""
    _utf8()
    from . import publish as pub

    try:
        plan = pub.publish(target, restart=restart, dry_run=dry_run, prune=prune, progress=typer.echo)
    except pub.PublishError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from e
    verb = "would send" if plan.dry_run else "sent"
    typer.echo(f"{verb} {plan.sent} files ({plan.sent_bytes / 1e9:.2f} GB), "
               f"{'would remove' if plan.dry_run else 'removed'} {plan.removed}; the mirror holds "
               f"{plan.matches} matches and {plan.renders} videos.")
    if plan.missing:
        typer.secho(f"{len(plan.missing)} file(s) listed by the database are not on this disk, e.g. "
                    f"{plan.missing[0]}. Those videos will not play on the mirror until they are made again.",
                    fg=typer.colors.YELLOW, err=True)


@app.command()
def mcp() -> None:
    """Run the MCP server over stdio (for Claude Desktop / Claude Code and other local MCP clients)."""
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    from .mcp_server import build_server

    build_server().run("stdio")


@app.command()
def doctor() -> None:
    """Check this machine: ffmpeg, renderers, data directory."""
    _utf8()
    from .config import get_settings
    from .render import describe_renderers

    s = get_settings()
    typer.echo(f"data dir      {s.data_dir.resolve()}")
    typer.echo(f"database      {s.db_url}")
    typer.echo(f"default       renderer={s.default_renderer} auto_render_top={s.auto_render_top} ingame_enabled={s.ingame_enabled}")
    for r in describe_renderers():
        mark = "OK " if r["available"] else "NO "
        typer.echo(f"renderer      {mark}{r['name']:7} {r['reason'] or json.dumps(r['details'], default=str)}")


if __name__ == "__main__":
    app()
