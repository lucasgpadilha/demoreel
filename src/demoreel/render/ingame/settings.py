"""Settings of the in-game renderer: where Steam, CS2 and the plugin binaries are.

Everything is read from the environment when :func:`load` is called (never at import time) and falls back to
auto-detection that mirrors CS Demo Manager's ``src/node/counter-strike/steam-folders.mts``:

* Windows: registry ``HKCU\\Software\\Valve\\Steam`` -> ``SteamPath``
* Linux: ``~/.steam/steam``, the snap location, then ``~/.local/share/Steam``
* then ``<steam>/steamapps/libraryfolders.vdf`` -> the library that owns app 730 ->
  ``steamapps/common/Counter-Strike Global Offensive``

Detection never raises: a missing piece is simply ``None`` and reported by ``IngameRenderer.availability()``.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CS2_APP_ID = "730"
CS2_FOLDER_NAME = "Counter-Strike Global Offensive"

ENV_CS2_DIR = "DEMOREEL_CS2_DIR"
ENV_STEAM_EXE = "DEMOREEL_STEAM_EXE"
ENV_PLUGIN_DIR = "DEMOREEL_INGAME_PLUGIN_DIR"
ENV_PLUGIN_VERSION = "DEMOREEL_INGAME_PLUGIN_VERSION"
ENV_TIMEOUT_S = "DEMOREEL_INGAME_TIMEOUT_S"
ENV_RUNTIME_SCRIPT = "DEMOREEL_INGAME_RUNTIME_SCRIPT"
ENV_BACKEND = "DEMOREEL_INGAME_BACKEND"  # auto (default: hlae where it is installed, else startmovie) | startmovie | hlae
ENV_HLAE_ENCODER = "DEMOREEL_INGAME_HLAE_ENCODER"  # x264 (default) | nvenc
ENV_HLAE_EXE = "DEMOREEL_INGAME_HLAE_EXE"  # HLAE.exe; default: highest version in <data_dir>/cache/hlae/*/

# HLAE's hook for CS2; a folder without it cannot record CS2 (an old release, or a partial extraction).
HLAE_HOOK_DLL = ("x64", "AfxHookSource2.dll")


def version_key(name: str) -> tuple[int, tuple[int, ...]]:
    """Sort key of a folder named after a version: ``2.193.10`` after ``2.193.9``; a name without any number
    (``latest``, ``backup``) before every version."""
    numbers = tuple(int(x) for x in re.findall(r"\d+", name))
    return (1, numbers) if numbers else (0, ())


@dataclass(frozen=True)
class HlaeInstall:
    exe: Path  # .../HLAE.exe
    usable: bool  # x64/AfxHookSource2.dll is next to it

    @property
    def version(self) -> str:
        """The folder name (``2.193.2``), which is how releases are extracted under ``cache/hlae``."""
        return self.exe.parent.name

    @property
    def hook_dll(self) -> Path:
        return self.exe.parent.joinpath(*HLAE_HOOK_DLL)


def hlae_installs(root: Path) -> list[HlaeInstall]:
    """Every ``<root>/<version>/HLAE.exe``, best first: usable ones (with the CS2 hook) by version number, then
    the rest. Never raises."""
    found: list[HlaeInstall] = []
    try:
        for exe in Path(root).glob("*/HLAE.exe"):
            if exe.is_file():
                found.append(HlaeInstall(exe=exe, usable=exe.parent.joinpath(*HLAE_HOOK_DLL).is_file()))
    except OSError:
        return []
    return sorted(found, key=lambda h: (h.usable, version_key(h.version)), reverse=True)


@dataclass(frozen=True)
class IngameSettings:
    cs2_dir: Path | None  # ".../steamapps/common/Counter-Strike Global Offensive"
    steam_dir: Path | None
    steam_exe: Path | None
    plugin_dir: Path  # where the user drops the CS Demo Manager plugin binaries
    data_dir: Path  # <data_dir>/ingame: lock file and the CS2 user config used while recording
    plugin_version: str | None = None  # "latest", "14168"... None = pick from the installed CS2 build
    timeout_s: float | None = None  # hard cap for one game session; None = derived from the clips
    runtime_script: Path | None = None  # Linux: SteamLinuxRuntime_sniper/_v2-entry-point override
    platform: str = sys.platform
    backend: str = "auto"  # recording system used when a render does not name one
    hlae_exe: Path | None = None  # HLAE.exe (Windows only), for the "hlae" backend
    hlae_encoder: str = "x264"  # encoder HLAE pipes the frames into

    @property
    def hlae_available(self) -> bool:
        """HLAE.exe is there *with* the CS2 hook: without it ``auto`` would pick a backend that cannot work."""
        try:
            return (self.platform == "win32" and self.hlae_exe is not None and self.hlae_exe.is_file()
                    and self.hlae_exe.parent.joinpath(*HLAE_HOOK_DLL).is_file())
        except OSError:
            return False

    @property
    def hlae_version(self) -> str | None:
        """The HLAE release in use, from its folder name (``.../hlae/2.193.2/HLAE.exe``), when it reads as one."""
        if self.hlae_exe is None:
            return None
        name = self.hlae_exe.parent.name
        return name if re.search(r"\d", name) else None

    @property
    def hlae_root(self) -> Path:
        """Where HLAE releases are looked for: ``<data_dir>/cache/hlae``."""
        return self.data_dir.parent / "cache" / "hlae"

    @property
    def is_windows(self) -> bool:
        return self.platform == "win32"

    @property
    def is_linux(self) -> bool:
        return self.platform.startswith("linux")

    # --- paths inside the CS2 install (only meaningful when cs2_dir is set) --------------------------
    def _cs2(self) -> Path:
        if self.cs2_dir is None:
            raise ValueError("CS2 folder unknown")
        return self.cs2_dir

    @property
    def csgo_dir(self) -> Path:
        return self._cs2() / "game" / "csgo"

    @property
    def gameinfo_path(self) -> Path:
        return self.csgo_dir / "gameinfo.gi"

    @property
    def plugin_install_dir(self) -> Path:
        """``game/csgo/csdm``: the folder added to the search paths, also where ``startmovie`` writes frames."""
        return self.csgo_dir / "csdm"

    @property
    def bin_dir(self) -> Path:
        return self._cs2() / "game" / "bin" / ("win64" if self.is_windows else "linuxsteamrt64")

    @property
    def game_executable(self) -> Path:
        """``cs2.exe`` on Windows, the ``cs2.sh`` run script on Linux (get-counter-strike-executable-path.ts)."""
        if self.is_windows:
            return self._cs2() / "game" / "bin" / "win64" / "cs2.exe"
        return self._cs2() / "game" / "cs2.sh"

    @property
    def steam_inf_path(self) -> Path:
        return self.csgo_dir / "steam.inf"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "recording.lock"

    @property
    def install_lock_path(self) -> Path | None:
        """This machine's lock for the CS2 install itself (see :func:`install_lock_path`); ``None`` without CS2."""
        return install_lock_path(self.cs2_dir) if self.cs2_dir is not None else None

    @property
    def cfg_dir(self) -> Path:
        """Folder CS2 stores its user config in while recording (``USRLOCALCSGO``), so the user's own
        video settings and key binds are left alone."""
        return self.data_dir / "cfg"


