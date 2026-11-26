"""Starting CS2 on a demo and babysitting it until it quits.

Ported from CS Demo Manager (MIT), ``src/node/counter-strike/launcher/start-counter-strike.ts``:

* launch parameters ``-insecure -novid +playdemo "<demo>" -width W -height H -sw`` (video generation always
  uses the windowed display mode and does not ``+exec`` the user's cfg);
* Windows runs ``game/bin/win64/cs2.exe`` directly, Linux runs
  ``SteamLinuxRuntime_sniper/_v2-entry-point --verb=waitforexitandrun -- game/cs2.sh``;
* ``USRLOCALCSGO`` points the game at a private config folder so the user's own settings are not overwritten
  (``define-cfg-folder-location.ts``);
* Steam must be running (``assert-steam-is-running.ts``).

Unlike the reference, an already running CS2 is never killed: recording is refused instead, because it may be
the user's own game. A lock file makes sure only one recording runs per machine.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..base import RecorderBusy, RenderCancelled, RenderError
from . import procs
from .settings import HLAE_HOOK_DLL, IngameSettings

INSECURE_FLAG = "-insecure"  # the plugin refuses to load without it; it also keeps VAC out of the session


def is_basic_latin(text: str) -> bool:
    """Port of ``stringContainsOnlyBasicLatinChars`` (assert-demo-path-is-valid.ts): CS2 fails to play a demo
    whose path has characters outside ASCII."""
    return all(ord(ch) < 128 for ch in text)


def build_launch_args(demo_path: Path | str, width: int, height: int,
                      extra: Sequence[str] = ()) -> list[str]:
    """Game arguments, in the reference's order. ``-insecure`` is always first and cannot be removed."""
    args = [INSECURE_FLAG, "-novid", "+playdemo", str(demo_path)]
    # Not in the reference: by default CS2 sleeps 20 ms per frame while its window is not focused, which caps
    # an unattended recording at ~20 frames per second. (cs-demodesk and CS2-insight-agent do the same.)
    args += ["+engine_no_focus_sleep", "0"]
    args += [a for a in extra if a and a != INSECURE_FLAG]
    args += ["-width", str(int(width)), "-height", str(int(height)), "-sw"]
    return args


def build_command(settings: IngameSettings, game_args: Sequence[str]) -> list[str]:
    """Full argv for ``subprocess.Popen`` (no shell)."""
    if INSECURE_FLAG not in game_args:
        raise RenderError("Refusing to start CS2 without -insecure.")
    if settings.cs2_dir is None:
        raise RenderError("CS2 folder not found.")
    executable = settings.game_executable
    if settings.is_windows:
        return [str(executable), *game_args]
    if settings.is_linux:
        if settings.runtime_script is None:
            raise RenderError("Steam Linux Runtime 'sniper' (_v2-entry-point) not found; install it from Steam "
                              "or set DEMOREEL_INGAME_RUNTIME_SCRIPT.")
        return [str(settings.runtime_script), "--verb=waitforexitandrun", "--", str(executable), *game_args]
    raise RenderError(f"The in-game renderer does not support the platform {settings.platform!r}.")


def build_hlae_command(settings: IngameSettings, game_args: Sequence[str]) -> list[str]:
    """argv that starts CS2 through HLAE (port of ``start-counter-strike-with-hlae.ts``, CS2 branch).

    HLAE.exe is only a loader here: it starts ``cs2.exe`` with the given command line, injects
    ``AfxHookSource2.dll`` (which provides the ``mirv_streams`` recorder) and exits."""
    if INSECURE_FLAG not in game_args:
        raise RenderError("Refusing to start CS2 without -insecure.")
    if not settings.is_windows:
        raise RenderError("The HLAE recording backend is available only on Windows; use the 'startmovie' backend.")
    if settings.cs2_dir is None:
        raise RenderError("CS2 folder not found.")
    hlae = settings.hlae_exe
    if hlae is None or not Path(hlae).is_file():
        raise RenderError("HLAE.exe not found. Download HLAE from https://github.com/advancedfx/advancedfx/releases, "
                          "extract it to <data_dir>/cache/hlae/<version>/ or set DEMOREEL_INGAME_HLAE_EXE.")
    dll = Path(hlae).parent.joinpath(*HLAE_HOOK_DLL)
    if not dll.is_file():
        raise RenderError(f"{dll} is missing: this HLAE version cannot record CS2. Install a current HLAE release.")
    cmdline = " ".join(f'"{a}"' if any(c in a for c in ' \t') else a for a in map(str, game_args))
    return [str(hlae), "-noGui", "-autoStart", "-noConfig", "-afxDisableSteamStorage", "-customLoader",
            "-hookDllPath", str(dll), "-programPath", str(settings.game_executable), "-cmdLine", cmdline]


