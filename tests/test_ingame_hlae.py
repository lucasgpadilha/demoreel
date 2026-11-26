"""The HLAE recording path and what happens when recording does not start (after a CS2 update, typically).

HLAE is never started: a fake loader writes what HLAE would write (a small real mp4 and a WAV per take, made
with the bundled ffmpeg) into the job's folder, and the CS2 install is a fake folder under tmp_path. Settings say
"win32" so the HLAE path runs on any system; nothing Windows-specific is called with them."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import time
import wave
from pathlib import Path

import pytest

from demoreel.analysis.schema import MatchAnalysis
from demoreel.render import ffmpeg
from demoreel.render.base import Clip, ClipSegment, RenderError, RenderJob, RenderOptions
from demoreel.render.ingame import IngameRenderer, encode, launcher, plugin, procs
from demoreel.render.ingame.renderer import HlaeFailed, _Launch
from demoreel.render.ingame.settings import IngameSettings

GAMEINFO = b"\"GameInfo\"\r\n{\r\n\tSearchPaths\r\n\t{\r\n\t\t\tGame\tcsgo\r\n\t\t\tGame\tcore\r\n\t}\r\n}\r\n"
STEAM_A = "76561198000000001"
ADDRESSES_BOX = ("AfxHookSource2 Error: addresses.cpp(812): Could not find the address of a function this "
                 "version of HLAE needs.")


@pytest.fixture
def hlae_env(tmp_path):
    cs2 = tmp_path / "cs2"
    (cs2 / "game" / "csgo").mkdir(parents=True)
    (cs2 / "game" / "csgo" / "gameinfo.gi").write_bytes(GAMEINFO)
    (cs2 / "game" / "csgo" / "steam.inf").write_text("PatchVersion=1.42.0.1\n")
    data = tmp_path / "data"
    exe = data / "cache" / "hlae" / "2.193.1" / "HLAE.exe"
    (exe.parent / "x64").mkdir(parents=True)
    exe.write_bytes(b"MZ")
    (exe.parent / "x64" / "AfxHookSource2.dll").write_bytes(b"MZ")
    settings = IngameSettings(cs2_dir=cs2, steam_dir=None, steam_exe=None, plugin_dir=tmp_path / "plugins",
                              data_dir=data / "ingame", platform="win32", hlae_exe=exe)
    settings.game_executable.parent.mkdir(parents=True)
    settings.game_executable.write_bytes(b"MZ")
    settings.plugin_dir.mkdir()
    (settings.plugin_dir / "server.dll").write_bytes(b"MZ" + b"\0" * plugin.MIN_LIBRARY_BYTES)
    demo = tmp_path / "demos" / "match.dem"
    demo.parent.mkdir()
    demo.write_bytes(b"not a demo")
    return settings, demo, tmp_path


def make_job(demo: Path, tmp_path: Path, clips: list[Clip], **extra) -> RenderJob:
    job = RenderJob(demo_path=demo, analysis=MatchAnalysis.model_construct(tickrate=64, total_ticks=100000),
                    clips=clips, out_path=tmp_path / "out" / "clip.mp4", work_dir=tmp_path / "work",
                    options=RenderOptions(width=128, height=72, fps=30, crf=30, extra=dict(extra)),
                    thumbnail_path=None)
    job.work_dir.mkdir(parents=True, exist_ok=True)
    return job


def sample_video(path: Path, frames: int, size: str = "128x72") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg.run(["-f", "lavfi", "-i", f"testsrc=size={size}:rate=30", "-frames:v", str(frames), "-c:v", "libx264",
                "-pix_fmt", "yuv420p", str(path)], "sample video")
    return path


def sample_wav(path: Path, seconds: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\0\0\0\0" * int(44100 * seconds))


def actions_of(command: list[str]) -> list[dict]:
    """The actions file of the demo HLAE was told to play (the game's command line is HLAE's -cmdLine)."""
    args = [a.strip('"') for a in shlex.split(command[command.index("-cmdLine") + 1], posix=False)]
    demo = Path(args[args.index("+playdemo") + 1])
    return json.loads(Path(str(demo) + ".json").read_text())


class FakeHlae:
    """Stands in for ``launcher.run_loader``: writes each take where the actions file tells HLAE to."""

    def __init__(self, settings: IngameSettings, *, record: bool = True, polls: tuple[float, ...] = (5.0,),
                 log: str | None = "default") -> None:
        self.settings, self.record, self.polls, self.log = settings, record, polls, log
        self.calls: list[dict] = []

    def __call__(self, command, *, on_poll, **kwargs):
        sequences = actions_of(command)
        self.calls.append({"command": list(command), "sequences": sequences, **kwargs})
        lines = []
        for sequence in sequences:
            cmds = [a["cmd"] for a in sequence["actions"]]
            lines += [f"[1] Executing: {c}" for c in cmds]
            if not self.record:
                continue
            name = next(c for c in cmds if c.startswith("mirv_streams record name"))
            take = Path(name.split('"')[1])
            ticks = {a["cmd"]: a["tick"] for a in sequence["actions"]}
            frames = round((ticks["mirv_streams record end"] - ticks["mirv_streams record start"]) / 64 * 30)
            sample_video(take / "video.mp4", frames)
            sample_wav(take / "take0000" / "audio.wav", frames / 30)
        if self.log is not None:
            (self.settings.bin_dir / "csdm.log").write_text("\n".join(lines) + "\n")
        for elapsed in self.polls:
            on_poll(elapsed)
        return launcher.GameRun(returncode=0, duration_s=60.0)


class FakeGame:
    """The game's own recorder (the fallback): JPEG frames and a WAV per take."""

    def __init__(self, settings: IngameSettings) -> None:
        self.settings, self.calls = settings, 0

    def __call__(self, command, *, on_poll, **kwargs):
        from PIL import Image

        self.calls += 1
        demo = Path(command[command.index("+playdemo") + 1])
        for sequence in json.loads(Path(str(demo) + ".json").read_text()):
            ticks = {a["cmd"].split()[0]: a["tick"] for a in sequence["actions"]}
            name = next(a["cmd"].split()[1] for a in sequence["actions"] if a["cmd"].startswith("startmovie"))
            folder = encode.tga_recording_dir(self.settings) / "2026_11_26"
            folder.mkdir(parents=True, exist_ok=True)
            for i in range(1, round((ticks["endmovie"] - ticks["startmovie"]) / 64 * 30) + 1):
                Image.new("RGB", (128, 72), (0, 0, 0)).save(folder / f"{name}{i:08d}.jpg", format="JPEG")
            sample_wav(encode.wav_recording_dir(self.settings) / "2026_11_26" / f"{name}.wav", 0.5)
        (self.settings.bin_dir / "csdm.log").write_text("[1] Executing: startmovie 1-sequence jpg wav\n")
        on_poll(5.0)
        return launcher.GameRun(returncode=0, duration_s=60.0)


