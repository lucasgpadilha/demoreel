"""The CS Demo Manager CS2 plugin: locating the user-provided binary, installing it for one game session and
patching / restoring ``gameinfo.gi``.

CS2 has no plugin system. CSDM's library is named like the game's own server library (``server.dll`` /
``libserver.so``) and placed in ``game/csgo/csdm/bin/``; adding ``Game csgo/csdm`` ahead of ``Game csgo`` in the
search paths of ``game/csgo/gameinfo.gi`` makes the game load it instead (it forwards everything to the real
library). It only loads when the game is started with ``-insecure``.

Ported from CS Demo Manager (MIT, https://github.com/akiver/cs-demo-manager):
``src/node/counter-strike/launcher/cs2-server-plugin.ts`` and ``cs2-server-plugin/AGENTS.md``.

demoreel never ships or downloads the binary: the user copies it from CSDM's ``static/cs2/`` folder into
:attr:`IngameSettings.plugin_dir`. What this module adds on top of the reference is safety around
``gameinfo.gi``: the original is saved next to it as ``gameinfo.gi.demoreel-backup`` before patching, always
restored when the session ends, and recovered at the next start if the process died in between. The plugin's
settings file (``game/csgo/cfg/csdm_settings.json``) gets the same treatment, because a recording changes it.

Which binary for which CS2 build (``src/common/types/cs2-plugin-version.ts``): ``server.dll`` / ``libserver.so``
is the *latest* build; ``server_<N>.dll`` / ``libserver_<N>.so`` are frozen builds that work with CS2 up to patch
``N`` (``PatchVersion`` of ``game/csgo/steam.inf`` without the dots: 1.41.6.8 -> 14168). CSDM lets the user pick
one in its settings (default: latest). demoreel picks automatically: the frozen build with the smallest
``N >= installed patch`` if the user provided one, otherwise the latest; ``DEMOREEL_INGAME_PLUGIN_VERSION``
overrides.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from ..base import RenderError
from .settings import IngameSettings

CSDM_REPO_URL = "https://github.com/akiver/cs-demo-manager"
CSDM_STATIC_DIR = "static/cs2"
LATEST = "latest"

# Frozen plugin builds known when this module was written (cs2-plugin-version.ts). Only used to word the
# "which file do I need" instruction; any server_<N>.dll the user provides is taken into account.
KNOWN_FROZEN_VERSIONS: tuple[int, ...] = (14030, 14088, 14094, 14103, 14112, 14152, 14168, 14196)

# cs2-server-plugin.ts installCs2ServerPlugin():
#   if (!content.includes('Game\tcsgo/csdm')) {
#     content.replace('Game\tcsgo', 'Game\tcsgo/csdm\n\t\t\tGame\tcsgo')   // first occurrence only
SEARCH_PATH_MARKER = "Game\tcsgo/csdm"
SEARCH_PATH_ANCHOR = "Game\tcsgo"
SEARCH_PATH_REPLACEMENT = "Game\tcsgo/csdm\n\t\t\tGame\tcsgo"
BACKUP_SUFFIX = ".demoreel-backup"
_TMP_SUFFIX = ".demoreel-tmp"

# Linux overlay layer (cs2-server-plugin.ts). The layer also fixes the red/blue channel order of the frames
# written by `startmovie` on Linux.
VULKAN_LAYER_NAME = "VK_LAYER_csdm_overlay"
VULKAN_LAYER_LIBRARY_NAME = "libcsdm_vulkan_layer.so"
VULKAN_LAYER_MANIFEST_NAME = "csdm_overlay.json"
VULKAN_LAYER_ENABLE_ENV = "ENABLE_CSDM_OVERLAY"
VULKAN_LAYER_DISABLE_ENV = "DISABLE_CSDM_OVERLAY"

PLUGIN_LOG_NAMES = ("csdm.log", "csdm-vulkan.log")


# --- which binary -----------------------------------------------------------------------------------------


def installed_binary_name(platform: str) -> str:
    return "server.dll" if platform == "win32" else "libserver.so"


def bundled_binary_name(version: str | None, platform: str) -> str:
    """Port of ``getBundledBinaryName``: the file name inside CSDM's ``static/cs2/``."""
    if version and version != LATEST:
        return f"server_{version}.dll" if platform == "win32" else f"libserver_{version}.so"
    return installed_binary_name(platform)


