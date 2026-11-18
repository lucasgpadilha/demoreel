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
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Same names CS Demo Manager looks for (src/node/counter-strike/is-steam-running.ts and
# get-counter-strike-process-names.ts). Compared lower-case.
STEAM_PROCESS_NAMES = ("steam.exe", "steamchina.exe") if sys.platform == "win32" else ("steam",)
CS2_PROCESS_NAMES = ("cs2.exe",) if sys.platform == "win32" else ("cs2",)
_DEDICATED = re.compile(r"(?<!\S)-dedicated(?!\S)", re.IGNORECASE)


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


def game_pids() -> list[int]:
    """Process ids that are certainly the game: ``cs2`` processes whose command line was read and is not a
    dedicated server's. What cannot be inspected is left out - and so never killed."""
    return [p.pid for p in cs2_processes() or [] if p.dedicated is False]


def popen_group_kwargs() -> dict:
    """``subprocess.Popen`` keyword arguments that make :func:`kill_process_tree` reliable."""
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def _kill_pid(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=30,
                       creationflags=_NO_WINDOW)
    else:
        os.kill(pid, signal.SIGKILL)


def kill_process_tree(proc: subprocess.Popen, also_names: Iterable[str] = ()) -> None:
    """Kill ``proc`` and its descendants. ``also_names`` are image names killed afterwards as a safety net
    (a loader such as HLAE starts the game and exits; on Linux the game runs inside the Steam runtime container
    and may outlive the wrapper script).

    For the game's own name that safety net is narrowed to the game: dedicated servers running under the same
    image name, and any ``cs2`` process that cannot be inspected, are left alone."""
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
                for pid in game_pids():
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