def renderer(settings, *, loader=None, game=None, dialog: str | None = None) -> IngameRenderer:
    return IngameRenderer(settings, run_loader=loader or FakeHlae(settings), run_game=game or FakeGame(settings),
                          resolve_slots=lambda demo, ticks: {STEAM_A: 6}, is_cs2_running=lambda: False,
                          is_steam_running=lambda: True, find_dialog=lambda marker: dialog)


CLIP = [Clip(segments=[ClipSegment(8000, 8064, STEAM_A)]), Clip(segments=[ClipSegment(9000, 9032, STEAM_A)])]


# --- the HLAE path end to end ---------------------------------------------------------------------------------


def test_hlae_records_each_take_and_the_frames_it_really_wrote_are_kept(hlae_env):
    settings, demo, tmp_path = hlae_env
    loader = FakeHlae(settings)
    result = renderer(settings, loader=loader).render(make_job(demo, tmp_path, CLIP))
    (call,) = loader.calls
    command = call["command"]
    assert command[0] == str(settings.hlae_exe) and "-noGui" in command
    assert command[command.index("-hookDllPath") + 1] == str(settings.hlae_exe.parent / "x64" / "AfxHookSource2.dll")
    assert command[command.index("-programPath") + 1] == str(settings.game_executable)
    assert command[command.index("-cmdLine") + 1].startswith("-insecure ")
    assert call["kill_marker"].endswith("demo.dem")
    cmds = [a["cmd"] for s in call["sequences"] for a in s["actions"]]
    assert "mirv_streams record start" in cmds and not any(c.startswith("startmovie") for c in cmds)
    ini = settings.hlae_exe.parent / "ffmpeg" / "ffmpeg.ini"
    assert ini.read_text(encoding="utf-8") == f"[Ffmpeg]\nPath={ffmpeg.ffmpeg_path()}"
    assert result.details["backend"] == "hlae" and result.details["frame_format"] == "video"
    assert [s["frames"] for s in result.details["spans"]] == [30, 15]
    assert result.duration_s == pytest.approx(45 / 30) and job_out(tmp_path).stat().st_size > 0
    assert not (tmp_path / "work" / "hlae").exists() and settings.gameinfo_path.read_bytes() == GAMEINFO