def bundled_vulkan_layer_name(version: str | None) -> str:
    """Port of ``getBundledVulkanLayerName``."""
    if version and version != LATEST:
        return f"libcsdm_vulkan_layer_{version}.so"
    return VULKAN_LAYER_LIBRARY_NAME


_PATCH_LINE = re.compile(r"(?mi)^\s*PatchVersion\s*=\s*([0-9.]+)\s*$")


def parse_patch_version(steam_inf_text: str) -> int | None:
    """``PatchVersion=1.41.9.7`` -> ``14197`` (the numbering used by CSDM's frozen plugin builds)."""
    match = _PATCH_LINE.search(steam_inf_text)
    if not match:
        return None
    digits = match.group(1).replace(".", "")
    return int(digits) if digits.isdigit() else None


def patch_key(number: int) -> tuple[int, ...]:
    """Order of two patch numbers in CSDM's numbering. The digits are only joined (1.41.9.7 -> 14197), so as
    plain numbers 1.41.10.2 (141102) would come after 1.42.0.1 (14201). The major version is one digit and the
    minor two in every CS2 build so far: compared on those first, then on the rest."""
    text = str(int(number))
    if len(text) < 4:
        return (int(text),)
    return int(text[0]), int(text[1:3]), int(text[3:])


def read_cs2_patch_version(settings: IngameSettings) -> int | None:
    try:
        return parse_patch_version(settings.steam_inf_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 - informational
        return None


def read_cs2_patch_text(settings: IngameSettings) -> str | None:
    """The installed CS2 patch as ``steam.inf`` writes it (``1.42.0.1``), for messages."""
    try:
        match = _PATCH_LINE.search(settings.steam_inf_path.read_text(encoding="utf-8", errors="replace"))
        return match.group(1) if match else None
    except Exception:  # noqa: BLE001 - informational
        return None


@dataclass(frozen=True)
class PluginBinary:
    path: Path
    version: str  # "latest" or the frozen build number
    vulkan_layer: Path | None = None  # Linux only, the layer built with this binary


# A library starts with these bytes (PE on Windows, ELF on Linux); the real plugin is far larger than this.
LIBRARY_MAGIC = {"win32": b"MZ", "linux": b"\x7fELF"}
MIN_LIBRARY_BYTES = 16 * 1024


def library_problem(path: Path, platform: str) -> str | None:
    """Why ``path`` cannot be the plugin library, or ``None`` when it looks like one. Catches the usual
    mistake: the GitHub *page* of the file saved under the file's name (an HTML document, not a library)."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(64)
        size = path.stat().st_size
    except OSError as exc:
        return f"cannot be read ({exc})"
    magic = LIBRARY_MAGIC["win32" if platform == "win32" else "linux"]
    if not head.startswith(magic):
        kind = "Windows DLL" if platform == "win32" else "Linux library"
        start = head[:15].decode("ascii", "replace").strip()
        page = (" (a web page: it was saved from the GitHub page of the file instead of downloaded with its "
                "'Download raw file' button)") if b"<" in head[:16] else ""
        return f"is not a {kind}: it starts with {start!r}{page}"
    if size < MIN_LIBRARY_BYTES:
        return f"is only {size} bytes long: the download did not finish"
    return None


def _plugin_files(plugin_dir: Path, platform: str) -> dict[str, Path]:
    """Every file named like a plugin build, valid or not."""
    found: dict[str, Path] = {}
    try:
        latest = plugin_dir / installed_binary_name(platform)
        if latest.is_file():
            found[LATEST] = latest
        pattern = re.compile(r"^server_(\d+)\.dll$" if platform == "win32" else r"^libserver_(\d+)\.so$",
                             re.IGNORECASE)
        for entry in plugin_dir.iterdir():
            match = pattern.match(entry.name)
            if match and entry.is_file():
                found[match.group(1)] = entry
    except OSError:
        pass
    return found


def list_plugin_binaries(plugin_dir: Path, platform: str) -> dict[str, Path]:
    """``{"latest": .../server.dll, "14168": .../server_14168.dll, ...}`` for the files that are libraries."""
    return {version: path for version, path in _plugin_files(plugin_dir, platform).items()
            if library_problem(path, platform) is None}


def invalid_plugin_files(plugin_dir: Path, platform: str) -> list[str]:
    """``"<path> is not a Windows DLL: ..."`` for each file named like a plugin build that is not one."""
    out = []
    for path in _plugin_files(plugin_dir, platform).values():
        try:
            empty = path.stat().st_size == 0  # an empty file is simply ignored
        except OSError:
            empty = False
        problem = library_problem(path, platform)
        if problem is not None and not empty:
            out.append(f"`{path}` {problem}")
    return out


def recommended_version(cs2_patch: int | None, frozen: tuple[int, ...] | list[int] = KNOWN_FROZEN_VERSIONS) -> str:
    """Frozen build with the smallest number >= the installed patch, else ``"latest"``."""
    if cs2_patch is not None:
        candidates = sorted((v for v in frozen if patch_key(v) >= patch_key(cs2_patch)), key=patch_key)
        if candidates:
            return str(candidates[0])
    return LATEST


def select_plugin_binary(plugin_dir: Path, platform: str, cs2_patch: int | None = None,
                         explicit: str | None = None) -> PluginBinary | None:
    available = list_plugin_binaries(plugin_dir, platform)
    if explicit:
        version = explicit.strip().lower()
    else:
        version = recommended_version(cs2_patch, [int(v) for v in available if v != LATEST])
    path = available.get(version)
    if path is None:
        return None
    layer: Path | None = None
    if platform != "win32":
        candidate = plugin_dir / bundled_vulkan_layer_name(version)
        layer = candidate if candidate.is_file() else None
    return PluginBinary(path=path, version=version, vulkan_layer=layer)


def missing_plugin_instruction(settings: IngameSettings, cs2_patch: int | None) -> str:
    """Precise "put this file there" message shown by ``availability()``."""
    platform = settings.platform
    version = (settings.plugin_version or "").strip().lower() or recommended_version(cs2_patch)
    source = f"{CSDM_STATIC_DIR}/{bundled_binary_name(version, platform)}"
    destination = settings.plugin_dir / bundled_binary_name(version, platform)
    build = f"CS2 patch {cs2_patch}" if cs2_patch is not None else "your CS2 build (patch unknown)"
    text = (
        f"CS Demo Manager's CS2 plugin is missing. Copy `{source}` from the cs-demo-manager repository "
        f"({CSDM_REPO_URL}, MIT) to `{destination}` (keep the file name). That is the build matching {build}; "
        f"`{bundled_binary_name(None, platform)}` is the latest build, `{bundled_binary_name('<N>', platform)}` "
        f"are frozen builds for CS2 patches up to N."
    )
    invalid = invalid_plugin_files(settings.plugin_dir, platform)
    if invalid:
        text += " Not usable: " + "; ".join(invalid) + "."
    if platform != "win32":
        text += (f" On Linux also copy `{CSDM_STATIC_DIR}/{bundled_vulkan_layer_name(version)}` next to it when "
                 f"it exists (correct colors in the recorded frames).")
    return text


# --- gameinfo.gi --------------------------------------------------------------------------------------------


def backup_path(gameinfo: Path) -> Path:
    return gameinfo.with_name(gameinfo.name + BACKUP_SUFFIX)


def patch_gameinfo_text(content: str) -> tuple[str, bool]:
    """Add the plugin search path ahead of ``Game csgo``. Returns ``(content, changed)``; idempotent."""
    if SEARCH_PATH_MARKER in content:
        return content, False
    if SEARCH_PATH_ANCHOR not in content:
        raise RenderError("gameinfo.gi has an unexpected format (no `Game<TAB>csgo` search path); "
                          "it was left untouched. Has a CS2 update changed the file?")
    return content.replace(SEARCH_PATH_ANCHOR, SEARCH_PATH_REPLACEMENT, 1), True


def unpatch_gameinfo_text(content: str) -> str:
    """Inverse of :func:`patch_gameinfo_text` (used when no usable backup exists)."""
    return content.replace(SEARCH_PATH_REPLACEMENT, SEARCH_PATH_ANCHOR)


def _decode(data: bytes) -> str:
    # gameinfo.gi is ASCII with CRLF line ends; surrogateescape keeps any other byte intact on the way back.
    return data.decode("utf-8", errors="surrogateescape")


def _encode(text: str) -> bytes:
    return text.encode("utf-8", errors="surrogateescape")


def _write_bytes(path: Path, data: bytes, attempts: int = 5) -> None:
    """Replace ``path`` atomically; falls back to an in-place write when the rename is refused (the file can
    stay locked for a moment after the game exits)."""
    tmp = path.with_name(path.name + _TMP_SUFFIX)
    last_error: OSError | None = None
    for attempt in range(attempts):
        try:
            with open(tmp, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            return
        except OSError as exc:
            last_error = exc
            try:
                with open(path, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                tmp.unlink(missing_ok=True)
                return
            except OSError as exc2:
                last_error = exc2
        if attempt < attempts - 1:
            time.sleep(0.5)
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass
    raise last_error if last_error is not None else OSError(f"cannot write {path}")


def _whole(data: bytes | None) -> bool:
    """Does this look like a complete ``gameinfo.gi``? An interrupted write leaves an empty or cut-short file,
    which must never be taken for a clean one (CS2 then only starts after Steam verified the game files)."""
    return bool(data and data.strip()) and SEARCH_PATH_ANCHOR.encode("ascii") in (data or b"")


def recover_gameinfo(gameinfo: Path) -> str:
    """Undo a patch left behind by a crashed run. Returns ``"none"``, ``"restored"``, ``"discarded"`` or
    ``"damaged"``.

    * backup present and ``gameinfo.gi`` still patched -> the backup is put back when it is the same file minus
      our line; when it is not, the file was rewritten after the backup was made (a CS2 update, then CS Demo
      Manager adding its line again) and only the line is taken out, so the update is kept;
    * backup present and ``gameinfo.gi`` missing, empty or cut short -> the backup is put back;
    * backup present but ``gameinfo.gi`` is clean (the plugin restored it, or Steam rewrote it during an update)
      -> the current file is kept, since it may be newer than the backup, and the stale backup is deleted;
    * neither is usable -> ``"damaged"``: both are left as they are (verify the game files in Steam).
    """
    backup = backup_path(gameinfo)
    if not backup.is_file():
        return "none"
    current = gameinfo.read_bytes() if gameinfo.is_file() else None
    marker = SEARCH_PATH_MARKER.encode("ascii")
    if _whole(current) and marker not in current:  # type: ignore[operator]
        backup.unlink(missing_ok=True)
        return "discarded"
    saved = backup.read_bytes()
    saved_ok = _whole(saved) and marker not in saved
    if current is not None and marker in current:
        # The file minus our line is byte for byte the backup when nothing else touched it, and the newer file
        # when something did: writing the backup over it would undo a CS2 update.
        _write_bytes(gameinfo, _encode(unpatch_gameinfo_text(_decode(current))))
    elif saved_ok:
        _write_bytes(gameinfo, saved)
    else:
        return "damaged"
    backup.unlink(missing_ok=True)
    return "restored"


def _restore_gameinfo(gameinfo: Path, original: bytes) -> None:
    backup = backup_path(gameinfo)
    try:
        current = gameinfo.read_bytes() if gameinfo.is_file() else None
        if not _whole(current) or SEARCH_PATH_MARKER.encode("ascii") in current:  # type: ignore[operator]
            _write_bytes(gameinfo, original)
        # else: already clean (restored by the plugin or replaced by Steam) - keep that version.
        backup.unlink(missing_ok=True)
    except OSError as exc:
        raise RenderError(
            f"Could not restore {gameinfo}: {exc}. The original is saved as {backup} and will be put back "
            f"automatically the next time the in-game renderer starts (or copy it over gameinfo.gi yourself)."
        ) from exc


@contextmanager
def patched_gameinfo(gameinfo: Path) -> Iterator[bool]:
    """Patch ``gameinfo.gi`` for the duration of the block and ALWAYS put the original back.

    Yields ``True`` when this call patched the file, ``False`` when the search path was already there (CSDM is
    installed/in use: the file is then left exactly as found).
    """
    recover_gameinfo(gameinfo)
    try:
        original = gameinfo.read_bytes()
    except OSError as exc:
        raise RenderError(f"Cannot read {gameinfo}: {exc}") from exc
    patched, changed = patch_gameinfo_text(_decode(original))
    if not changed:
        yield False
        return
    backup = backup_path(gameinfo)
    try:
        _write_bytes(backup, original)  # the backup exists before the first byte of gameinfo.gi changes
    except OSError as exc:
        raise RenderError(f"Cannot write {backup}: {exc}. Is the CS2 folder writable?") from exc
    try:
        try:
            _write_bytes(gameinfo, _encode(patched))
        except OSError as exc:
            raise RenderError(f"Cannot write {gameinfo}: {exc}. Is the CS2 folder writable?") from exc
        yield True
    finally:
        _restore_gameinfo(gameinfo, original)


# --- the plugin's own settings file -------------------------------------------------------------------------

# cs2-server-plugin/settings.cpp: the plugin keeps the state of its panels in `<game>/csgo/cfg/csdm_settings.json`,
# reads it when it loads and writes it back when a setting changes (checked every 128 frames, and on shutdown).
# The file belongs to whoever uses CS Demo Manager on this machine. Every recording changes settings (see
# ``actions.PLUGIN_PANEL_COMMANDS``), so without care it would switch their voice indicator off for good, or
# leave a file behind in an install that never had one.
PLUGIN_SETTINGS_NAME = "csdm_settings.json"


def plugin_settings_path(settings: IngameSettings) -> Path:
    return settings.csgo_dir / "cfg" / PLUGIN_SETTINGS_NAME


def recover_plugin_settings(path: Path) -> str:
    """Put back the plugin settings saved by a run that died before restoring them. Returns ``"none"`` or
    ``"restored"``.

    The backup holds the file as it was before the recording; an empty backup means there was no file, and what
    the plugin wrote is deleted. Unlike ``gameinfo.gi`` nothing in the file tells who wrote it last, so settings
    changed in CS Demo Manager between that crash and this call are lost to the older copy.
    """
    backup = backup_path(path)
    if not backup.is_file():
        return "none"
    saved = backup.read_bytes()
    if saved:
        _write_bytes(path, saved)
    else:
        path.unlink(missing_ok=True)
    backup.unlink(missing_ok=True)
    return "restored"


def _restore_plugin_settings(path: Path, original: bytes | None) -> None:
    backup = backup_path(path)
    try:
        if original is None:
            path.unlink(missing_ok=True)
        elif not path.is_file() or path.read_bytes() != original:
            _write_bytes(path, original)
        backup.unlink(missing_ok=True)
    except OSError as exc:
        raise RenderError(
            f"Could not restore {path} (the panel settings of CS Demo Manager's plugin): {exc}. The original is "
            f"saved as {backup} and will be put back automatically the next time the in-game renderer starts."
        ) from exc


@contextmanager
def preserved_plugin_settings(path: Path) -> Iterator[None]:
    """Leave the plugin's settings file exactly as found - same bytes, or still absent - once the block ends,
    whatever the plugin wrote to it meanwhile. The game must be gone by then: the plugin saves on shutdown."""
    original: bytes | None = None
    try:
        recover_plugin_settings(path)
        if path.is_file():
            original = path.read_bytes()
        if path.parent.is_dir():  # no cfg folder: nothing to save, and nowhere for the plugin to write
            _write_bytes(backup_path(path), original or b"")
    except OSError as exc:
        raise RenderError(f"Cannot save a copy of {path} before recording: {exc}. "
                          f"Is the CS2 folder writable?") from exc
    try:
        yield
    finally:
        _restore_plugin_settings(path, original)


# --- installation -------------------------------------------------------------------------------------------


@dataclass
class InstalledPlugin:
    binary: PluginBinary
    vulkan_layer_installed: bool = False
    gameinfo_patched: bool = False
    env: dict[str, str] = field(default_factory=dict)  # extra environment for the game process


def plugin_bin_dir(settings: IngameSettings) -> Path:
    root = settings.plugin_install_dir
    return root / "bin" if settings.is_windows else root / "bin" / "linuxsteamrt64"


def vulkan_layer_dir(settings: IngameSettings) -> Path:
    """Must match ``VULKAN_LAYER_LIB_PATH`` of the plugin (``/csgo/csdm/vulkan/libcsdm_vulkan_layer.so``)."""
    return settings.plugin_install_dir / "vulkan"


def delete_plugin_logs(settings: IngameSettings) -> None:
    for name in PLUGIN_LOG_NAMES:
        try:
            (settings.bin_dir / name).unlink(missing_ok=True)
        except OSError:
            pass


def read_plugin_log(settings: IngameSettings, max_chars: int = 1500) -> str:
    """Tail of ``csdm.log`` (written next to the game executable), for error messages."""
    try:
        text = (settings.bin_dir / "csdm.log").read_text(encoding="utf-8", errors="replace")
        return text[-max_chars:].strip()
    except OSError:
        return ""


def _install_vulkan_layer(settings: IngameSettings, binary: PluginBinary) -> bool:
    """Port of ``installCs2VulkanLayer``: library + implicit-layer manifest inside the plugin folder."""
    layer_dir = vulkan_layer_dir(settings)
    if binary.vulkan_layer is None:
        shutil.rmtree(layer_dir, ignore_errors=True)
        return False
    manifest_dir = layer_dir / "implicit_layer.d"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    library_path = layer_dir / VULKAN_LAYER_LIBRARY_NAME
    shutil.copyfile(binary.vulkan_layer, library_path)
    manifest = {
        "file_format_version": "1.2.0",
        "layer": {
            "name": VULKAN_LAYER_NAME,
            "type": "GLOBAL",
            "library_path": str(library_path),
            "api_version": "1.3.0",
            "implementation_version": "1",
            "description": "CS Demo Manager in-game overlay",
            "enable_environment": {VULKAN_LAYER_ENABLE_ENV: "1"},
            "disable_environment": {VULKAN_LAYER_DISABLE_ENV: "1"},
        },
    }
    (manifest_dir / VULKAN_LAYER_MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return True


def vulkan_layer_env(settings: IngameSettings, base_env: Mapping[str, str]) -> dict[str, str]:
    """Port of ``getCs2VulkanLayerEnv``: make the Vulkan loader find and enable the implicit layer."""
    manifest_dir = vulkan_layer_dir(settings) / "implicit_layer.d"

    def prepend(value: str, current: str | None) -> str:
        return f"{value}:{current}" if current else value

    return {
        "VK_ADD_IMPLICIT_LAYER_PATH": prepend(str(manifest_dir), base_env.get("VK_ADD_IMPLICIT_LAYER_PATH")),
        "XDG_DATA_DIRS": prepend(str(settings.plugin_install_dir),
                                 base_env.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"),
        VULKAN_LAYER_ENABLE_ENV: "1",
        "VK_LOADER_DEBUG": base_env.get("VK_LOADER_DEBUG") or "layer,error",
    }


def install_plugin(settings: IngameSettings, binary: PluginBinary,
                   base_env: Mapping[str, str] | None = None) -> InstalledPlugin:
    """Copy the user's plugin binary into ``game/csgo/csdm/bin`` (port of ``installCs2ServerPlugin``, minus
    the ``gameinfo.gi`` edit which :func:`patched_gameinfo` owns)."""
    installed = InstalledPlugin(binary=binary)
    try:
        delete_plugin_logs(settings)
        bin_dir = plugin_bin_dir(settings)
        bin_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(binary.path, bin_dir / installed_binary_name(settings.platform))
        if settings.is_linux:
            installed.vulkan_layer_installed = _install_vulkan_layer(settings, binary)
            if installed.vulkan_layer_installed:
                installed.env.update(vulkan_layer_env(settings, base_env if base_env is not None else os.environ))
    except OSError as exc:
        uninstall_plugin(settings)
        raise RenderError(f"Cannot install the plugin into {settings.plugin_install_dir}: {exc}") from exc
    return installed


# While the plugin folder is the first `Game` search path, CS2 itself drops these into it. Seen on the first
# real recording (CS2 1.41.9.7): they are regenerated on demand and must not outlive the session.
GAME_GENERATED_NAMES = ("ServerConfig.vdf", "_iconcache")


def remove_game_generated(settings: IngameSettings) -> None:
    """Delete what the game wrote into the plugin folder, then the folder itself once it is empty, so the CS2
    install ends up exactly as it was found. Safe to call repeatedly."""
    root = settings.plugin_install_dir
    for name in GAME_GENERATED_NAMES:
        path = root / name
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        root.rmdir()
    except OSError:
        pass  # not empty (raw frames still there) or already gone


def uninstall_plugin(settings: IngameSettings) -> None:
    """Remove the binaries copied by :func:`install_plugin` and the files the game generated next to them. The
    ``movie`` folder (raw frames) is left to the encoder's cleanup; the plugin folder is removed once empty."""
    root = settings.plugin_install_dir
    shutil.rmtree(root / "bin", ignore_errors=True)
    shutil.rmtree(root / "vulkan", ignore_errors=True)
    remove_game_generated(settings)


@contextmanager
def plugin_session(settings: IngameSettings, binary: PluginBinary,
                   base_env: Mapping[str, str] | None = None) -> Iterator[InstalledPlugin]:
    """Plugin installed + ``gameinfo.gi`` patched inside the block, both undone afterwards no matter what, and
    the plugin's settings file put back as it was."""
    installed = install_plugin(settings, binary, base_env)
    try:
        with (preserved_plugin_settings(plugin_settings_path(settings)),
              patched_gameinfo(settings.gameinfo_path) as patched):
            installed.gameinfo_patched = patched
            yield installed
    finally:
        uninstall_plugin(settings)
