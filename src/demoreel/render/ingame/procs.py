"""Process helpers without ``psutil``: list running processes and kill a process tree.

Windows uses the Toolhelp snapshot API through ``ctypes`` (no console window, ~1 ms) and falls back to
``tasklist``; Linux reads ``/proc`` and falls back to ``pgrep``. Nothing here raises.

A machine that hosts game servers runs ``cs2 -dedicated`` all day under the same image name as the game.
Those are **never** taken for the game and never killed: everything here that looks for "CS2" means the
game client, told apart by its command line.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Same names CS Demo Manager looks for (src/node/counter-strike/is-steam-running.ts and
# get-counter-strike-process-names.ts). Compared lower-case.
STEAM_PROCESS_NAMES = ("steam.exe", "steamchina.exe") if sys.platform == "win32" else ("steam",)
CS2_PROCESS_NAMES = ("cs2.exe",) if sys.platform == "win32" else ("cs2",)
_DEDICATED = re.compile(r"(?<!\S)-dedicated(?!\S)", re.IGNORECASE)
# Anti-cheats that run outside a match and refuse HLAE the access it needs to start CS2 with its hook (HLAE
# shows "the loader could not create the requested process", GetLastWin32Error = 5). Image name -> its name.
ANTI_CHEAT_PROCESSES = {"faceitclient.exe": "FACEIT Anti-Cheat", "faceitservice.exe": "FACEIT Anti-Cheat"}


@dataclass(frozen=True)
class GameProcess:
    """One running ``cs2`` process. ``command_line`` / ``exe`` are ``None`` when they could not be read
    (the process belongs to another user or runs elevated)."""

    pid: int
    exe: str | None = None
    command_line: str | None = None

    @property
    def dedicated(self) -> bool | None:
        """``True`` for a dedicated game server, ``False`` for the game, ``None`` when it cannot be told."""
        if self.command_line is None:
            return None
        return bool(_DEDICATED.search(self.command_line))


def _windows_processes() -> list[tuple[int, str]]:
    import ctypes
    from ctypes import wintypes

    class ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if snapshot is None or snapshot == wintypes.HANDLE(-1).value:
        raise OSError("CreateToolhelp32Snapshot failed")
    found: list[tuple[int, str]] = []
    try:
        entry = ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(ProcessEntry32W)
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            found.append((int(entry.th32ProcessID), entry.szExeFile.lower()))
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return found


def _tasklist_processes() -> list[tuple[int, str]]:
    out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=20,
                         creationflags=_NO_WINDOW).stdout
    found = []
    for line in out.splitlines():
        cells = line.strip().strip('"').split('","')
        if len(cells) >= 2 and cells[1].isdigit():
            found.append((int(cells[1]), cells[0].lower()))
    return found


def _proc_processes() -> list[tuple[int, str]]:
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            name = (entry / "comm").read_text(encoding="utf-8", errors="replace").strip().lower()
        except OSError:
            continue  # the process ended, or it belongs to someone else
        found.append((int(entry.name), name))
    return found


def running_processes() -> list[tuple[int, str]] | None:
    """``(pid, lower-case image name)`` of every running process, or ``None`` when they cannot be listed."""
    strategies = ((_windows_processes, _tasklist_processes) if sys.platform == "win32" else (_proc_processes,))
    for strategy in strategies:
        try:
            found = strategy()
            if found:
                return found
        except Exception:  # noqa: BLE001 - try the next strategy
            continue
    return None


def running_process_names() -> set[str] | None:
    """Lower-case image names of every running process, or ``None`` when they cannot be listed."""
    found = running_processes()
    return None if found is None else {name for _pid, name in found}


def running_anti_cheats(names: set[str] | None = None) -> list[str]:
    """The anti-cheats of :data:`ANTI_CHEAT_PROCESSES` that are running; empty when none is, or when the
    processes cannot be listed."""
    names = running_process_names() if names is None else names
    return sorted({label for image, label in ANTI_CHEAT_PROCESSES.items() if image in (names or ())})


def _windows_details(pid: int) -> tuple[str | None, str | None]:
    import ctypes
    from ctypes import wintypes

    class UnicodeString(ctypes.Structure):
        _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT), ("Buffer", ctypes.c_void_p)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    ntdll = ctypes.WinDLL("ntdll")  # type: ignore[attr-defined]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                    ctypes.POINTER(wintypes.DWORD)]
    ntdll.NtQueryInformationProcess.restype = ctypes.c_long
    ntdll.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.ULONG,
                                                ctypes.POINTER(wintypes.ULONG)]
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None, None
    exe = command_line = None
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            exe = buffer.value
        needed = wintypes.ULONG(0)
        ntdll.NtQueryInformationProcess(handle, 60, None, 0, ctypes.byref(needed))  # ProcessCommandLineInformation
        if needed.value:
            raw = ctypes.create_string_buffer(needed.value)
            if ntdll.NtQueryInformationProcess(handle, 60, raw, needed.value, ctypes.byref(needed)) == 0:
                text = UnicodeString.from_buffer(raw)
                command_line = ctypes.wstring_at(text.Buffer, text.Length // 2) if text.Buffer else ""
    finally:
        kernel32.CloseHandle(handle)
    return exe, command_line


def _proc_details(pid: int) -> tuple[str | None, str | None]:
    exe = command_line = None
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        pass
    try:
        parts = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        command_line = " ".join(part.decode("utf-8", "replace") for part in parts if part)
    except OSError:
        pass
    return exe, command_line


def process_details(pid: int) -> tuple[str | None, str | None]:
    """``(executable path, command line)`` of a running process; ``None`` for what cannot be read."""
    try:
        return _windows_details(pid) if sys.platform == "win32" else _proc_details(pid)
    except Exception:  # noqa: BLE001 - "cannot tell" is an answer here, never an error
        return None, None


def cs2_processes() -> list[GameProcess] | None:
    """Every running ``cs2`` process - games and dedicated servers alike - or ``None`` when processes cannot
    be listed."""
    found = running_processes()
    if found is None:
        return None
    return [GameProcess(pid, *process_details(pid)) for pid, name in found if name in CS2_PROCESS_NAMES]


def _pgrep(name: str) -> bool:
    try:
        return subprocess.run(["pgrep", "-x", name], capture_output=True, timeout=10).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def is_any_running(names: Iterable[str]) -> bool | None:
    """``True``/``False`` when it could be determined, ``None`` when the process list is unavailable."""
    wanted = {n.lower() for n in names}
    running = running_process_names()
    if running is not None:
        return bool(wanted & running)
    if sys.platform != "win32":
        return any(_pgrep(n) for n in wanted)
    return None


def is_steam_running() -> bool | None:
    return is_any_running(STEAM_PROCESS_NAMES)


def is_cs2_running() -> bool | None:
    """Is the **game** running? Dedicated servers do not count. A ``cs2`` process that cannot be inspected is
    taken for the game: recording next to somebody's match is the mistake to avoid."""
    found = cs2_processes()
    if found is None:
        return any(_pgrep(n) for n in CS2_PROCESS_NAMES) if sys.platform != "win32" else None
    return any(p.dedicated is not True for p in found)