def job_out(tmp_path: Path) -> Path:
    return tmp_path / "out" / "clip.mp4"


def test_mux_hlae_takes_copies_the_picture_and_scales_only_a_wrong_size(tmp_path):
    hlae = tmp_path / "hlae"
    sample_video(hlae / "1-sequence" / "video.mp4", 12)
    sample_wav(hlae / "1-sequence" / "take0000" / "audio.wav", 0.4)
    sample_video(hlae / "2-sequence" / "video.mp4", 9, size="160x90")  # no sound for this one
    out = encode.mux_hlae_takes(hlae, ["1-sequence", "2-sequence"], tmp_path / "takes", width=128, height=72,
                                fps=30, crf=30)
    assert out.take_frames == [12, 9] and out.frames == 21
    assert any("no sound" in w for w in out.warnings) and any("scaled to 128x72" in w for w in out.warnings)
    with pytest.raises(RenderError, match="HLAE recorded no video for 3-sequence"):
        encode.mux_hlae_takes(hlae, ["3-sequence"], tmp_path / "takes", width=128, height=72, fps=30, crf=30)


def test_build_hlae_command_refuses_without_insecure_or_its_hook(hlae_env):
    settings, _, _ = hlae_env
    with pytest.raises(RenderError, match="-insecure"):
        launcher.build_hlae_command(settings, ["+playdemo", "x.dem"])
    (settings.hlae_exe.parent / "x64" / "AfxHookSource2.dll").unlink()
    with pytest.raises(RenderError, match="AfxHookSource2.dll is missing"):
        launcher.build_hlae_command(settings, ["-insecure"])


def test_run_loader_follows_the_game_hlae_started_until_it_is_gone(tmp_path):
    seen = iter([False, True, True, False])
    run = launcher.run_loader([sys.executable, "-c", "pass"], timeout_s=30, poll_s=0.01,
                              is_game_running=lambda: next(seen))
    assert run.returncode == 0
    with pytest.raises(launcher.LoaderFailed, match="exit code 3"):
        launcher.run_loader([sys.executable, "-c", "import sys; sys.exit(3)"], timeout_s=30, poll_s=0.05,
                            is_game_running=lambda: False)
    with pytest.raises(launcher.LoaderFailed, match="did not appear within 0 s"):
        launcher.run_loader([sys.executable, "-c", "import time; time.sleep(30)"], timeout_s=30, poll_s=0.05,
                            is_game_running=lambda: False, appear_timeout_s=0.2)


# --- after a CS2 update: fail early, say what to do, and keep recording with the game ------------------------


def test_hlae_that_does_not_record_says_which_hlae_which_patch_and_what_to_do(hlae_env):
    settings, demo, tmp_path = hlae_env
    loader = FakeHlae(settings, record=False, polls=(31.0,))
    with pytest.raises(HlaeFailed) as excinfo:  # the plugin ran `mirv_streams record start`: HLAE's recorder
        renderer(settings, loader=loader).render(make_job(demo, tmp_path, CLIP, ingame_backend="hlae",
                                                          ingame_startup_timeout_s=30))
    message = str(excinfo.value)
    assert f"HLAE 2.193.1 ({settings.hlae_exe})" in message and "CS2 patch 1.42.0.1" in message
    assert "within 30 s" in message and str(settings.hlae_root) in message
    assert "DEMOREEL_INGAME_BACKEND=startmovie" in message and 'ingame_backend = "startmovie"' in message
    assert settings.gameinfo_path.read_bytes() == GAMEINFO


