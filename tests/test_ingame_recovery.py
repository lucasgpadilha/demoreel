"""A recording that is killed halfway, and the CS2 folder of someone who also uses CS Demo Manager. Everything
happens in a fake CS2 folder under tmp_path; the "game" is never started, and a killed demoreel is a child Python
process that leaves with os._exit (so none of its `finally` blocks run)."""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from test_ingame_plugin import DLL, GAMEINFO, PLUGIN_SETTINGS, RECORDED_SETTINGS, make_settings
from test_ingame_renderer import STEAM_A, FakeGame, add_plugin, env, make_job, renderer_for  # noqa: F401

from demoreel.render.base import Clip, ClipSegment
from demoreel.render.ingame import encode, launcher, plugin, procs
from demoreel.render.ingame.settings import IngameSettings

THEIR_DLL = b"MZ" + b"\x01" * plugin.MIN_LIBRARY_BYTES  # CS Demo Manager's copy, not the same bytes as ours


def cs_demo_manager_user(tmp_path: Path) -> IngameSettings:
    """A CS2 folder where CS Demo Manager is used too: its plugin stays in csdm/bin, it keeps the raw files of
    one of its own videos, its plugin settings and its last log are there, and the user has an empty folder
    in csgo/movie."""
    settings = make_settings(tmp_path)
    (settings.plugin_dir / "server.dll").write_bytes(DLL)
    root = settings.plugin_install_dir
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "server.dll").write_bytes(THEIR_DLL)
    (root / "ServerConfig.vdf").write_text("theirs")
    (root / "movie" / "csdm-video").mkdir(parents=True)
    (root / "movie" / "csdm-video" / "1-sequence00000001.tga").write_bytes(b"their frame")
    (encode.wav_recording_dir(settings) / "my-folder").mkdir(parents=True)
    path = plugin.plugin_settings_path(settings)
    path.parent.mkdir()
    path.write_bytes(PLUGIN_SETTINGS)
    (settings.bin_dir / "csdm.log").write_text("their last session")
    return settings


def snapshot(root: Path) -> dict[str, bytes | None]:
    return {p.relative_to(root).as_posix(): (p.read_bytes() if p.is_file() else None)
            for p in sorted(root.rglob("*"))}


# --- the folder of a CS Demo Manager user ---------------------------------------------------------------------


def test_a_recording_leaves_a_cs_demo_manager_users_files_as_it_found_them(tmp_path):
    settings = cs_demo_manager_user(tmp_path)
    before = snapshot(settings.cs2_dir)
    binary = plugin.select_plugin_binary(settings.plugin_dir, "win32", None)
    with pytest.raises(RuntimeError):
        with plugin.recording_session(settings) as scope:
            with plugin.plugin_session(settings, binary):
                assert (plugin.plugin_bin_dir(settings) / "server.dll").read_bytes() == DLL  # ours, in place
                take = encode.tga_recording_dir(settings) / "2026_11_26"
                take.mkdir()
                (take / "1-sequence00000001.jpg").write_bytes(b"our frame")
                (encode.wav_recording_dir(settings) / "2026_11_26").mkdir()
                (encode.wav_recording_dir(settings) / "2026_11_26" / "1-sequence.wav").write_bytes(b"x" * 64)
                (settings.bin_dir / "csdm.log").write_text("[100] Executing: startmovie 1-sequence")
            # our take is found, theirs (same name, older folder) is not
            assert encode.find_raw_take(settings, "1-sequence", scope).tga_dir == take
            assert encode.frame_names(settings, scope) == ["1-sequence00000001.jpg"]
            raise RuntimeError("encoding failed")

    assert snapshot(settings.cs2_dir) == before
    assert (settings.data_dir / plugin.LAST_LOG_NAME).read_text() == "[100] Executing: startmovie 1-sequence"


