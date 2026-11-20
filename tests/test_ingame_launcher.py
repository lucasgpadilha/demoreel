"""Command line, environment, lock file and watchdog of the in-game renderer. No game is started: the
watchdog tests babysit a short-lived Python child process instead."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from demoreel.render.base import RecorderBusy, RenderCancelled, RenderError
from demoreel.render.ingame import launcher, procs, staging
from demoreel.render.ingame.settings import IngameSettings


def settings_for(tmp_path: Path, platform: str, runtime: Path | None = None) -> IngameSettings:
    return IngameSettings(cs2_dir=tmp_path / "cs2", steam_dir=None, steam_exe=None,
                          plugin_dir=tmp_path / "plugins", data_dir=tmp_path / "data" / "ingame",
                          runtime_script=runtime, platform=platform)


# --- arguments ----------------------------------------------------------------------------------------------


def test_launch_args_follow_the_reference_and_always_start_with_insecure():
    args = launcher.build_launch_args(Path("C:/demos/my match.dem"), 1280, 720)
    assert args == ["-insecure", "-novid", "+playdemo", str(Path("C:/demos/my match.dem")),
                    "+engine_no_focus_sleep", "0",  # demoreel addition, see build_launch_args
                    "-width", "1280", "-height", "720", "-sw"]


def test_extra_launch_args_cannot_drop_or_duplicate_insecure():
    args = launcher.build_launch_args("d.dem", 1920, 1080, extra=["-insecure", "", "-noborder"])
    assert args.count("-insecure") == 1 and args[0] == "-insecure"
    assert args.index("-noborder") < args.index("-width")
    assert args[args.index("+playdemo") + 1] == "d.dem"


def test_windows_command_runs_cs2_exe_directly(tmp_path):
    settings = settings_for(tmp_path, "win32")
    cmd = launcher.build_command(settings, launcher.build_launch_args("d.dem", 1280, 720))
    assert cmd[0] == str(tmp_path / "cs2" / "game" / "bin" / "win64" / "cs2.exe")
    assert cmd[1] == "-insecure" and "-sw" in cmd


def test_linux_command_goes_through_the_steam_runtime(tmp_path):
    runtime = tmp_path / "SteamLinuxRuntime_sniper" / "_v2-entry-point"
    settings = settings_for(tmp_path, "linux", runtime)
    cmd = launcher.build_command(settings, launcher.build_launch_args("d.dem", 1280, 720))
    assert cmd[:4] == [str(runtime), "--verb=waitforexitandrun", "--", str(tmp_path / "cs2" / "game" / "cs2.sh")]
    assert cmd[4] == "-insecure"

    with pytest.raises(RenderError, match="sniper"):
        launcher.build_command(settings_for(tmp_path, "linux"), ["-insecure"])


def test_command_is_refused_without_insecure_or_on_other_platforms(tmp_path):
    with pytest.raises(RenderError, match="-insecure"):
        launcher.build_command(settings_for(tmp_path, "win32"), ["-novid", "+playdemo", "d.dem"])
    with pytest.raises(RenderError, match="darwin"):
        launcher.build_command(settings_for(tmp_path, "darwin"), ["-insecure"])


def test_environment_isolates_user_config_and_the_csdm_websocket(tmp_path):
    settings = settings_for(tmp_path, "win32")
    env = launcher.build_env(settings, {"ENABLE_CSDM_OVERLAY": "1"}, base={"PATH": "x", "CSDM_WS_PORT": "4574"})
    assert env["USRLOCALCSGO"] == str(tmp_path / "data" / "ingame" / "cfg")
    assert env["PATH"] == "x" and env["ENABLE_CSDM_OVERLAY"] == "1"
    assert env["CSDM_WS_PORT"].isdigit() and env["CSDM_WS_PORT"] != "4574"


def test_basic_latin_check():
    assert launcher.is_basic_latin(r"C:\Users\lucas\demos\match (1).dem")
    assert not launcher.is_basic_latin(r"C:\Users\João\demos\match.dem")


# --- preconditions --------------------------------------------------------------------------------------------


def test_refuses_to_start_when_cs2_is_running_or_steam_is_not():
    with pytest.raises(RenderError, match="already running"):
        launcher.assert_can_start(lambda: True, lambda: True)
    with pytest.raises(RenderError, match="Steam is not running"):
        launcher.assert_can_start(lambda: False, lambda: False)
    launcher.assert_can_start(lambda: False, lambda: True)
    launcher.assert_can_start(lambda: None, lambda: None)  # process list unavailable: do not block


def test_process_listing_sees_this_interpreter():
    names = procs.running_process_names()
    assert names, "process list should be available on Windows and Linux"
    assert any("python" in n or "pytest" in n or "uv" in n for n in names)
    assert procs.is_any_running(["definitely-not-a-real-process-name.exe"]) is False


# --- lock -----------------------------------------------------------------------------------------------------


def test_only_one_recording_lock_can_be_held(tmp_path):
    path = tmp_path / "ingame" / "recording.lock"
    assert launcher.RecordingLock(path).is_held_elsewhere() is False  # and the probe creates nothing
    assert not path.exists()

    first, second = launcher.RecordingLock(path), launcher.RecordingLock(path)
    assert first.try_acquire() and first.try_acquire()
    assert not second.try_acquire()
    assert second.is_held_elsewhere() and not first.is_held_elsewhere()
    first.release()
    assert second.try_acquire()
    second.release()
    first.release()  # releasing twice is harmless


def test_recording_lock_context_fails_fast_waits_and_honours_cancel(tmp_path):
    path = tmp_path / "recording.lock"
    other = launcher.RecordingLock(path)  # what another process recording looks like from here
    assert other.try_acquire()
    try:
        with pytest.raises(RecorderBusy, match="Another in-game recording") as busy:
            with launcher.recording_lock(path, wait_s=0):
                pass
        assert isinstance(busy.value, RenderError)  # still the error a direct render reports
        waits: list[float] = []
        with pytest.raises(RenderCancelled):
            with launcher.recording_lock(path, wait_s=30, cancelled=lambda: len(waits) >= 2,
                                         on_wait=waits.append, poll_s=0.01):
                pass
        assert len(waits) == 2
    finally:
        other.release()
    with launcher.recording_lock(path, wait_s=0):  # released: can be taken again
        pass


def test_whoever_holds_the_recording_lock_enters_again_and_keeps_it_until_the_outer_use_ends(tmp_path):
    path = tmp_path / "recording.lock"
    probe = launcher.RecordingLock(path)
    with launcher.recording_lock(path) as outer:
        with launcher.recording_lock(path, wait_s=0) as inner:  # a job recording twice in a row
            assert inner is outer
        assert probe.is_held_elsewhere()  # leaving the inner use did not let go of the game
        seen = []
        thread = threading.Thread(target=lambda: seen.append(_enters(path)))  # another thread is someone else
        thread.start()
        thread.join()
        assert seen == [False]
    assert not probe.is_held_elsewhere()


def _enters(path) -> bool:
    try:
        with launcher.recording_lock(path, wait_s=0):
            return True
    except RecorderBusy:
        return False


def test_a_probe_passing_by_is_not_taken_for_a_recording(tmp_path):
    path = tmp_path / "recording.lock"
    passing = launcher.RecordingLock(path)
    assert passing.try_acquire()
    threading.Timer(0.08, passing.release).start()  # an idle worker asking "is it taken?" lets go at once
    with launcher.recording_lock(path, wait_s=0):
        pass


# --- watchdog -------------------------------------------------------------------------------------------------


def sleeper(seconds: float) -> list[str]:
    return [sys.executable, "-c", f"import time; print('hello from child', flush=True); time.sleep({seconds})"]


def test_run_game_returns_exit_code_and_output_and_reports_progress(tmp_path):
    polls: list[float] = []
    run = launcher.run_game([sys.executable, "-c", "import sys, time; print('bye'); time.sleep(0.4); sys.exit(3)"],
                            timeout_s=30, on_poll=polls.append, log_path=tmp_path / "out.log", poll_s=0.05)
    assert run.returncode == 3
    assert "bye" in run.output_tail
    assert polls and polls == sorted(polls)


def test_watchdog_kills_the_process_tree_on_timeout(tmp_path):
    started = time.monotonic()
    with pytest.raises(RenderError, match="did not finish within 1 s"):
        launcher.run_game(sleeper(60), timeout_s=1, log_path=tmp_path / "out.log", poll_s=0.05)
    assert time.monotonic() - started < 30


def test_cancel_kills_the_game_and_raises_render_cancelled(tmp_path):
    started = time.monotonic()
    with pytest.raises(RenderCancelled):
        launcher.run_game(sleeper(60), timeout_s=120, cancelled=lambda: time.monotonic() - started > 0.5,
                          poll_s=0.05)
    assert time.monotonic() - started < 30


def test_error_raised_by_the_poll_callback_stops_the_game(tmp_path):
    def stalled(elapsed: float) -> None:
        if elapsed > 0.3:
            raise RenderError("Recording stalled")

    started = time.monotonic()
    with pytest.raises(RenderError, match="stalled"):
        launcher.run_game(sleeper(60), timeout_s=120, on_poll=stalled, poll_s=0.05)
    assert time.monotonic() - started < 30


def test_missing_executable_is_a_render_error(tmp_path):
    with pytest.raises(RenderError, match="Could not start the game"):
        launcher.run_game([str(tmp_path / "nope" / "cs2.exe"), "-insecure"], timeout_s=5)


# --- demo staging ---------------------------------------------------------------------------------------------


def test_demo_is_staged_in_the_work_dir_with_its_actions_file(tmp_path):
    demo = tmp_path / "demos" / "my match.dem"
    demo.parent.mkdir()
    demo.write_bytes(b"PBDEMS2\x00 fake demo")
    staged = staging.stage_demo(demo, tmp_path / "work")
    assert staged.owned and staged.path == tmp_path / "work" / "ingame" / "demo.dem"
    assert staged.path.read_bytes() == demo.read_bytes()
    assert staged.actions_path.name == "demo.dem.json"  # exactly "<path given to +playdemo>.json"
    staged.write_actions('[{"actions": []}]')
    assert staged.actions_path.read_text() == '[{"actions": []}]'

    staged.cleanup()
    assert not staged.path.exists() and not staged.actions_path.exists()
    assert demo.is_file() and not Path(str(demo) + ".json").exists()  # the original is untouched


def test_demo_with_ascii_path_is_used_in_place_when_the_work_dir_is_not_ascii(tmp_path):
    demo = tmp_path / "match.dem"
    demo.write_bytes(b"fake")
    try:
        work = tmp_path / "trabalho-ç"
        work.mkdir()
    except OSError:
        pytest.skip("filesystem cannot create a non-ASCII folder")
    if not launcher.is_basic_latin(str(demo)):
        pytest.skip("temp folder itself is not ASCII")
    staged = staging.stage_demo(demo, work)
    assert not staged.owned and staged.path == demo
    staged.write_actions("[]")
    staged.cleanup()
    assert demo.is_file() and not staged.actions_path.exists()

    staged.actions_path.write_text("[]")  # someone else's actions file: refuse rather than overwrite
    with pytest.raises(RenderError, match="already exists"):
        staging.stage_demo(demo, work)


def test_missing_demo_is_reported(tmp_path):
    with pytest.raises(RenderError, match="Demo file not found"):
        staging.stage_demo(tmp_path / "nope.dem", tmp_path / "work")