def register_hlae_ffmpeg(hlae_exe: Path, ffmpeg_exe: str | Path) -> Path:
    """HLAE finds ffmpeg through ``<HLAE folder>/ffmpeg/ffmpeg.ini`` (``registerFfmpegLocation`` in the reference)."""
    ini = Path(hlae_exe).parent / "ffmpeg" / "ffmpeg.ini"
    text = f"[Ffmpeg]\nPath={ffmpeg_exe}"
    try:
        if not ini.is_file() or ini.read_text(encoding="utf-8") != text:
            ini.parent.mkdir(parents=True, exist_ok=True)
            ini.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise RenderError(f"Could not write {ini}: {exc}") from exc
    return ini


def unused_tcp_port() -> int | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])
    except OSError:
        return None


def build_env(settings: IngameSettings, extra: Mapping[str, str] | None = None,
              base: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    env["USRLOCALCSGO"] = str(settings.cfg_dir)
    # The plugin connects to the CS Demo Manager app over a local WebSocket (default port 4574). Point it at a
    # port nobody listens on so a running CSDM app cannot drive our recording session.
    port = unused_tcp_port()
    if port is not None:
        env["CSDM_WS_PORT"] = str(port)
    env.update(extra or {})
    return env


# --- one recording at a time ------------------------------------------------------------------------------


class RecordingLock:
    """Cross-process exclusive lock on a file (``msvcrt.locking`` on Windows, ``fcntl.flock`` elsewhere).
    The OS releases it when the process dies, so a crash never leaves it stuck."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None

    def try_acquire(self) -> bool:
        if self._handle is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")  # noqa: SIM115 - kept open for as long as the lock is held
        try:
            handle.seek(0)
            if sys.platform == "win32":
                import msvcrt  # noqa: PLC0415

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl  # noqa: PLC0415

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            handle.seek(0)
            if sys.platform == "win32":
                import msvcrt  # noqa: PLC0415

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl  # noqa: PLC0415

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            handle.close()

    def is_held_elsewhere(self) -> bool:
        """``True`` when another process (or lock object) holds the lock right now. Never raises."""
        if self._handle is not None:
            return False
        try:
            if not self.path.exists():  # nobody ever recorded here; do not create the file just to probe
                return False
            probe = RecordingLock(self.path)
            if probe.try_acquire():
                probe.release()
                return False
            return True
        except OSError:
            return False


_HELD: dict[tuple[str, int], RecordingLock] = {}  # the locks this process holds, by file and thread
# Asking whether the lock is held (the doctor, an idle worker) takes it for an instant: that is not a recording.
PROBE_GRACE_S = 0.25


BUSY_MESSAGE = ("Another in-game recording is running on this machine (only one CS2 instance can record at a "
                "time). Try again when it is finished.")


@contextmanager
def recording_lock(path: Path, wait_s: float = 0.0, cancelled: Callable[[], bool] = lambda: False,
                   on_wait: Callable[[float], None] = lambda waited: None,
                   poll_s: float = 1.0, busy_message: str = BUSY_MESSAGE) -> Iterator[RecordingLock]:
    """Hold this machine's one-recording-at-a-time lock, waiting up to ``wait_s`` for another process to let
    go of it (:class:`RecorderBusy` after that).

    Re-entrant: code that already holds it enters again at once. A job that records several times in a row
    takes it once around all of them, so no other recording gets the game in between."""
    key = (os.path.normcase(os.path.abspath(path)), threading.get_ident())
    held = _HELD.get(key)
    if held is not None:
        yield held
        return
    lock = RecordingLock(path)
    started = time.monotonic()
    while not lock.try_acquire():
        waited = time.monotonic() - started
        if cancelled():
            raise RenderCancelled("Render cancelled while waiting for another in-game recording.")
        if waited >= max(wait_s, PROBE_GRACE_S):
            raise RecorderBusy(busy_message)
        if waited < PROBE_GRACE_S:
            time.sleep(0.05)
            continue
        on_wait(waited)
        time.sleep(min(poll_s, max(0.05, wait_s - waited)))
    _HELD[key] = lock
    try:
        yield lock
    finally:
        _HELD.pop(key, None)
        lock.release()


# --- running the game ---------------------------------------------------------------------------------------


class LoaderFailed(RenderError):
    """The loader (HLAE) could not start the game, or the game never appeared: HLAE's problem, not the game's."""


@dataclass
class GameRun:
    returncode: int
    duration_s: float
    output_tail: str = ""


def assert_can_start(is_cs2_running: Callable[[], bool | None] = procs.is_cs2_running,
                     is_steam_running: Callable[[], bool | None] = procs.is_steam_running) -> None:
    if is_cs2_running():
        raise RenderError("CS2 is already running. Close the game before recording (demoreel never kills a "
                          "game it did not start). Dedicated servers do not count; a cs2 process started by "
                          "another user or as administrator cannot be told apart and is taken for the game.")
    if is_steam_running() is False:
        raise RenderError("Steam is not running. Start Steam and log in, then try again.")


def _tail(path: Path | None, max_chars: int = 2000) -> str:
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-max_chars:].strip()
    except OSError:
        return ""


def _start(command: Sequence[str], env: Mapping[str, str] | None, cwd: Path | None, log_handle,
           container: procs.Containment) -> subprocess.Popen:
    """``Popen`` with the process group of :func:`procs.popen_group_kwargs` and ``container``'s additions; the
    container sees the process before anything else happens to it."""
    proc = subprocess.Popen(
        list(command), env=dict(env) if env is not None else None, cwd=str(cwd) if cwd else None,
        stdin=subprocess.DEVNULL, stdout=log_handle or subprocess.DEVNULL, stderr=subprocess.STDOUT,
        **container.popen_kwargs(procs.popen_group_kwargs()))
    try:
        container.started(proc)
    except OSError:
        procs.kill_process_tree(proc)
        raise
    return proc


def run_game(command: Sequence[str], *, env: Mapping[str, str] | None = None, cwd: Path | None = None,
             timeout_s: float, cancelled: Callable[[], bool] = lambda: False,
             on_poll: Callable[[float], None] = lambda elapsed: None, log_path: Path | None = None,
             poll_s: float = 0.5, kill_names: Sequence[str] = (), kill_marker: str | None = None,
             contain: Callable[[], procs.Containment] = procs.DieWithParent) -> GameRun:
    """Start ``command`` and wait for it to exit.

    * ``on_poll(elapsed_s)`` is called every ``poll_s``; it reports progress and may raise ``RenderError`` to
      abort (stall detection). Any exception kills the process tree before propagating.
    * ``cancelled()`` returning true kills the tree and raises :class:`RenderCancelled`.
    * after ``timeout_s`` the watchdog kills the tree and raises :class:`RenderError`.
    * ``contain()`` ties the process to this one (:class:`procs.DieWithParent`: the game dies with demoreel).
    """
    log_handle = None
    container = contain()
    try:
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_handle = open(log_path, "wb")  # noqa: SIM115 - closed in the finally below
        try:
            proc = _start(command, env, cwd, log_handle, container)
        except OSError as exc:
            hint = " (Access is denied: check the antivirus / folder permissions)" if isinstance(
                exc, PermissionError) else ""
            raise RenderError(f"Could not start the game: {exc}{hint}") from exc

        started = time.monotonic()
        try:
            while True:
                returncode = proc.poll()
                elapsed = time.monotonic() - started
                if returncode is not None:
                    break
                if cancelled():
                    raise RenderCancelled("Render cancelled; the game was closed.")
                if elapsed > timeout_s:
                    raise RenderError(f"The game did not finish within {int(timeout_s)} s and was killed "
                                      f"(watchdog). {_tail(log_path, 600)}".strip())
                on_poll(elapsed)
                time.sleep(poll_s)
        except BaseException:
            container.kill()
            procs.kill_process_tree(proc, also_names=kill_names, only_with=kill_marker)
            raise
    finally:
        container.close()
        if log_handle is not None:
            log_handle.close()
    return GameRun(returncode=int(returncode), duration_s=elapsed, output_tail=_tail(log_path))


def run_loader(command: Sequence[str], *, env: Mapping[str, str] | None = None, cwd: Path | None = None,
               timeout_s: float, cancelled: Callable[[], bool] = lambda: False,
               on_poll: Callable[[float], None] = lambda elapsed: None, log_path: Path | None = None,
               poll_s: float = 1.0, kill_names: Sequence[str] = (), kill_marker: str | None = None,
               is_game_running: Callable[[], bool | None] = procs.is_cs2_running,
               appear_timeout_s: float = 120.0,
               contain: Callable[[], procs.Containment] = procs.DieWithParent) -> GameRun:
    """Like :func:`run_game` for a *loader* (HLAE): ``command`` starts the game and exits, so the session lasts
    until the game process itself is gone. Same progress / cancellation / watchdog contract. The game is
    started by the loader, so it is in the loader's Job Object on Windows and dies with demoreel as well."""
    log_handle = None
    container = contain()
    try:
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_handle = open(log_path, "wb")  # noqa: SIM115 - closed in the finally below
        try:
            proc = _start(command, env, cwd, log_handle, container)
        except OSError as exc:
            raise RenderError(f"Could not start HLAE: {exc}") from exc

        started = time.monotonic()
        seen = False
        try:
            while True:
                elapsed = time.monotonic() - started
                running = bool(is_game_running())
                seen = seen or running
                if seen and not running:
                    break
                loader_code = proc.poll()
                if not seen and loader_code not in (None, 0):
                    raise LoaderFailed(f"HLAE could not start the game (exit code {loader_code}). "
                                      f"{_tail(log_path, 600)}".strip())
                if not seen and elapsed > appear_timeout_s:
                    raise LoaderFailed(f"HLAE was started but CS2 did not appear within {int(appear_timeout_s)} s. "
                                      f"{_tail(log_path, 600)}".strip())
                if cancelled():
                    raise RenderCancelled("Render cancelled; the game was closed.")
                if elapsed > timeout_s:
                    raise RenderError(f"The game did not finish within {int(timeout_s)} s and was killed (watchdog).")
                on_poll(elapsed)
                time.sleep(poll_s)
        except BaseException:
            container.kill()
            procs.kill_process_tree(proc, also_names=kill_names, only_with=kill_marker)
            raise
    finally:
        container.close()
        if log_handle is not None:
            log_handle.close()
    return GameRun(returncode=0, duration_s=elapsed, output_tail=_tail(log_path))