def test_a_successful_recording_deletes_its_log_and_its_journal(tmp_path):
    settings = make_settings(tmp_path)
    (settings.plugin_dir / "server.dll").write_bytes(DLL)
    binary = plugin.select_plugin_binary(settings.plugin_dir, "win32", None)
    with plugin.recording_session(settings):
        assert plugin.journal_path(settings).is_file()
        with plugin.plugin_session(settings, binary):
            (settings.bin_dir / "csdm.log").write_text("ours")
    assert sorted(snapshot(settings.cs2_dir)) == ["game", "game/bin", "game/bin/win64", "game/csgo",
                                                  "game/csgo/gameinfo.gi"]
    assert not (settings.data_dir / plugin.LAST_LOG_NAME).exists()


# --- demoreel killed in the middle of a recording ------------------------------------------------------------

KILLED = textwrap.dedent("""
    import os, sys
    from pathlib import Path
    from demoreel.render.ingame import encode, plugin
    from demoreel.render.ingame.settings import IngameSettings

    cs2, plugins, data = map(Path, sys.argv[1:4])
    settings = IngameSettings(cs2_dir=cs2, steam_dir=None, steam_exe=None, plugin_dir=plugins, data_dir=data,
                              platform="win32")
    binary = plugin.select_plugin_binary(plugins, "win32", None)
    with plugin.recording_session(settings):
        with plugin.plugin_session(settings, binary):
            take = encode.tga_recording_dir(settings) / "2026_11_26"
            take.mkdir(parents=True)
            for i in range(1, 4):
                (take / f"1-sequence{i:08d}.jpg").write_bytes(b"frame")
            (encode.wav_recording_dir(settings) / "2026_11_26").mkdir(parents=True, exist_ok=True)
            (encode.wav_recording_dir(settings) / "2026_11_26" / "1-sequence.wav").write_bytes(b"x" * 64)
            plugin.plugin_settings_path(settings).write_bytes(sys.argv[4].encode())
            (settings.bin_dir / "csdm.log").write_text("ours, cut short")
            print("recording", flush=True)
            os._exit(3)  # killed: no finally runs
""")


def kill_mid_recording(settings: IngameSettings) -> None:
    run = subprocess.run([sys.executable, "-c", KILLED, str(settings.cs2_dir), str(settings.plugin_dir),
                          str(settings.data_dir), RECORDED_SETTINGS.decode()], capture_output=True, text=True,
                         timeout=60)
    assert run.returncode == 3 and "recording" in run.stdout, run.stderr


def test_the_next_start_puts_back_what_a_killed_recording_changed(tmp_path):
    settings = cs_demo_manager_user(tmp_path)
    before = snapshot(settings.cs2_dir)
    kill_mid_recording(settings)

    # what the audit found after Ctrl+C on `demoreel serve`: all of it still changed
    assert b"Game\tcsgo/csdm" in settings.gameinfo_path.read_bytes()
    assert (plugin.plugin_bin_dir(settings) / "server.dll").read_bytes() == DLL
    assert plugin.plugin_settings_path(settings).read_bytes() == RECORDED_SETTINGS
    left = plugin.leftovers(settings)
    assert any("gameinfo.gi" in x for x in left) and any("csdm_settings.json" in x for x in left)
    assert any("csdm/bin" in x for x in left) and any("raw frames" in x for x in left)

    done = plugin.recover_install(settings)
    assert "gameinfo.gi restored" in done and "csdm_settings.json restored" in done
    assert "CS Demo Manager's csdm/bin put back" in done and "3 raw frames deleted" in done
    assert snapshot(settings.cs2_dir) == before  # byte for byte, CS Demo Manager's files included
    assert (settings.data_dir / plugin.LAST_LOG_NAME).read_text() == "ours, cut short"
    assert plugin.leftovers(settings) == [] and plugin.recover_install(settings) == []


