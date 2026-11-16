"""Process helpers without ``psutil``: list running image names and kill a process tree.

Windows uses the Toolhelp snapshot API through ``ctypes`` (no console window, ~1 ms) and falls back to
``tasklist``; Linux reads ``/proc`` and falls back to ``pgrep``. Nothing here raises.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Same names CS Demo Manager looks for (src/node/counter-strike/is-steam-running.ts and
# get-counter-strike-process-names.ts). Compared lower-case.
STEAM_PROCESS_NAMES = ("steam.exe", "steamchina.exe") if sys.platform == "win32" else ("steam",)
CS2_PROCESS_NAMES = ("cs2.exe",) if sys.platform == "win32" else ("cs2",)


def _windows_process_names() -> set[str]:
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
    names: set[str] = set()
    try:
        entry = ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(ProcessEntry32W)
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            names.add(entry.szExeFile.lower())
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return names


def _tasklist_process_names() -> set[str]:
    out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=20,
                         creationflags=_NO_WINDOW).stdout
    return {line.split('","')[0].strip('"').lower() for line in out.splitlines() if line.startswith('"')}


def _proc_process_names() -> set[str]:
    names: set[str] = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            names.add((entry / "comm").read_text(encoding="utf-8", errors="replace").strip().lower())
        except OSError:
            continue  # the process ended, or it belongs to someone else
    return names


def running_process_names() -> set[str] | None:
    """Lower-case image names of every running process, or ``None`` when they cannot be listed."""
    strategies = ((_windows_process_names, _tasklist_process_names) if sys.platform == "win32"
                  else (_proc_process_names,))
    for strategy in strategies:
        try:
            names = strategy()
            if names:
                return names
        except Exception:  # noqa: BLE001 - try the next strategy
            continue
    return None


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
    return is_any_running(CS2_PROCESS_NAMES)


def popen_group_kwargs() -> dict:
    """``subprocess.Popen`` keyword arguments that make :func:`kill_process_tree` reliable."""
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def kill_process_tree(proc: subprocess.Popen, also_names: Iterable[str] = ()) -> None:
    """Kill ``proc`` and its descendants. ``also_names`` are image names killed afterwards as a safety net
    (on Linux the game runs inside the Steam runtime container and may outlive the wrapper script)."""
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
            if sys.platform == "win32":
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