def dedicated_servers() -> list[GameProcess]:
    """The dedicated game servers running on this machine (empty when processes cannot be listed)."""
    return [p for p in cs2_processes() or [] if p.dedicated is True]


def _comparable(text: str) -> str:
    return text.replace("\\", "/").replace('"', "").lower()


def game_pids(marker: str | None = None) -> list[int]:
    """Process ids that are certainly the game: ``cs2`` processes whose command line was read and is not a
    dedicated server's. What cannot be inspected is left out - and so never killed.

    ``marker``: only games whose command line contains it (a recording passes the path of the demo it plays,
    which is unique to its job), so a game somebody opened meanwhile is not taken for the recording's."""
    wanted = _comparable(marker) if marker else None
    return [p.pid for p in cs2_processes() or [] if p.dedicated is False
            and (wanted is None or wanted in _comparable(p.command_line or ""))]


@dataclass(frozen=True)
class TopWindow:
    """A top-level window as :func:`top_windows` sees it."""

    pid: int
    class_name: str
    visible: bool
    title: str
    texts: tuple[str, ...] = ()  # the text of its static controls (a message box's message)


def dialog_texts(windows: Iterable[TopWindow], pids: set[int]) -> list[str]:
    """``"<title>: <text>"`` of the visible dialog boxes (window class ``#32770``) of these processes."""
    out = []
    for w in windows:
        if w.pid in pids and w.visible and w.class_name == "#32770":
            text = " ".join(t for t in w.texts if t)
            out.append(f"{w.title}: {text}" if text else w.title or "(a dialog without text)")
    return out