def test_a_killed_recording_in_a_folder_that_never_had_cs_demo_manager_is_undone_too(tmp_path):
    settings = make_settings(tmp_path)
    (settings.plugin_dir / "server.dll").write_bytes(DLL)
    plugin.plugin_settings_path(settings).parent.mkdir()
    before = snapshot(settings.cs2_dir)
    kill_mid_recording(settings)
    done = plugin.recover_install(settings)
    assert "recording plugin removed from csdm/bin" in done
    assert snapshot(settings.cs2_dir) == before
    assert not settings.plugin_install_dir.exists() and not encode.wav_recording_dir(settings).exists()


@pytest.fixture
def killed(env):  # noqa: F811 - the fixture imported from test_ingame_renderer
    """A fake install whose last recording was killed, and a renderer for it that never starts a game."""
    settings, demo, tmp_path = env
    add_plugin(settings)
    original = settings.gameinfo_path.read_bytes()
    kill_mid_recording_with(settings)
    return settings, demo, tmp_path, original


def kill_mid_recording_with(settings: IngameSettings) -> None:
    """Same as :func:`kill_mid_recording` for the renderer's fake install (its platform may be Linux)."""
    script = KILLED.replace('platform="win32"', f'platform="{settings.platform}"').replace(
        'select_plugin_binary(plugins, "win32"', f'select_plugin_binary(plugins, "{settings.platform}"')
    plugin.plugin_settings_path(settings).parent.mkdir(parents=True, exist_ok=True)
    run = subprocess.run([sys.executable, "-c", script, str(settings.cs2_dir), str(settings.plugin_dir),
                          str(settings.data_dir), "{}"], capture_output=True, text=True, timeout=60)
    assert run.returncode == 3, run.stderr


def test_availability_says_the_folder_is_still_changed_until_it_is_put_back(killed):
    settings, _, _, original = killed
    renderer = renderer_for(settings, None)
    availability = renderer.availability()
    assert not availability.ok and "interrupted" in availability.reason
    assert "Do not start CS2 from Steam" in availability.reason
    assert availability.details["interrupted_recording"]

    assert "gameinfo.gi restored" in renderer.recover_install()
    assert settings.gameinfo_path.read_bytes() == original
    assert renderer.availability().ok and renderer.availability().details["interrupted_recording"] == []


def test_nothing_is_put_back_while_a_recording_runs_or_while_cs2_is_open(killed):
    settings, _, _, original = killed
    holder = launcher.RecordingLock(settings.install_lock_path)  # another data folder recording right now
    assert holder.try_acquire()
    try:
        assert renderer_for(settings, None).recover_install() == []
        assert renderer_for(settings, None).availability().details["interrupted_recording"] == []
    finally:
        holder.release()
    assert b"Game\tcsgo/csdm" in settings.gameinfo_path.read_bytes()  # untouched

    said = renderer_for(settings, None, cs2_running=True).recover_install()
    assert len(said) == 1 and "CS2 is running" in said[0]
    assert b"Game\tcsgo/csdm" in settings.gameinfo_path.read_bytes()

    assert renderer_for(settings, None).recover_install()  # game closed, nobody recording: now it is done
    assert settings.gameinfo_path.read_bytes() == original


def test_the_next_recording_puts_it_back_first_and_says_so(killed):
    settings, demo, tmp_path, original = killed
    game = FakeGame(settings)
    result = renderer_for(settings, game).render(make_job(demo, tmp_path, [Clip(segments=[
        ClipSegment(8000, 8032, STEAM_A)])]))
    assert game.calls[0]["gameinfo"].count(b"Game\tcsgo/csdm") == 1
    assert result.details["frames"] == 15  # the killed run's frames were not taken for this one's
    assert any("previous run" in w and "gameinfo.gi restored" in w for w in result.details["warnings"])
    assert settings.gameinfo_path.read_bytes() == original and not plugin.journal_path(settings).exists()


# --- serve, worker and doctor put it back when they start ----------------------------------------------------


