"""IngameRenderer orchestration with a fake game: a stand-in for ``run_game`` writes the TGA/WAV files CS2
would write into a temp "CS2 install". Nothing here starts CS2 or touches the real Steam folder."""

from __future__ import annotations

import json
import sys
import wave
from pathlib import Path

import pytest

from demoreel.analysis.schema import MatchAnalysis
from demoreel.render.base import Clip, ClipSegment, RenderCancelled, RenderError, RenderJob, RenderOptions
from demoreel.render.ingame import IngameRenderer, encode, launcher, plugin
from demoreel.render.ingame.settings import IngameSettings

GAMEINFO = (b"\"GameInfo\"\r\n{\r\n\tFileSystem\r\n\t{\r\n\t\tSearchPaths\r\n\t\t{\r\n\t\t\tGame\tcsgo\r\n"
            b"\t\t\tGame\tcore\r\n\t\t}\r\n\t}\r\n}\r\n")
STEAM_A, STEAM_B = "76561198000000001", "76561198000000002"
FAKE_DEMO = b"not a real demo, just enough bytes to be read as a header"
PLATFORM = "win32" if sys.platform == "win32" else "linux"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A fake CS2 install + plugin folder + demo."""
    monkeypatch.setenv("DISPLAY", ":0")
    cs2 = tmp_path / "cs2"
    (cs2 / "game" / "csgo").mkdir(parents=True)
    (cs2 / "game" / "csgo" / "gameinfo.gi").write_bytes(GAMEINFO)
    (cs2 / "game" / "csgo" / "steam.inf").write_text("PatchVersion=1.41.9.7\n")
    runtime = tmp_path / "SteamLinuxRuntime_sniper" / "_v2-entry-point"
    runtime.parent.mkdir()
    runtime.write_text("#!/bin/sh\n")
    settings = IngameSettings(cs2_dir=cs2, steam_dir=None, steam_exe=None, plugin_dir=tmp_path / "plugins",
                              data_dir=tmp_path / "data" / "ingame", runtime_script=runtime, platform=PLATFORM)
    settings.game_executable.parent.mkdir(parents=True, exist_ok=True)
    settings.game_executable.write_bytes(b"")  # placeholder so the "executable exists" check passes
    settings.bin_dir.mkdir(parents=True, exist_ok=True)
    settings.plugin_dir.mkdir()
    demo = tmp_path / "demos" / "match.dem"
    demo.parent.mkdir()
    demo.write_bytes(FAKE_DEMO)
    return settings, demo, tmp_path


def add_plugin(settings: IngameSettings) -> None:
    (settings.plugin_dir / plugin.installed_binary_name(settings.platform)).write_bytes(b"placeholder library")


def make_job(demo: Path, tmp_path: Path, clips: list[Clip], **kwargs) -> RenderJob:
    analysis = MatchAnalysis.model_construct(tickrate=64, total_ticks=100000)
    job = RenderJob(demo_path=demo, analysis=analysis, clips=clips, out_path=tmp_path / "out" / "reel.mp4",
                    work_dir=tmp_path / "work", options=RenderOptions(width=128, height=72, fps=30, crf=30),
                    thumbnail_path=tmp_path / "out" / "reel.jpg", **kwargs)
    job.work_dir.mkdir(parents=True, exist_ok=True)
    return job


class FakeGame:
    """Stands in for ``launcher.run_game``: records what the real game would have been given and writes the
    frames + audio of every ``startmovie`` found in the actions file."""

    def __init__(self, settings: IngameSettings, *, write: bool = True, returncode: int = 0,
                 skip_names: tuple[str, ...] = ()) -> None:
        self.settings, self.write, self.returncode, self.skip_names = settings, write, returncode, skip_names
        self.calls: list[dict] = []

    def __call__(self, command, *, env, timeout_s, cancelled, on_poll, log_path, kill_names):
        from PIL import Image

        demo = Path(command[command.index("+playdemo") + 1])
        sequences = json.loads(Path(str(demo) + ".json").read_text())
        self.calls.append({
            "command": list(command), "env": dict(env), "timeout_s": timeout_s, "sequences": sequences,
            "gameinfo": self.settings.gameinfo_path.read_bytes(),
            "plugin_installed": (plugin.plugin_bin_dir(self.settings)
                                 / plugin.installed_binary_name(self.settings.platform)).is_file(),
            "backup_exists": plugin.backup_path(self.settings.gameinfo_path).is_file(),
            "demo_bytes": demo.read_bytes(),
        })
        if self.write:
            tga_dir = encode.tga_recording_dir(self.settings) / "2026_11_14"
            wav_dir = encode.wav_recording_dir(self.settings) / "2026_11_14"
            tga_dir.mkdir(parents=True, exist_ok=True)
            wav_dir.mkdir(parents=True, exist_ok=True)
            for sequence in sequences:
                ticks = {a["cmd"].split()[0]: a["tick"] for a in sequence["actions"]}
                name = next(a["cmd"].split()[1] for a in sequence["actions"] if a["cmd"].startswith("startmovie"))
                if name in self.skip_names:
                    continue
                frames = round((ticks["endmovie"] - ticks["startmovie"]) / 64 * 30)
                for i in range(1, frames + 1):
                    Image.new("RGB", (128, 72), (0, 120, 255)).save(tga_dir / f"{name}{i:08d}.tga", format="TGA")
                with wave.open(str(wav_dir / f"{name}.wav"), "wb") as wav:
                    wav.setnchannels(2)
                    wav.setsampwidth(2)
                    wav.setframerate(44100)
                    wav.writeframes(b"\x00\x00\x00\x00" * int(44100 * frames / 30))
            on_poll(5.0)  # the renderer's poller sees frames on disk
        return launcher.GameRun(returncode=self.returncode, duration_s=42.0, output_tail="")


def renderer_for(settings: IngameSettings, game, *, cs2_running=False, steam_running=True,
                 slots=None) -> IngameRenderer:
    table = {STEAM_A: 6, STEAM_B: 11} if slots is None else slots
    return IngameRenderer(settings, run_game=game, resolve_slots=lambda demo, ticks: table,
                          is_cs2_running=lambda: cs2_running, is_steam_running=lambda: steam_running)


# --- availability ---------------------------------------------------------------------------------------------


def test_availability_explains_exactly_which_plugin_file_goes_where(env):
    settings, _, _ = env
    availability = renderer_for(settings, None).availability()
    assert availability.ok is False
    binary = plugin.installed_binary_name(settings.platform)
    assert f"static/cs2/{binary}" in availability.reason
    assert str(settings.plugin_dir / binary) in availability.reason
    assert availability.details["cs2_patch"] == 14197
    assert availability.details["cs2_dir"] == str(settings.cs2_dir)


def test_availability_ok_and_runtime_blockers(env):
    settings, _, _ = env
    add_plugin(settings)
    ok = renderer_for(settings, None).availability()
    assert ok.ok is True and ok.reason == ""
    assert ok.details["plugin_version"] == "latest" and ok.details["free_disk_gb"] > 0
    assert ok.details["recording_in_progress"] is False

    assert "Steam is not running" in renderer_for(settings, None, steam_running=False).availability().reason
    assert "already running" in renderer_for(settings, None, cs2_running=True).availability().reason

    # CS2 running because *we* are recording (lock held) is not a reason to call the renderer unavailable.
    with launcher.recording_lock(settings.lock_path):
        busy = renderer_for(settings, None, cs2_running=True).availability()
    assert busy.ok is True and busy.details["recording_in_progress"] is True


def test_availability_never_raises_and_reports_missing_cs2(tmp_path):
    settings = IngameSettings(cs2_dir=None, steam_dir=None, steam_exe=None, plugin_dir=tmp_path / "p",
                              data_dir=tmp_path / "d", platform=PLATFORM)
    availability = IngameRenderer(settings, is_cs2_running=lambda: False, is_steam_running=lambda: True).availability()
    assert not availability.ok and "DEMOREEL_CS2_DIR" in availability.reason

    def boom():
        raise RuntimeError("process list exploded")

    broken = IngameRenderer(settings, is_cs2_running=boom, is_steam_running=boom).availability()
    assert not broken.ok and "exploded" in broken.reason

    mac = IngameSettings(cs2_dir=None, steam_dir=None, steam_exe=None, plugin_dir=tmp_path, data_dir=tmp_path,
                         platform="darwin")
    assert "Windows or Linux" in IngameRenderer(mac).availability().reason


# --- render ---------------------------------------------------------------------------------------------------


def test_render_records_encodes_and_leaves_the_game_folder_as_it_found_it(env):
    settings, demo, tmp_path = env
    add_plugin(settings)
    game = FakeGame(settings)
    progress: list[tuple[float, str]] = []
    clips = [Clip(segments=[ClipSegment(20000, 20064, STEAM_A)], title="second in the demo, first in the reel"),
             Clip(segments=[ClipSegment(8000, 8032, STEAM_B), ClipSegment(9000, 9032, None)])]
    job = make_job(demo, tmp_path, clips, progress=lambda f, m: progress.append((f, m)))

    result = renderer_for(settings, game).render(job)

    # What the game was started with
    (call,) = game.calls
    command = call["command"]
    assert "-insecure" in command and command[command.index("-width") + 1] == "128" and command[-1] == "-sw"
    staged_demo = Path(command[command.index("+playdemo") + 1])
    assert staged_demo.parent == job.work_dir / "ingame" and call["demo_bytes"] == FAKE_DEMO
    assert call["env"]["USRLOCALCSGO"] == str(settings.cfg_dir)
    assert b"Game\tcsgo/csdm" in call["gameinfo"] and call["plugin_installed"] and call["backup_exists"]
    starts = [a["cmd"] for s in call["sequences"] for a in s["actions"] if a["cmd"].startswith("startmovie")]
    # JPEG frames are the default: measured 4x faster to record than TGA and 60x smaller
    assert starts == ["startmovie 2-sequence jpg wav", "startmovie 3-sequence jpg wav", "startmovie 1-sequence jpg wav"]
    specs = [a["cmd"] for s in call["sequences"] for a in s["actions"] if a["cmd"].startswith("spec_player")]
    # each first camera is issued twice: 8 ticks before the take starts, and on its start tick
    assert specs == ["spec_player 11", "spec_player 11", "spec_player 6", "spec_player 6"]
    assert call["sequences"][-1]["actions"][-1] == {"cmd": "quit", "tick": 20128}

    # The result
    assert job.out_path.stat().st_size > 0 and job.thumbnail_path.stat().st_size > 0
    assert (result.width, result.height, result.fps) == (128, 72, 30)
    assert result.duration_s == pytest.approx((30 + 15 + 15) / 30)
    assert result.details["sequences"] == 3 and result.details["frames"] == 60
    assert result.details["plugin_version"] == "latest" and result.details["game_exit_code"] == 0
    assert result.details["warnings"] == []
    fractions = [f for f, _ in progress]
    assert fractions == sorted(fractions) and fractions[-1] == 1.0
    assert any("Recording in CS2" in m for _, m in progress)

    # Nothing left behind in the game folder or next to the demo
    assert settings.gameinfo_path.read_bytes() == GAMEINFO
    assert not plugin.backup_path(settings.gameinfo_path).exists()
    assert not settings.plugin_install_dir.exists()
    assert not encode.wav_recording_dir(settings).exists()
    assert not staged_demo.exists() and not Path(str(staged_demo) + ".json").exists()
    assert sorted(p.name for p in demo.parent.iterdir()) == ["match.dem"]
    assert not launcher.RecordingLock(settings.lock_path).is_held_elsewhere()


def test_render_hides_the_plugin_panels_and_leaves_the_plugin_settings_as_it_found_them(env):
    """A demo with voice chat came out with the plugin's "who is talking" panel in the picture. Hiding it makes
    the plugin save that in its settings file, which belongs to whoever uses CS Demo Manager on the machine."""
    settings, demo, tmp_path = env
    add_plugin(settings)
    path = plugin.plugin_settings_path(settings)
    path.parent.mkdir()
    theirs = b'{\n  "csdm_buttons_overlay": true,\n  "csdm_voice_indicator": true\n}'
    seen: list[bytes] = []

    class GameWithPlugin(FakeGame):
        def __call__(self, command, **kwargs):
            seen.append(plugin.backup_path(path).read_bytes())
            run = super().__call__(command, **kwargs)
            commands = [a["cmd"] for s in self.calls[-1]["sequences"] for a in s["actions"]]
            state = {name: f"{name} 0" not in commands for name in ("csdm_buttons_overlay", "csdm_voice_indicator")}
            path.write_text(json.dumps(state, indent=2))  # what the plugin does when a setting changed
            return run

    clips = [Clip(segments=[ClipSegment(8000, 8032, STEAM_A)]), Clip(segments=[ClipSegment(9000, 9032, STEAM_B)])]
    for before in (theirs, None):  # a CS Demo Manager user's file, then an install that has none
        if before is None:
            path.unlink()
        else:
            path.write_bytes(before)
        game = GameWithPlugin(settings)
        job = make_job(demo, tmp_path, clips)
        job.options.extra["ingame_player_voices"] = before is None  # hidden with the voices on as well
        renderer_for(settings, game).render(job)

        for sequence in game.calls[0]["sequences"]:  # every take, without anybody asking
            commands = [a["cmd"] for a in sequence["actions"]]
            assert "csdm_voice_indicator 0" in commands and "csdm_buttons_overlay 0" in commands
        assert seen.pop() == (before or b"")
        assert (path.read_bytes() if path.exists() else None) == before
        assert not plugin.backup_path(path).exists()
    assert list(path.parent.iterdir()) == []


def test_render_starts_the_game_again_after_a_take_that_plays_past_the_end_of_the_match(env):
    from types import SimpleNamespace

    settings, demo, tmp_path = env
    add_plugin(settings)
    game = FakeGame(settings)
    # the match ends at tick 30000; the killer's take and the victim's take for the replay both reach it
    clips = [Clip(segments=[ClipSegment(20000, 20064, STEAM_A)]),
             Clip(segments=[ClipSegment(29952, 30016, STEAM_A), ClipSegment(29920, 29984, STEAM_B)])]
    job = make_job(demo, tmp_path, clips)
    job.analysis = MatchAnalysis.model_construct(tickrate=64, total_ticks=100000,
                                                 rounds=[SimpleNamespace(end_tick=12000, kills=[]),
                                                         SimpleNamespace(end_tick=30000, kills=[])])

    result = renderer_for(settings, game).render(job)

    first, second = game.calls
    starts = [[a["cmd"].split()[1] for s in call["sequences"] for a in s["actions"] if a["cmd"].startswith("startmovie")]
              for call in (first, second)]
    assert starts == [["1-sequence", "3-sequence"], ["2-sequence"]]  # by start tick; the victim's take ends launch 1
    assert [call["sequences"][-1]["actions"][-1]["cmd"] for call in (first, second)] == ["quit", "quit"]
    assert all(call["plugin_installed"] for call in (first, second))
    # one video with the three takes, in the order of the request
    assert result.details["launches"] == 2 and result.details["sequences"] == 3
    assert result.details["frames"] == 90 and result.duration_s == pytest.approx(3.0)
    assert [s["start_tick"] for s in result.details["spans"]] == [20000, 29952, 29920]
    assert result.details["game_seconds"] == pytest.approx(84.0)
    assert result.details["warnings"] == []
    # and the game folder is left as it was found
    assert settings.gameinfo_path.read_bytes() == GAMEINFO and not settings.plugin_install_dir.exists()


def test_render_restores_everything_when_the_game_wrote_nothing(env):
    settings, demo, tmp_path = env
    add_plugin(settings)
    (settings.bin_dir / "csdm.log").write_text("stale")
    game = FakeGame(settings, write=False, returncode=1)
    job = make_job(demo, tmp_path, [Clip(segments=[ClipSegment(8000, 8064, STEAM_A)])])
    with pytest.raises(RenderError) as excinfo:
        renderer_for(settings, game).render(job)
    message = str(excinfo.value)
    assert "wrote no frames for '1-sequence'" in message
    assert "exit code: 1" in message.lower() and "plugin did not load" in message
    assert settings.gameinfo_path.read_bytes() == GAMEINFO
    assert not plugin.backup_path(settings.gameinfo_path).exists()
    assert not settings.plugin_install_dir.exists()
    assert not job.out_path.exists()


def test_render_cleans_raw_frames_when_one_take_is_missing(env):
    settings, demo, tmp_path = env
    add_plugin(settings)
    game = FakeGame(settings, skip_names=("2-sequence",))
    clips = [Clip(segments=[ClipSegment(8000, 8032, STEAM_A)]), Clip(segments=[ClipSegment(9000, 9032, STEAM_A)])]
    with pytest.raises(RenderError, match="2-sequence"):
        renderer_for(settings, game).render(make_job(demo, tmp_path, clips))
    assert not encode.tga_recording_dir(settings).exists()  # gigabytes of frames must not be left behind
    assert settings.gameinfo_path.read_bytes() == GAMEINFO


def test_render_recovers_a_gameinfo_left_patched_by_a_crash(env):
    settings, demo, tmp_path = env
    add_plugin(settings)
    plugin.backup_path(settings.gameinfo_path).write_bytes(GAMEINFO)
    settings.gameinfo_path.write_bytes(plugin.patch_gameinfo_text(GAMEINFO.decode())[0].encode())
    game = FakeGame(settings)
    result = renderer_for(settings, game).render(
        make_job(demo, tmp_path, [Clip(segments=[ClipSegment(8000, 8032, STEAM_A)])]))
    assert game.calls[0]["gameinfo"].count(b"Game\tcsgo/csdm") == 1
    assert settings.gameinfo_path.read_bytes() == GAMEINFO
    assert any("previous run" in w for w in result.details["warnings"])


def test_render_refuses_when_cs2_is_running_steam_is_down_or_plugin_is_missing(env):
    settings, demo, tmp_path = env
    clips = [Clip(segments=[ClipSegment(8000, 8032, STEAM_A)])]
    game = FakeGame(settings)
    with pytest.raises(RenderError, match="static/cs2"):
        renderer_for(settings, game).render(make_job(demo, tmp_path, clips))
    add_plugin(settings)
    with pytest.raises(RenderError, match="already running"):
        renderer_for(settings, game, cs2_running=True).render(make_job(demo, tmp_path, clips))
    with pytest.raises(RenderError, match="Steam is not running"):
        renderer_for(settings, game, steam_running=False).render(make_job(demo, tmp_path, clips))
    with pytest.raises(RenderError, match="Nothing to record"):
        renderer_for(settings, game).render(make_job(demo, tmp_path, [Clip(segments=[ClipSegment(50, 60)])]))
    with pytest.raises(RenderError, match="backend"):
        job = make_job(demo, tmp_path, clips)
        job.options.extra["ingame_backend"] = "obs"
        renderer_for(settings, game).render(job)
    with pytest.raises(RenderError, match="HLAE"):  # known backend, but HLAE is not installed here
        job = make_job(demo, tmp_path, clips)
        job.options.extra["ingame_backend"] = "hlae"
        renderer_for(settings, game).render(job)
    assert game.calls == []  # the game was never started
    assert settings.gameinfo_path.read_bytes() == GAMEINFO


def test_render_fails_fast_when_another_recording_holds_the_lock(env):
    settings, demo, tmp_path = env
    add_plugin(settings)
    game = FakeGame(settings)
    job = make_job(demo, tmp_path, [Clip(segments=[ClipSegment(8000, 8032, STEAM_A)])])
    job.options.extra["ingame_lock_wait_s"] = 0
    with launcher.recording_lock(settings.lock_path):
        with pytest.raises(RenderError, match="Another in-game recording"):
            renderer_for(settings, game).render(job)
    assert game.calls == []


def test_render_propagates_cancellation_and_cleans_up(env):
    settings, demo, tmp_path = env
    add_plugin(settings)

    def cancelled_game(command, **kwargs):
        raise RenderCancelled("Render cancelled; the game was closed.")

    job = make_job(demo, tmp_path, [Clip(segments=[ClipSegment(8000, 8032, STEAM_A)])])
    with pytest.raises(RenderCancelled):
        renderer_for(settings, cancelled_game).render(job)
    assert settings.gameinfo_path.read_bytes() == GAMEINFO
    assert not settings.plugin_install_dir.exists()
    assert not (job.work_dir / "ingame" / "demo.dem.json").exists()


def test_per_job_options_reach_the_actions_file_and_unknown_players_only_warn(env):
    settings, demo, tmp_path = env
    add_plugin(settings)
    game = FakeGame(settings)
    job = make_job(demo, tmp_path, [Clip(segments=[ClipSegment(8000, 8032, "BOT:Hank", speed=0.5)])])
    job.options.extra.update({"ingame_hud": "full", "ingame_xray": True, "ingame_player_voices": "yes",
                              "ingame_commands": "cl_drawhud_force_radar 0; fov_cs_debug 90",
                              "ingame_launch_args": ["-noborder"]})
    result = renderer_for(settings, game).render(job)
    commands = [a["cmd"] for a in game.calls[0]["sequences"][0]["actions"]]
    assert "cl_draw_only_deathnotices 0" in commands and "spec_show_xray 1" in commands
    assert "tv_listen_voice_indices -1" in commands
    assert "cl_drawhud_force_radar 0" in commands and "fov_cs_debug 90" in commands
    assert not any(c.startswith("spec_player") for c in commands)
    assert "-noborder" in game.calls[0]["command"]
    warnings = " ".join(result.details["warnings"])
    assert "BOT:Hank" in warnings and "speed is ignored" in warnings