def top_windows(pids: set[int]) -> list[TopWindow]:
    """The top-level windows of these processes, with the static texts of their dialogs. Windows only."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [enum_proc, wintypes.LPARAM]
    user32.EnumChildWindows.argtypes = [wintypes.HWND, enum_proc, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, ctypes.c_void_p,
                                           wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
    user32.SendMessageTimeoutW.restype = ctypes.c_size_t

    def text_of(hwnd) -> str:
        # WM_GETTEXT, not GetWindowText: only the message reaches controls of another process. With a timeout,
        # so a hung game cannot hang the recorder.
        buffer = ctypes.create_unicode_buffer(1024)
        result = ctypes.c_size_t(0)
        if user32.SendMessageTimeoutW(hwnd, 0x000D, 1024, buffer, 0x0002, 500, ctypes.byref(result)):
            return buffer.value.strip()
        return ""

    def class_of(hwnd) -> str:
        buffer = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, buffer, 64)
        return buffer.value

    found: list[TopWindow] = []

    def each_window(hwnd, _lparam) -> bool:
        try:
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value not in pids:
                return True
            cls, visible = class_of(hwnd), bool(user32.IsWindowVisible(hwnd))
            lines: list[str] = []
            if visible and cls == "#32770":  # a dialog: read its message too

                def each_child(child, _lp) -> bool:
                    if class_of(child) == "Static":
                        line = text_of(child)
                        if line:
                            lines.append(line)
                    return True

                user32.EnumChildWindows(hwnd, enum_proc(each_child), 0)
            found.append(TopWindow(pid=pid.value, class_name=cls, visible=visible, title=text_of(hwnd),
                                   texts=tuple(lines)))
        except Exception:  # noqa: BLE001 - one odd window must not stop the search
            pass
        return True

    user32.EnumWindows(enum_proc(each_window), 0)
    return found


def dialogs_of(pids: Iterable[int],
               windows: Callable[[set[int]], Iterable[TopWindow]] | None = None) -> list[str]:
    """``"<title>: <text>"`` of every dialog box (an error box, typically) shown by these processes. Windows
    only (``windows`` replaces the real window list, for tests); elsewhere, and when windows cannot be listed,
    ``[]``. Never raises."""
    wanted = {int(p) for p in pids}
    if not wanted or (windows is None and sys.platform != "win32"):
        return []
    try:
        return dialog_texts((windows or top_windows)(wanted), wanted)
    except Exception:  # noqa: BLE001
        return []


def game_dialog(marker: str | None) -> str | None:
    """The first dialog box shown by the recording's game (see :func:`game_pids`), or ``None``. A game that
    shows one - HLAE's hook failing after a CS2 update does - waits for a click that never comes."""
    found = dialogs_of(game_pids(marker))
    return found[0] if found else None


def popen_group_kwargs() -> dict:
    """``subprocess.Popen`` keyword arguments that make :func:`kill_process_tree` reliable. On Windows also
    ``CREATE_NO_WINDOW``: the game and HLAE are window programs, for which it changes nothing, while a console
    program started this way (a test's stand-in for the game) does not open a console window."""
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | _NO_WINDOW}
    return {"start_new_session": True}


# --- the game dies with demoreel ------------------------------------------------------------------------------

class Containment:
    """How a started process is tied to demoreel. The default ties nothing; :class:`DieWithParent` makes the
    game die with the process that started it. Injected into ``launcher.run_game`` so tests can watch it."""

    def popen_kwargs(self, base: dict) -> dict:
        """``base`` (the ``Popen`` keyword arguments) with whatever this containment needs added."""
        return base

    def started(self, proc: subprocess.Popen) -> None:
        """Called right after ``Popen``, before anything else happens to the process."""

    def kill(self) -> None:
        """End every process that is contained (the game and whatever it started)."""

    def close(self) -> None:
        """The session is over; let go of what was held."""


_CREATE_SUSPENDED = 0x00000004
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_PROCESS_TERMINATE, _PROCESS_SET_QUOTA, _PROCESS_SUSPEND_RESUME = 0x0001, 0x0100, 0x0800
_PR_SET_PDEATHSIG = 1


def _windows_job() -> tuple:
    """``(kernel32, ntdll, job handle)``: a Job Object whose processes are all killed when its last handle is
    closed. Only this process holds the handle, and Windows closes it when this process ends - however it
    ends: Ctrl+C, the console window closed, TerminateProcess, a crash."""
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount",
            "WriteTransferCount", "OtherTransferCount")]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    ntdll = ctypes.WinDLL("ntdll")  # type: ignore[attr-defined]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    ntdll.NtResumeProcess.restype = ctypes.c_long

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObject failed")
    info = ExtendedLimits()
    # BREAKAWAY_OK: a child that explicitly asks to leave the job may (instead of failing to start). HLAE's
    # loader is not known to ask; if it did, the game would simply not be contained, as before.
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | _JOB_OBJECT_LIMIT_BREAKAWAY_OK
    if not kernel32.SetInformationJobObject(job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info),
                                            ctypes.sizeof(info)):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        raise OSError(error, "SetInformationJobObject failed")
    return kernel32, ntdll, job