class FakeIngame:
    name = "ingame"
    description = "fake"

    def __init__(self, done: list[str]) -> None:
        self.done, self.calls = done, 0

    def recover_install(self) -> list[str]:
        self.calls += 1
        return self.done

    def availability(self):
        from demoreel.render.base import Availability

        return Availability(ok=True, details={"hlae": None, "hlae_installs": []})


def test_doctor_puts_the_cs2_folder_back_before_it_checks(monkeypatch):
    from typer.testing import CliRunner

    import demoreel.render as render
    from demoreel.cli import app

    fake = FakeIngame(["gameinfo.gi restored", "CS Demo Manager's csdm/bin put back"])
    real = render.get_renderer
    monkeypatch.setattr(render, "get_renderer", lambda name: fake if name == "ingame" else real(name))
    out = CliRunner().invoke(app, ["doctor"])
    assert out.exit_code == 0, out.output
    assert fake.calls == 1
    assert "ingame        gameinfo.gi restored" in out.output
    assert out.output.index("gameinfo.gi restored") < out.output.index("renderer      OK ingame")


def test_start_up_recovery_never_keeps_demoreel_from_starting(monkeypatch, capsys):
    import demoreel.render as render
    from demoreel.cli import _recover_ingame

    def broken(name):
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(render, "get_renderer", broken)
    _recover_ingame()
    assert "Could not check the CS2 folder" in capsys.readouterr().out


class FakeWorker:
    def __init__(self, stops_in: float | None) -> None:
        self.stops_in, self.terminated, self.joined = stops_in, False, []

    def join(self, timeout=None):
        self.joined.append(timeout)

    def is_alive(self):
        return self.stops_in is None or self.stops_in > (self.joined[-1] if self.joined else 0)

    def terminate(self):
        self.terminated = True

    pid = 4242


def test_serve_asks_its_workers_to_stop_and_waits_before_ending_them():
    import threading

    from demoreel.cli import stop_workers

    stop = threading.Event()
    quick, stuck = FakeWorker(stops_in=0.0), FakeWorker(stops_in=None)
    late = stop_workers([quick, stuck], stop, timeout_s=0.2)
    assert stop.is_set()  # asked first...
    assert quick.joined and stuck.joined  # ...waited for...
    assert late == [stuck] and stuck.terminated and not quick.terminated  # ...and only the stuck one ended


# --- the game dies with demoreel -----------------------------------------------------------------------------

PARENT = textwrap.dedent("""
    import subprocess, sys, time
    from demoreel.render.ingame import procs
    container = procs.DieWithParent()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                             **container.popen_kwargs(procs.popen_group_kwargs()))
    container.started(child)
    print(child.pid, flush=True)
    time.sleep(120)
""")


def _alive(pid: int) -> bool:
    found = procs.running_processes()
    assert found is not None
    return any(p == pid for p, _ in found)


@pytest.mark.skipif(not (sys.platform == "win32" or sys.platform.startswith("linux")), reason="Windows/Linux only")
def test_a_game_started_by_demoreel_dies_when_demoreel_is_killed():
    """What `p.terminate()` of a worker, a closed console or a crash look like from the game's side: the
    process that started it is gone, at once and without cleaning up. A sleeping Python child stands in for CS2."""
    parent = subprocess.Popen([sys.executable, "-c", PARENT], stdout=subprocess.PIPE, text=True)
    child_pid = None
    try:
        child_pid = int(parent.stdout.readline())
        assert _alive(child_pid)
        parent.kill()  # TerminateProcess / SIGKILL
        parent.wait(timeout=30)
        deadline = time.monotonic() + 15
        while _alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.2)
        assert not _alive(child_pid), "the child outlived the process that started it"
    finally:
        parent.kill()
        if child_pid is not None and _alive(child_pid):
            procs._kill_pid(child_pid)