# --- VDF ----------------------------------------------------------------------------------------------

_VDF_TOKEN = re.compile(r'"((?:\\.|[^"\\])*)"|([{}])')


def parse_vdf(text: str) -> dict[str, Any]:
    """Minimal parser for Valve's text KeyValues (enough for ``libraryfolders.vdf``). Never raises."""
    tokens: list[tuple[str, bool]] = []
    for match in _VDF_TOKEN.finditer(re.sub(r"(?m)^\s*//.*$", "", text)):
        if match.group(2):
            tokens.append((match.group(2), False))
        else:
            tokens.append((match.group(1).replace("\\\\", "\\").replace('\\"', '"'), True))

    pos = 0

    def parse_object() -> dict[str, Any]:
        nonlocal pos
        out: dict[str, Any] = {}
        while pos < len(tokens):
            value, is_string = tokens[pos]
            pos += 1
            if not is_string:
                if value == "}":
                    return out
                continue  # stray "{"
            if pos >= len(tokens):
                break
            nxt, nxt_is_string = tokens[pos]
            pos += 1
            if nxt_is_string:
                out[value] = nxt
            elif nxt == "{":
                out[value] = parse_object()
            else:  # "}" right after a key: malformed, stop this object
                return out
        return out

    try:
        return parse_object()
    except Exception:  # noqa: BLE001 - never raise during detection
        return {}


