"""Recording on a machine that also hosts game servers.

A dedicated server is a ``cs2`` process like the game. It must never be taken for the game (recording would be
refused all day) and above all never be killed when a recording is cancelled or times out. No game is started
here: the process list is faked, except for one check against real, harmless child processes.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from demoreel.render.ingame import procs
from demoreel.render.ingame.renderer import IngameRenderer
from demoreel.render.ingame.settings import IngameSettings

NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}  # no console windows
SERVER = procs.GameProcess(10, r"D:\servers\cs2\game\bin\win64\cs2.exe",
                           r'"D:\servers\cs2\game\bin\win64\cs2.exe" -dedicated -usercon +map de_mirage -port 27015')
GAME = procs.GameProcess(20, r"C:\Steam\steamapps\common\cs2\game\bin\win64\cs2.exe",
                         r'"C:\Steam\...\cs2.exe" -insecure -novid +playdemo x.dem')
UNKNOWN = procs.GameProcess(30)  # started by another user: Windows will not show its command line


@pytest.mark.parametrize(("command_line", "dedicated"), [
    ('"C:\\cs2\\cs2.exe" -dedicated +map de_dust2', True),
    ("cs2.exe -console -DEDICATED -port 27016", True),
    ("/srv/cs2/game/bin/linuxsteamrt64/cs2 -dedicated", True),
    ('"C:\\cs2\\cs2.exe" -insecure +playdemo "C:\\demos\\a-dedicated-fan.dem"', False),
    ("cs2.exe -dedicatedserver", False),
    ("cs2.exe", False),
    ("", False),
    (None, None),
])
def test_a_dedicated_server_is_told_apart_by_its_command_line(command_line, dedicated):
    assert procs.GameProcess(1, None, command_line).dedicated is dedicated


def test_servers_do_not_count_as_the_game_but_what_cannot_be_inspected_does(monkeypatch):
    running: list[procs.GameProcess] | None = []
    monkeypatch.setattr(procs, "cs2_processes", lambda: running)
    assert procs.is_cs2_running() is False and procs.game_pids() == []
    running = [SERVER, procs.GameProcess(11, SERVER.exe, SERVER.command_line)]
    assert procs.is_cs2_running() is False  # two servers up, nobody playing on this PC: recording may start
    assert [p.pid for p in procs.dedicated_servers()] == [10, 11] and procs.game_pids() == []
    running = [SERVER, GAME]
    assert procs.is_cs2_running() is True and procs.game_pids() == [20]
    running = [SERVER, UNKNOWN]
    assert procs.is_cs2_running() is True  # cannot tell: take it for somebody's game and do not record
    assert procs.game_pids() == []  # ...and for the same doubt, never kill it
    if sys.platform == "win32":
        running = None
        assert procs.is_cs2_running() is None and procs.dedicated_servers() == [] and procs.game_pids() == []


class _Proc:
    pid = 999

    def kill(self) -> None:
        pass

    def wait(self, timeout: float | None = None) -> int:
        return 0


def test_closing_the_game_never_touches_a_server(monkeypatch):
    killed: list[int] = []
    commands: list[list[str]] = []
    monkeypatch.setattr(procs, "cs2_processes", lambda: [SERVER, GAME, UNKNOWN])
    monkeypatch.setattr(procs, "_kill_pid", killed.append)
    monkeypatch.setattr(procs.os, "killpg", lambda *a: None, raising=False)
    monkeypatch.setattr(procs.os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(procs.subprocess, "run", lambda cmd, **kw: commands.append(list(cmd)))
    procs.kill_process_tree(_Proc(), also_names=procs.CS2_PROCESS_NAMES)
    assert killed == [GAME.pid]  # the game, and only the game
    assert all("/IM" not in c and "pkill" not in c for c in commands)  # nothing is killed by name any more
    assert all(str(SERVER.pid) not in c and str(UNKNOWN.pid) not in c for c in commands)


def test_closing_the_recording_never_closes_a_game_somebody_opened_meanwhile(monkeypatch):
    """Between the "is CS2 open?" check and the end of a recording, the machine's owner may start the game: a
    timeout or a cancel must close the recording's game only, told apart by the demo it plays."""
    theirs = procs.GameProcess(21, GAME.exe, r'"C:\Steam\steamapps\common\cs2\game\bin\win64\cs2.exe" -steam')
    ours = procs.GameProcess(22, GAME.exe, r'"C:\Steam\...\cs2.exe" -insecure +playdemo '
                                           r'"C:\demoreel\data\tmp\j_1\ingame\demo.dem" -sw')
    killed: list[int] = []
    monkeypatch.setattr(procs, "cs2_processes", lambda: [SERVER, theirs, ours])
    monkeypatch.setattr(procs, "_kill_pid", killed.append)
    monkeypatch.setattr(procs.os, "killpg", lambda *a: None, raising=False)
    monkeypatch.setattr(procs.os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(procs.subprocess, "run", lambda cmd, **kw: None)
    procs.kill_process_tree(_Proc(), also_names=procs.CS2_PROCESS_NAMES,
                            only_with="C:/demoreel/data/tmp/j_1/ingame/demo.dem")  # either kind of slash
    assert killed == [22]
    assert procs.game_pids() == [21, 22] and procs.game_pids("nothing.dem") == []


def test_command_lines_of_real_processes_can_be_read():
    exe, command_line = procs.process_details(os.getpid())
    assert exe and command_line and ("pytest" in command_line or "python" in command_line.lower())
    code = "import time; time.sleep(30)"
    server = subprocess.Popen([sys.executable, "-c", code, "-dedicated", "+map", "de_test"], **NO_WINDOW)
    game = subprocess.Popen([sys.executable, "-c", code, "-insecure"], **NO_WINDOW)
    try:
        assert procs.GameProcess(server.pid, *procs.process_details(server.pid)).dedicated is True
        assert procs.GameProcess(game.pid, *procs.process_details(game.pid)).dedicated is False
        assert server.pid in [pid for pid, _name in procs.running_processes() or []]
    finally:
        server.kill()
        game.kill()
    assert procs.process_details(4_000_000) == (None, None)  # no such process: an answer, not an error


def _settings(tmp_path) -> IngameSettings:
    cs2 = tmp_path / "cs2"
    (cs2 / "game" / "bin" / "win64").mkdir(parents=True)
    return IngameSettings(cs2_dir=cs2, steam_dir=None, steam_exe=None, plugin_dir=tmp_path / "plugins",
                          data_dir=tmp_path / "data" / "ingame", platform=sys.platform)


def test_a_server_running_from_the_folder_being_recorded_from_blocks_recording(tmp_path):
    settings = _settings(tmp_path)
    elsewhere = procs.GameProcess(10, str(tmp_path / "servers" / "cs2" / "game" / "bin" / "win64" / "cs2.exe"),
                                  "cs2.exe -dedicated")
    same = procs.GameProcess(11, str(settings.cs2_dir / "game" / "bin" / "win64" / "cs2.exe"), "cs2.exe -dedicated")

    def renderer(*servers):
        return IngameRenderer(settings, is_cs2_running=lambda: False, is_steam_running=lambda: True,
                              dedicated_servers=lambda: list(servers))

    separate = renderer(elsewhere).availability()
    assert separate.details["dedicated_servers"] == 1 and separate.details["cs2_running"] is False
    assert not any("dedicated game server" in p for p in separate.details["problems"])  # another copy: no concern
    shared = renderer(elsewhere, same).availability()
    problem = next(p for p in shared.details["problems"] if "dedicated game server" in p)
    assert "process 11" in problem and "DEMOREEL_CS2_DIR" in problem and "process 10" not in problem
    assert not shared.ok

    def boom():
        raise RuntimeError("process list exploded")

    broken = IngameRenderer(settings, is_cs2_running=lambda: False, is_steam_running=lambda: True,
                            dedicated_servers=boom).availability()
    assert broken.details["dedicated_servers"] is None  # a broken listing never blocks recording by itself
    assert not any("dedicated game server" in p for p in broken.details["problems"])