class WatchedContainment(procs.Containment):
    def __init__(self) -> None:
        self.events: list[str] = []

    def popen_kwargs(self, base: dict) -> dict:
        self.events.append("kwargs")
        return base

    def started(self, proc) -> None:
        self.events.append(f"started {proc.pid}")

    def kill(self) -> None:
        self.events.append("kill")

    def close(self) -> None:
        self.events.append("close")


def test_run_game_hands_the_process_to_its_containment_and_lets_go_after(tmp_path):
    watched = WatchedContainment()
    launcher.run_game([sys.executable, "-c", "pass"], timeout_s=30, poll_s=0.05, contain=lambda: watched)
    assert watched.events[0] == "kwargs" and watched.events[1].startswith("started ")
    assert watched.events[-1] == "close" and "kill" not in watched.events

    watched = WatchedContainment()
    with pytest.raises(launcher.RenderCancelled):
        launcher.run_game([sys.executable, "-c", "import time; time.sleep(60)"], timeout_s=30, poll_s=0.05,
                          cancelled=lambda: True, contain=lambda: watched)
    assert watched.events[-2:] == ["kill", "close"]


def test_a_job_is_given_back_when_its_worker_stops(monkeypatch):
    """Ctrl+C on `demoreel serve`, or SIGTERM: the job sees itself cancelled, the recording puts the folder back,
    and the job waits in the queue for the next worker instead of ending as canceled."""
    import threading

    from demoreel import jobs
    from demoreel.db import Job, init_db, session_scope

    init_db()
    monkeypatch.setattr(jobs, "_STOP", threading.Event())
    seen = []

    @jobs.handler("test_stop")
    def handler(ctx):
        jobs.request_stop()  # the signal arrives while the job runs
        seen.append(ctx.cancelled())
        raise jobs.JobCancelled()

    with session_scope() as s:
        job_id = jobs.enqueue(s, "test_stop", {}, capability="test-stop").id
    assert jobs.run_one("test-worker", ["test-stop"])
    with session_scope() as s:
        row = s.get(Job, job_id)
        assert (row.status, row.message, row.worker_id) == ("queued", jobs.STOPPED_MESSAGE, None)
    assert seen == [True]
    jobs.worker_loop(["test-stop"], poll_s=0.01)  # a stopping worker takes nothing more
    with session_scope() as s:
        assert s.get(Job, job_id).status == "queued"
        s.delete(s.get(Job, job_id))
    jobs.HANDLERS.pop("test_stop")


def test_a_second_stop_signal_stops_at_once(monkeypatch):
    import signal
    import threading

    from demoreel import jobs

    monkeypatch.setattr(jobs, "_STOP", threading.Event())
    installed = {}
    monkeypatch.setattr(signal, "signal", lambda sig, handler: installed.__setitem__(sig, handler))
    handled = jobs.install_stop_handlers()
    assert "SIGINT" in handled and "SIGTERM" in handled
    assert ("SIGBREAK" in handled) == (sys.platform == "win32")
    handler = installed[signal.SIGINT]
    handler(signal.SIGINT, None)
    assert jobs.stopping()
    with pytest.raises(KeyboardInterrupt):
        handler(signal.SIGINT, None)


def test_the_journal_is_kept_when_something_of_ours_could_not_be_removed(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    (settings.plugin_dir / "server.dll").write_bytes(DLL)
    binary = plugin.select_plugin_binary(settings.plugin_dir, "win32", None)
    real = plugin.uninstall_plugin
    monkeypatch.setattr(plugin, "uninstall_plugin", lambda s: None)  # the game kept server.dll locked
    with plugin.recording_session(settings):
        with plugin.plugin_session(settings, binary):
            pass
    assert plugin.journal_path(settings).is_file()
    monkeypatch.setattr(plugin, "uninstall_plugin", real)
    assert "recording plugin removed from csdm/bin" in plugin.recover_install(settings)
    assert not settings.plugin_install_dir.exists() and not plugin.journal_path(settings).exists()
    assert settings.gameinfo_path.read_bytes() == GAMEINFO.encode()