def steam_library_dirs(steam_dir: Path) -> list[Path]:
    """Every Steam library folder, the one(s) owning CS2 first. Never raises."""
    owners: list[Path] = []
    others: list[Path] = []
    try:
        text = (steam_dir / "steamapps" / "libraryfolders.vdf").read_text(encoding="utf-8", errors="replace")
        data = parse_vdf(text)
        folders = next((v for k, v in data.items() if k.lower() == "libraryfolders"), {})
        if isinstance(folders, dict):
            for entry in folders.values():
                if isinstance(entry, str):  # old format: "1"  "D:\\SteamLibrary"
                    others.append(Path(entry))
                elif isinstance(entry, dict) and isinstance(entry.get("path"), str):
                    apps = entry.get("apps")
                    has_cs2 = isinstance(apps, dict) and CS2_APP_ID in apps
                    (owners if has_cs2 else others).append(Path(entry["path"]))
    except Exception:  # noqa: BLE001
        pass
    out: list[Path] = []
    for path in [*owners, *others, steam_dir]:
        if path not in out:
            out.append(path)
    return out


# --- detection ----------------------------------------------------------------------------------------


def _registry_value(root_name: str, key: str, value: str) -> str | None:
    try:
        import winreg  # noqa: PLC0415 - Windows only

        with winreg.OpenKey(getattr(winreg, root_name), key) as handle:
            data, _ = winreg.QueryValueEx(handle, value)
        return data if isinstance(data, str) and data.strip() else None
    except Exception:  # noqa: BLE001 - not Windows, key missing, access denied...
        return None