def test_the_error_box_of_a_broken_hlae_ends_the_wait_at_once_and_auto_records_with_the_game(hlae_env):
    """HLAE 2.193.1 on CS2 1.42.0.1: the AfxHookSource2 addresses.cpp box, owned by cs2.exe. Nothing looked for
    it: five minutes of "Waiting for CS2 to load the demo", then a failure, for every job in the queue."""
    settings, demo, tmp_path = hlae_env
    loader, game = FakeHlae(settings, record=False, polls=(2.0, 6.0), log=None), FakeGame(settings)
    rendering = renderer(settings, loader=loader, game=game, dialog=ADDRESSES_BOX)
    result = rendering.render(make_job(demo, tmp_path, CLIP))
    assert len(loader.calls) == 1 and game.calls == 1
    assert result.details["backend"] == "startmovie"
    first = result.details["warnings"][0]
    assert first.startswith("Recorded with the game's own recorder instead of HLAE.")
    assert "addresses.cpp(812)" in first and "HLAE 2.193.1" in first and "1.42.0.1" in first

    # remembered for this HLAE and this patch: the next job goes straight to the game's recorder
    again = renderer(settings, loader=loader, game=game, dialog=ADDRESSES_BOX).render(
        make_job(demo, tmp_path / "next", CLIP))
    assert len(loader.calls) == 1 and game.calls == 2 and again.details["backend"] == "startmovie"
    assert any("is not used with this CS2 patch" in w for w in again.details["warnings"])
    assert renderer(settings).availability().details["hlae_failed_with_this_patch"]
    assert renderer(settings)._backend(settings, {}) == "startmovie"

    # a newer HLAE in its folder is tried again
    newer = settings.hlae_root / "2.193.2" / "HLAE.exe"
    (newer.parent / "x64").mkdir(parents=True)
    newer.write_bytes(b"MZ")
    (newer.parent / "x64" / "AfxHookSource2.dll").write_bytes(b"MZ")
    from dataclasses import replace

    assert renderer(settings)._backend(replace(settings, hlae_exe=newer), {}) == "hlae"


def test_a_backend_asked_for_by_name_is_not_swapped(hlae_env):
    settings, demo, tmp_path = hlae_env
    game = FakeGame(settings)
    with pytest.raises(HlaeFailed, match="error box"):
        renderer(settings, loader=FakeHlae(settings, record=False, polls=(6.0,)), game=game,
                 dialog=ADDRESSES_BOX).render(make_job(demo, tmp_path, CLIP, ingame_backend="hlae"))
    assert game.calls == 0


def test_no_csdm_log_two_minutes_in_ends_the_wait_and_names_the_plugin_file(hlae_env):
    settings, demo, tmp_path = hlae_env

    def silent_game(command, *, on_poll, **kwargs):
        on_poll(60.0)  # still loading: fine
        on_poll(121.0)  # no csdm.log: the plugin did not load, waiting longer changes nothing
        raise AssertionError("should have given up")

    rendering = IngameRenderer(settings, run_game=silent_game, resolve_slots=lambda d, t: {STEAM_A: 6},
                               is_cs2_running=lambda: False, is_steam_running=lambda: True,
                               find_dialog=lambda marker: None)
    with pytest.raises(RenderError) as excinfo:
        rendering.render(make_job(demo, tmp_path, CLIP, ingame_backend="startmovie"))
    message = str(excinfo.value)
    assert "no csdm.log was written 120 s after CS2 was started" in message
    assert f"`{settings.plugin_dir / 'server.dll'}` (build latest, copied there on " in message
    assert "CS2 patch 1.42.0.1" in message and "static/cs2" in message
    assert not isinstance(excinfo.value, HlaeFailed)


def test_a_game_that_closes_at_once_is_not_called_a_plugin_problem(hlae_env):
    """Steam updating CS2, or not logged in: the game is gone in seconds with exit code 0."""
    settings, demo, tmp_path = hlae_env

    def gone(command, **kwargs):
        return launcher.GameRun(returncode=0, duration_s=6.0, output_tail="")

    rendering = IngameRenderer(settings, run_game=gone, resolve_slots=lambda d, t: {STEAM_A: 6},
                               is_cs2_running=lambda: False, is_steam_running=lambda: True,
                               find_dialog=lambda marker: None)
    with pytest.raises(RenderError) as excinfo:
        rendering.render(make_job(demo, tmp_path, CLIP, ingame_backend="startmovie"))
    message = str(excinfo.value)
    assert "CS2 closed 6 s after it was started" in message and "logged in" in message and "update" in message
    assert "plugin" not in message