class DieWithParent(Containment):
    """The game - and on Windows whatever it starts - dies with the demoreel process that started it, so a
    worker that is killed never leaves CS2 recording on its own.

    * Windows: the process is created suspended, put in a Job Object with ``KILL_ON_JOB_CLOSE``, then resumed
      (so nothing it starts can escape the job before it is in it). Processes it starts are in the job too.
    * Linux: ``PR_SET_PDEATHSIG`` = ``SIGKILL`` in the child before it runs the command. It reaches the direct
      child (the Steam runtime's entry point); what that one starts inside its container is up to it.

    Anything that goes wrong here is logged and the process runs uncontained, as it always did: containment
    must never be the reason a recording does not start."""

    def __init__(self, platform: str = sys.platform) -> None:
        self.platform = platform
        self._job = None
        self._kernel32 = self._ntdll = None
        self._suspended = False

    def popen_kwargs(self, base: dict) -> dict:
        out = dict(base)
        if self.platform == "win32":
            try:
                self._kernel32, self._ntdll, self._job = _windows_job()
            except Exception as exc:  # noqa: BLE001
                _log_containment(f"no Job Object ({exc})")
                return out
            out["creationflags"] = out.get("creationflags", 0) | _CREATE_SUSPENDED
            self._suspended = True
        elif self.platform.startswith("linux"):
            try:
                import ctypes

                prctl = ctypes.CDLL(None, use_errno=True).prctl  # looked up here, not in the child
                parent = os.getpid()

                def die_with_parent() -> None:  # runs in the child, between fork and exec: keep it tiny
                    prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
                    if os.getppid() != parent:  # the parent died before the line above: do not start at all
                        os._exit(1)

                out["preexec_fn"] = die_with_parent
            except Exception as exc:  # noqa: BLE001 - no libc prctl (not glibc/musl?)
                _log_containment(f"no PR_SET_PDEATHSIG ({exc})")
        return out

    def started(self, proc: subprocess.Popen) -> None:
        if self.platform != "win32" or not self._suspended:
            return
        import ctypes

        k32, ntdll = self._kernel32, self._ntdll
        handle = k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE | _PROCESS_SUSPEND_RESUME, False, proc.pid)
        if not handle:
            error = ctypes.get_last_error()
            proc.kill()  # created suspended and cannot be resumed: it would hang there forever
            raise OSError(error, f"cannot open the process it just started (pid {proc.pid})")
        try:
            if self._job and not k32.AssignProcessToJobObject(self._job, handle):
                _log_containment(f"AssignProcessToJobObject failed ({ctypes.get_last_error()})")
            status = ntdll.NtResumeProcess(handle)
            if status != 0:
                proc.kill()
                raise OSError(status, f"cannot resume the process it just started (NTSTATUS {status:#x})")
        finally:
            k32.CloseHandle(handle)

    def kill(self) -> None:
        if self._job and self._kernel32 is not None:
            self._kernel32.TerminateJobObject(self._job, 1)

    def close(self) -> None:
        job, self._job = self._job, None
        if job and self._kernel32 is not None:
            self._kernel32.CloseHandle(job)  # kills what is still in the job (nothing, after a normal end)


def _log_containment(what: str) -> None:
    import logging

    logging.getLogger("demoreel.ingame").warning("The game will not be closed if demoreel dies: %s", what)


def _kill_pid(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=30,
                       creationflags=_NO_WINDOW)
    else:
        os.kill(pid, signal.SIGKILL)


def kill_process_tree(proc: subprocess.Popen, also_names: Iterable[str] = (), only_with: str | None = None) -> None:
    """Kill ``proc`` and its descendants. ``also_names`` are image names killed afterwards as a safety net
    (a loader such as HLAE starts the game and exits; on Linux the game runs inside the Steam runtime container
    and may outlive the wrapper script).

    For the game's own name that safety net is narrowed to the game: dedicated servers running under the same
    image name, and any ``cs2`` process that cannot be inspected, are left alone; with ``only_with`` (the demo
    the recording plays) also every game that was not started with it - demoreel never closes a game it did
    not start."""
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=30,
                           creationflags=_NO_WINDOW)
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    except Exception:  # noqa: BLE001 - fall through to the direct kill below
        pass
    try:
        proc.kill()
    except Exception:  # noqa: BLE001 - already gone
        pass
    for name in also_names:
        try:
            if name.lower() in CS2_PROCESS_NAMES:
                for pid in game_pids(only_with):
                    try:
                        _kill_pid(pid)
                    except Exception:  # noqa: BLE001 - already gone
                        pass
            elif sys.platform == "win32":
                subprocess.run(["taskkill", "/IM", name, "/F"], capture_output=True, timeout=30,
                               creationflags=_NO_WINDOW)
            else:
                subprocess.run(["pkill", "-9", "-x", name], capture_output=True, timeout=30)
        except Exception:  # noqa: BLE001
            pass
    try:
        proc.wait(timeout=15)
    except Exception:  # noqa: BLE001
        pass