def detect_steam_dir(platform: str = sys.platform, home: Path | None = None) -> Path | None:
    try:
        if platform == "win32":
            for root, key, value in (
                ("HKEY_CURRENT_USER", r"Software\Valve\Steam", "SteamPath"),
                ("HKEY_LOCAL_MACHINE", r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
                ("HKEY_LOCAL_MACHINE", r"SOFTWARE\Valve\Steam", "InstallPath"),
            ):
                found = _registry_value(root, key, value)
                if found and Path(found).is_dir():
                    return Path(found)  # Path() normalizes the registry's "c:/program files (x86)/steam"
            return None
        home = home or Path.home()
        candidates = [
            home / ".steam" / "steam",
            home / "snap" / "steam" / "common" / ".local" / "share" / "Steam",
            home / ".local" / "share" / "Steam",
            home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam",  # flatpak
        ]
        for candidate in candidates:
            if (candidate / "steamapps").is_dir():
                return candidate.resolve()
    except Exception:  # noqa: BLE001
        pass
    return None


def normalize_cs2_dir(path: Path) -> Path | None:
    """Accept the install root, its ``game`` folder or ``game/csgo`` and return the install root."""
    try:
        path = Path(path).expanduser()
        for root in (path, path.parent, path.parent.parent):
            if (root / "game" / "csgo" / "gameinfo.gi").is_file():
                return root
    except Exception:  # noqa: BLE001
        pass
    return None


def detect_cs2_dir(steam_dir: Path | None) -> Path | None:
    if steam_dir is None:
        return None
    try:
        for library in steam_library_dirs(steam_dir):
            found = normalize_cs2_dir(library / "steamapps" / "common" / CS2_FOLDER_NAME)
            if found is not None:
                return found
    except Exception:  # noqa: BLE001
        pass
    return None


def find_linux_runtime_script(steam_dir: Path | None) -> Path | None:
    """CS2 must be started through the Steam Linux Runtime "sniper" entry point (start-counter-strike.ts)."""
    if steam_dir is None:
        return None
    try:
        relative = Path("steamapps") / "common" / "SteamLinuxRuntime_sniper" / "_v2-entry-point"
        for library in steam_library_dirs(steam_dir):
            if (library / relative).is_file():
                return library / relative
    except Exception:  # noqa: BLE001
        pass
    return None


def _default_data_dir() -> Path:
    try:
        from ...config import get_settings  # noqa: PLC0415 - keep the import (and its mkdir) out of import time

        return Path(get_settings().data_dir)
    except Exception:  # noqa: BLE001 - config unavailable (tests, partial installs)
        return Path(os.environ.get("DEMOREEL_DATA_DIR") or "data")


def install_lock_path(cs2_dir: Path) -> Path:
    """A lock file named after the CS2 folder, in this machine's temp folder.

    ``recording.lock`` lives in the data folder, so two demoreel set-ups with different data folders (the main
    checkout and a worktree, say) would record with the same game at once: each deletes the other's frames and
    both patch ``gameinfo.gi``. Whoever records holds this lock too, for as long as it holds its own."""
    try:
        key = os.path.normcase(str(Path(cs2_dir).resolve()))
    except OSError:
        key = os.path.normcase(os.path.abspath(cs2_dir))
    digest = hashlib.sha1(key.encode("utf-8", "surrogatepass")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"demoreel-cs2-{digest}.lock"


def default_lock_path() -> Path:
    """The recording lock of the default data folder, without any of the detection :func:`load` runs: idle
    workers ask whether the recorder is taken about once a second."""
    return _default_data_dir().expanduser().absolute() / "ingame" / "recording.lock"


def _env_path(env: Mapping[str, str], name: str) -> Path | None:
    raw = (env.get(name) or "").strip().strip('"')
    return Path(raw).expanduser() if raw else None


def load(env: Mapping[str, str] | None = None, data_dir: Path | None = None,
         platform: str = sys.platform) -> IngameSettings:
    """Build the settings from the environment + auto-detection. Never raises."""
    environ: Mapping[str, str] = os.environ if env is None else env
    try:
        root = Path(data_dir) if data_dir is not None else _default_data_dir()
        root = root.expanduser().absolute()
    except Exception:  # noqa: BLE001
        root = Path("data").absolute()

    steam_exe = _env_path(environ, ENV_STEAM_EXE)
    steam_dir: Path | None = None
    try:
        if steam_exe is not None and steam_exe.parent.is_dir():
            steam_dir = steam_exe.parent
    except Exception:  # noqa: BLE001
        steam_dir = None
    if steam_dir is None:
        steam_dir = detect_steam_dir(platform)
    if steam_exe is None and steam_dir is not None:
        steam_exe = steam_dir / ("steam.exe" if platform == "win32" else "steam.sh")

    cs2_dir: Path | None = None
    cs2_env = _env_path(environ, ENV_CS2_DIR)
    if cs2_env is not None:
        cs2_dir = normalize_cs2_dir(cs2_env)
    if cs2_dir is None:
        cs2_dir = detect_cs2_dir(steam_dir)

    timeout_s: float | None = None
    try:
        raw_timeout = (environ.get(ENV_TIMEOUT_S) or "").strip()
        if raw_timeout and float(raw_timeout) > 0:
            timeout_s = float(raw_timeout)
    except ValueError:
        timeout_s = None

    runtime_script = _env_path(environ, ENV_RUNTIME_SCRIPT)
    if runtime_script is None and platform.startswith("linux"):
        runtime_script = find_linux_runtime_script(steam_dir)

    hlae_exe = _env_path(environ, ENV_HLAE_EXE)
    if hlae_exe is None:
        usable = [h for h in hlae_installs(root / "cache" / "hlae") if h.usable]
        hlae_exe = usable[0].exe if usable else None

    return IngameSettings(
        backend=(environ.get(ENV_BACKEND) or "auto").strip().lower() or "auto",
        hlae_exe=hlae_exe,
        hlae_encoder=(environ.get(ENV_HLAE_ENCODER) or "x264").strip().lower() or "x264",
        cs2_dir=cs2_dir,
        steam_dir=steam_dir,
        steam_exe=steam_exe,
        plugin_dir=_env_path(environ, ENV_PLUGIN_DIR) or (root / "cache" / "csdm-plugin"),
        data_dir=root / "ingame",
        plugin_version=(environ.get(ENV_PLUGIN_VERSION) or "").strip() or None,
        timeout_s=timeout_s,
        runtime_script=runtime_script,
        platform=platform,
    )