def test_a_second_launch_that_never_starts_is_reported_as_such_not_as_a_stall(hlae_env):
    """Frames of the first launch are on disk: they said nothing about whether the second one started."""
    from types import SimpleNamespace

    settings, demo, tmp_path = hlae_env
    calls = []

    def game(command, *, on_poll, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return FakeGame(settings)(command, on_poll=on_poll)
        (settings.bin_dir / "csdm.log").write_text("[1] Executing: sv_cheats 1\n")
        on_poll(11.0)
        raise AssertionError("should have given up")

    job = make_job(demo, tmp_path, [Clip(segments=[ClipSegment(8000, 8032, STEAM_A)]),
                                    Clip(segments=[ClipSegment(29952, 30016, STEAM_A)]),
                                    Clip(segments=[ClipSegment(29984, 30016, STEAM_A)])],
                   ingame_backend="startmovie", ingame_startup_timeout_s=10)
    job.analysis = MatchAnalysis.model_construct(tickrate=64, total_ticks=100000,
                                                 rounds=[SimpleNamespace(end_tick=30000, kills=[])])
    rendering = IngameRenderer(settings, run_game=game, resolve_slots=lambda d, t: {STEAM_A: 6},
                               is_cs2_running=lambda: False, is_steam_running=lambda: True,
                               find_dialog=lambda marker: None)
    with pytest.raises(RenderError, match="did not start recording within 10 s"):
        rendering.render(job)
    assert len(calls) == 2


def test_the_last_hlae_take_is_finished_only_when_the_plugin_ran_its_end(tmp_path):
    hlae = tmp_path / "hlae"
    for take in ("1-sequence", "2-sequence"):
        (hlae / take).mkdir(parents=True)
        (hlae / take / "video.mp4").write_bytes(b"x" * 100)
    settings = IngameSettings(cs2_dir=tmp_path / "cs2", steam_dir=None, steam_exe=None, plugin_dir=tmp_path,
                              data_dir=tmp_path / "d", platform="win32")
    settings.bin_dir.mkdir(parents=True)
    log = settings.bin_dir / "csdm.log"
    launch = _Launch(settings=settings, binary=None, marker="demo.dem", hlae=True, patch="1.42.0.1")
    job = make_job(tmp_path / "x.dem", tmp_path, CLIP)
    poll = renderer(settings)._hlae_poller(job, hlae, 2, 300, 300, done_at=2, launch=launch, in_launch=2)
    poll(1.0)  # both files there, then no new byte

    log.write_text("[1] Executing: mirv_streams record end\n")  # frozen in the second take
    poll(70.0)  # a minute without new bytes, but take 2 never ended: not finished, the stall timeout decides
    log.write_text("[1] Executing: mirv_streams record end\n[2] Executing: mirv_streams record end\n")
    with pytest.raises(RenderError, match="did not quit by itself"):
        poll(80.0)
    log.unlink()  # a plugin build without a log: as before, the files decide
    with pytest.raises(RenderError, match="did not quit by itself"):
        poll(90.0)


# --- the error box itself, on Windows ---------------------------------------------------------------------------

BOX = ("import ctypes; ctypes.windll.user32.MessageBoxW(None, 'addresses.cpp(812): test only, closed by the "
       "test', 'AfxHookSource2 Error', 0x10)")


@pytest.mark.skipif(sys.platform != "win32", reason="error boxes are looked for on Windows only")
def test_an_error_box_of_a_process_is_found_with_its_title_and_text():
    # The base interpreter: a venv's python.exe on Windows can be a launcher that runs it as a child process,
    # and the box would belong to that child.
    child = subprocess.Popen([getattr(sys, "_base_executable", sys.executable), "-c", BOX])
    try:
        deadline = time.monotonic() + 15
        found: list[str] = []
        while not found and time.monotonic() < deadline:
            time.sleep(0.2)
            found = procs.dialogs_of([child.pid])
        assert found and found[0].startswith("AfxHookSource2 Error: addresses.cpp(812)")
        assert procs.dialogs_of([]) == []
    finally:
        child.kill()
        child.wait(timeout=15)
