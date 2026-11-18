"""Plugin selection and gameinfo.gi patch / restore / crash recovery, always on a temp copy (never the game)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from demoreel.render.base import RenderError
from demoreel.render.ingame import plugin
from demoreel.render.ingame.settings import IngameSettings

# Same structure as the real file (tabs, CRLF line ends, a Game_LowViolence line before the Game lines).
GAMEINFO = (
    '"GameInfo"\r\n{\r\n\tgame \t\t"Counter-Strike 2"\r\n\tFileSystem\r\n\t{\r\n\t\tSearchPaths\r\n\t\t{\r\n'
    "\t\t\tGame_LowViolence\tcsgo_lv // Perfect World content override\r\n\r\n"
    "\t\t\tGame\tcsgo\r\n\t\t\tGame\tcsgo_imported\r\n\t\t\tGame\tcsgo_core\r\n\t\t\tGame\tcore\r\n\r\n"
    "\t\t\tMod\t\tcsgo\r\n\t\t}\r\n\t}\r\n}\r\n"
)
MARKER = b"Game\tcsgo/csdm"


def make_settings(tmp_path: Path, platform: str = "win32", **kwargs) -> IngameSettings:
    cs2 = tmp_path / "cs2"
    (cs2 / "game" / "csgo").mkdir(parents=True, exist_ok=True)
    (cs2 / "game" / "bin" / ("win64" if platform == "win32" else "linuxsteamrt64")).mkdir(parents=True,
                                                                                           exist_ok=True)
    (cs2 / "game" / "csgo" / "gameinfo.gi").write_bytes(GAMEINFO.encode())
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir(exist_ok=True)
    return IngameSettings(cs2_dir=cs2, steam_dir=None, steam_exe=None, plugin_dir=plugin_dir,
                          data_dir=tmp_path / "data" / "ingame", platform=platform, **kwargs)


# --- gameinfo.gi text -----------------------------------------------------------------------------------


def test_patch_adds_the_search_path_ahead_of_csgo_exactly_like_csdm():
    patched, changed = plugin.patch_gameinfo_text(GAMEINFO)
    assert changed
    assert patched == GAMEINFO.replace("\t\t\tGame\tcsgo\r\n", "\t\t\tGame\tcsgo/csdm\n\t\t\tGame\tcsgo\r\n", 1)
    assert patched.count("Game\tcsgo/csdm") == 1
    assert patched.index("Game\tcsgo/csdm") < patched.index("Game\tcsgo\r\n")
    assert "Game_LowViolence\tcsgo_lv" in patched and "Game\tcsgo_imported" in patched  # untouched


def test_patch_is_idempotent_and_reversible():
    patched, _ = plugin.patch_gameinfo_text(GAMEINFO)
    again, changed = plugin.patch_gameinfo_text(patched)
    assert (again, changed) == (patched, False)
    assert plugin.unpatch_gameinfo_text(patched) == GAMEINFO


def test_patch_refuses_an_unknown_format():
    with pytest.raises(RenderError, match="unexpected format"):
        plugin.patch_gameinfo_text('"GameInfo"\n{\n\tSearchPaths { Game  csgo }\n}\n')


# --- gameinfo.gi file: patch, restore, recovery ----------------------------------------------------------


def test_context_manager_patches_then_restores_byte_for_byte(tmp_path):
    gameinfo = make_settings(tmp_path).gameinfo_path
    original = gameinfo.read_bytes()
    with plugin.patched_gameinfo(gameinfo) as changed:
        assert changed is True
        assert MARKER in gameinfo.read_bytes()
        assert plugin.backup_path(gameinfo).read_bytes() == original
    assert gameinfo.read_bytes() == original
    assert not plugin.backup_path(gameinfo).exists()
    assert sorted(p.name for p in gameinfo.parent.iterdir()) == ["gameinfo.gi"]  # no temp file left


def test_original_is_restored_when_the_body_raises(tmp_path):
    gameinfo = make_settings(tmp_path).gameinfo_path
    original = gameinfo.read_bytes()
    with pytest.raises(KeyboardInterrupt):
        with plugin.patched_gameinfo(gameinfo):
            raise KeyboardInterrupt
    assert gameinfo.read_bytes() == original
    assert not plugin.backup_path(gameinfo).exists()


def test_file_already_patched_by_csdm_is_left_exactly_as_found(tmp_path):
    gameinfo = make_settings(tmp_path).gameinfo_path
    csdm_patched = plugin.patch_gameinfo_text(GAMEINFO)[0].encode()
    gameinfo.write_bytes(csdm_patched)
    with plugin.patched_gameinfo(gameinfo) as changed:
        assert changed is False
        assert not plugin.backup_path(gameinfo).exists()
    assert gameinfo.read_bytes() == csdm_patched


def test_restore_keeps_a_file_that_was_cleaned_by_someone_else(tmp_path):
    """The plugin (or a Steam update) may rewrite gameinfo.gi while the game runs: do not overwrite that."""
    gameinfo = make_settings(tmp_path).gameinfo_path
    newer = GAMEINFO.replace("Counter-Strike 2", "Counter-Strike 2 (updated)").encode()
    with plugin.patched_gameinfo(gameinfo):
        gameinfo.write_bytes(newer)
    assert gameinfo.read_bytes() == newer
    assert not plugin.backup_path(gameinfo).exists()


def test_crash_recovery_restores_the_backup_on_next_start(tmp_path):
    gameinfo = make_settings(tmp_path).gameinfo_path
    original = gameinfo.read_bytes()
    # Simulate a process killed between patch and restore.
    plugin.backup_path(gameinfo).write_bytes(original)
    gameinfo.write_bytes(plugin.patch_gameinfo_text(GAMEINFO)[0].encode())

    assert plugin.recover_gameinfo(gameinfo) == "restored"
    assert gameinfo.read_bytes() == original
    assert not plugin.backup_path(gameinfo).exists()
    assert plugin.recover_gameinfo(gameinfo) == "none"


def test_crash_recovery_runs_before_a_new_patch(tmp_path):
    gameinfo = make_settings(tmp_path).gameinfo_path
    original = gameinfo.read_bytes()
    plugin.backup_path(gameinfo).write_bytes(original)
    gameinfo.write_bytes(plugin.patch_gameinfo_text(GAMEINFO)[0].encode())
    with plugin.patched_gameinfo(gameinfo) as changed:
        assert changed is True  # recovered first, then patched again from the clean original
        assert gameinfo.read_bytes().count(MARKER) == 1
    assert gameinfo.read_bytes() == original


def test_crash_recovery_discards_a_stale_backup_when_the_file_is_already_clean(tmp_path):
    gameinfo = make_settings(tmp_path).gameinfo_path
    newer = GAMEINFO.replace("Counter-Strike 2", "Counter-Strike 2 (updated by Steam)").encode()
    gameinfo.write_bytes(newer)
    plugin.backup_path(gameinfo).write_bytes(GAMEINFO.encode())
    assert plugin.recover_gameinfo(gameinfo) == "discarded"
    assert gameinfo.read_bytes() == newer
    assert not plugin.backup_path(gameinfo).exists()


def test_crash_recovery_with_a_corrupt_backup_removes_only_our_line(tmp_path):
    gameinfo = make_settings(tmp_path).gameinfo_path
    gameinfo.write_bytes(plugin.patch_gameinfo_text(GAMEINFO)[0].encode())
    plugin.backup_path(gameinfo).write_bytes(b"")
    assert plugin.recover_gameinfo(gameinfo) == "restored"
    assert gameinfo.read_bytes() == GAMEINFO.encode()


# --- the plugin's settings file ---------------------------------------------------------------------------

# What the plugin writes (settings.cpp, `dump(2)`), shortened. The owner of this one likes the voice indicator.
PLUGIN_SETTINGS = b'{\n  "csdm_buttons_overlay": true,\n  "csdm_voice_indicator": true\n}'
RECORDED_SETTINGS = b'{\n  "csdm_buttons_overlay": false,\n  "csdm_voice_indicator": false\n}'


def settings_file(tmp_path: Path) -> Path:
    path = plugin.plugin_settings_path(make_settings(tmp_path))
    path.parent.mkdir()
    return path


def test_plugin_settings_live_in_the_cfg_folder_of_the_game(tmp_path):
    settings = make_settings(tmp_path)
    assert plugin.plugin_settings_path(settings) == settings.cs2_dir / "game" / "csgo" / "cfg" / "csdm_settings.json"


def test_settings_changed_by_the_plugin_while_recording_are_put_back_byte_for_byte(tmp_path):
    path = settings_file(tmp_path)
    path.write_bytes(PLUGIN_SETTINGS)
    with plugin.preserved_plugin_settings(path):
        assert plugin.backup_path(path).read_bytes() == PLUGIN_SETTINGS  # on disk before the game can write
        path.write_bytes(RECORDED_SETTINGS)  # the plugin saving `csdm_voice_indicator 0`
    assert path.read_bytes() == PLUGIN_SETTINGS
    assert sorted(p.name for p in path.parent.iterdir()) == ["csdm_settings.json"]  # no backup, no temp file


def test_a_settings_file_the_plugin_created_while_recording_is_removed(tmp_path):
    path = settings_file(tmp_path)  # CS Demo Manager was never used with this install
    with plugin.preserved_plugin_settings(path):
        path.write_bytes(RECORDED_SETTINGS)
    assert list(path.parent.iterdir()) == []


def test_settings_are_put_back_when_the_body_raises_and_untouched_ones_are_not_rewritten(tmp_path):
    path = settings_file(tmp_path)
    path.write_bytes(PLUGIN_SETTINGS)
    with pytest.raises(KeyboardInterrupt):
        with plugin.preserved_plugin_settings(path):
            path.write_bytes(RECORDED_SETTINGS)
            raise KeyboardInterrupt
    assert path.read_bytes() == PLUGIN_SETTINGS and not plugin.backup_path(path).exists()

    before = path.stat().st_mtime_ns
    with plugin.preserved_plugin_settings(path):
        pass  # the plugin had nothing to save
    assert path.stat().st_mtime_ns == before


def test_an_install_without_a_cfg_folder_is_left_alone(tmp_path):
    path = plugin.plugin_settings_path(make_settings(tmp_path))
    with plugin.preserved_plugin_settings(path):
        assert not path.parent.exists()
    assert not path.parent.exists()


def test_crash_recovery_puts_the_saved_settings_back_or_removes_what_the_plugin_created(tmp_path):
    path = settings_file(tmp_path)
    assert plugin.recover_plugin_settings(path) == "none"
    # killed while recording, the file had been there...
    plugin.backup_path(path).write_bytes(PLUGIN_SETTINGS)
    path.write_bytes(RECORDED_SETTINGS)
    assert plugin.recover_plugin_settings(path) == "restored"
    assert path.read_bytes() == PLUGIN_SETTINGS and not plugin.backup_path(path).exists()
    # ...or it had not (an empty backup says so)
    path.unlink()
    plugin.backup_path(path).write_bytes(b"")
    path.write_bytes(RECORDED_SETTINGS)
    assert plugin.recover_plugin_settings(path) == "restored"
    assert list(path.parent.iterdir()) == []
    # and the next session does it by itself, before saving its own copy
    plugin.backup_path(path).write_bytes(PLUGIN_SETTINGS)
    path.write_bytes(RECORDED_SETTINGS)
    with plugin.preserved_plugin_settings(path):
        assert path.read_bytes() == PLUGIN_SETTINGS
    assert path.read_bytes() == PLUGIN_SETTINGS


# --- which binary -----------------------------------------------------------------------------------------


def test_patch_version_parsing():
    inf = "ClientVersion=2000943\nServerVersion=2000943\nPatchVersion=1.41.9.7\nProductName=cs2\n"
    assert plugin.parse_patch_version(inf) == 14197
    assert plugin.parse_patch_version("PatchVersion=1.40.3.0\r\n") == 14030
    assert plugin.parse_patch_version("nothing here") is None


def test_binary_names_per_platform_and_version():
    assert plugin.bundled_binary_name(None, "win32") == "server.dll"
    assert plugin.bundled_binary_name("latest", "linux") == "libserver.so"
    assert plugin.bundled_binary_name("14168", "win32") == "server_14168.dll"
    assert plugin.bundled_binary_name("14168", "linux") == "libserver_14168.so"
    assert plugin.bundled_vulkan_layer_name("14196") == "libcsdm_vulkan_layer_14196.so"
    assert plugin.bundled_vulkan_layer_name("latest") == "libcsdm_vulkan_layer.so"


def test_recommended_version_is_the_smallest_frozen_build_covering_the_patch():
    assert plugin.recommended_version(14197) == "latest"
    assert plugin.recommended_version(14196) == "14196"
    assert plugin.recommended_version(14170) == "14196"
    assert plugin.recommended_version(14100) == "14103"
    assert plugin.recommended_version(13000) == "14030"
    assert plugin.recommended_version(None) == "latest"


def test_select_plugin_binary(tmp_path):
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    assert plugin.select_plugin_binary(plugin_dir, "win32", 14197) is None
    (plugin_dir / "server.dll").write_bytes(b"placeholder, not a real library")
    (plugin_dir / "server_14168.dll").write_bytes(b"placeholder, not a real library")
    (plugin_dir / "server_14030.dll").write_bytes(b"")  # empty files are ignored
    (plugin_dir / "libserver.so").write_bytes(b"other platform")

    assert plugin.list_plugin_binaries(plugin_dir, "win32").keys() == {"latest", "14168"}
    assert plugin.select_plugin_binary(plugin_dir, "win32", 14197).version == "latest"
    assert plugin.select_plugin_binary(plugin_dir, "win32", None).path.name == "server.dll"
    assert plugin.select_plugin_binary(plugin_dir, "win32", 14160).path.name == "server_14168.dll"
    assert plugin.select_plugin_binary(plugin_dir, "win32", 14197, explicit="14168").version == "14168"
    assert plugin.select_plugin_binary(plugin_dir, "win32", 14197, explicit="14152") is None

    linux = plugin.select_plugin_binary(plugin_dir, "linux", 14197)
    assert linux.path.name == "libserver.so" and linux.vulkan_layer is None
    (plugin_dir / "libcsdm_vulkan_layer.so").write_bytes(b"placeholder")
    assert plugin.select_plugin_binary(plugin_dir, "linux", 14197).vulkan_layer.name == "libcsdm_vulkan_layer.so"


def test_missing_plugin_instruction_names_source_and_destination(tmp_path):
    settings = make_settings(tmp_path)
    text = plugin.missing_plugin_instruction(settings, 14197)
    assert "static/cs2/server.dll" in text
    assert str(settings.plugin_dir / "server.dll") in text
    assert "cs-demo-manager" in text and "14197" in text

    old = plugin.missing_plugin_instruction(settings, 14160)
    assert "static/cs2/server_14168.dll" in old and str(settings.plugin_dir / "server_14168.dll") in old

    linux = plugin.missing_plugin_instruction(make_settings(tmp_path, platform="linux"), 14197)
    assert "static/cs2/libserver.so" in linux and "libcsdm_vulkan_layer.so" in linux


# --- install / uninstall in a fake game tree --------------------------------------------------------------


def test_plugin_session_installs_patches_and_cleans_up_on_windows_layout(tmp_path):
    settings = make_settings(tmp_path)
    original = settings.gameinfo_path.read_bytes()
    (settings.plugin_dir / "server.dll").write_bytes(b"placeholder, not a real library")
    (settings.bin_dir / "csdm.log").write_text("stale log")
    binary = plugin.select_plugin_binary(settings.plugin_dir, "win32", 14197)

    with plugin.plugin_session(settings, binary) as installed:
        target = settings.csgo_dir / "csdm" / "bin" / "server.dll"
        assert target.read_bytes() == b"placeholder, not a real library"
        assert MARKER in settings.gameinfo_path.read_bytes()
        assert installed.gameinfo_patched and not installed.vulkan_layer_installed and installed.env == {}
        assert not (settings.bin_dir / "csdm.log").exists()  # stale plugin log removed like CSDM does
        (settings.plugin_install_dir / "movie").mkdir()  # the game writes frames here
        (settings.plugin_install_dir / "movie" / "1-sequence00000001.tga").write_bytes(b"x")

    assert settings.gameinfo_path.read_bytes() == original
    assert not (settings.plugin_install_dir / "bin").exists()
    assert (settings.plugin_install_dir / "movie" / "1-sequence00000001.tga").exists()  # left for the encoder


def test_plugin_session_cleans_up_when_the_body_raises(tmp_path):
    settings = make_settings(tmp_path)
    original = settings.gameinfo_path.read_bytes()
    (settings.plugin_dir / "server.dll").write_bytes(b"placeholder")
    binary = plugin.select_plugin_binary(settings.plugin_dir, "win32", None)
    with pytest.raises(RuntimeError):
        with plugin.plugin_session(settings, binary):
            raise RuntimeError("game crashed")
    assert settings.gameinfo_path.read_bytes() == original
    assert not settings.plugin_install_dir.exists()
    assert not plugin.backup_path(settings.gameinfo_path).exists()


def test_plugin_session_puts_the_plugin_settings_back_after_gameinfo(tmp_path):
    settings = make_settings(tmp_path)
    (settings.plugin_dir / "server.dll").write_bytes(b"placeholder")
    binary = plugin.select_plugin_binary(settings.plugin_dir, "win32", None)
    path = plugin.plugin_settings_path(settings)
    path.parent.mkdir()
    path.write_bytes(PLUGIN_SETTINGS)
    with pytest.raises(RuntimeError):
        with plugin.plugin_session(settings, binary):
            assert plugin.backup_path(path).read_bytes() == PLUGIN_SETTINGS
            path.write_bytes(RECORDED_SETTINGS)
            raise RuntimeError("game crashed")
    assert path.read_bytes() == PLUGIN_SETTINGS
    assert sorted(p.name for p in path.parent.iterdir()) == ["csdm_settings.json"]
    assert settings.gameinfo_path.read_bytes() == GAMEINFO.encode()


def test_linux_layout_installs_the_vulkan_layer_and_its_environment(tmp_path):
    settings = make_settings(tmp_path, platform="linux")
    (settings.plugin_dir / "libserver.so").write_bytes(b"placeholder")
    (settings.plugin_dir / "libcsdm_vulkan_layer.so").write_bytes(b"layer placeholder")
    binary = plugin.select_plugin_binary(settings.plugin_dir, "linux", 14197)

    installed = plugin.install_plugin(settings, binary, base_env={"XDG_DATA_DIRS": "/usr/share"})
    root = settings.plugin_install_dir
    assert (root / "bin" / "linuxsteamrt64" / "libserver.so").is_file()
    assert (root / "vulkan" / "libcsdm_vulkan_layer.so").read_bytes() == b"layer placeholder"
    manifest = json.loads((root / "vulkan" / "implicit_layer.d" / "csdm_overlay.json").read_text())
    assert manifest["layer"]["name"] == "VK_LAYER_csdm_overlay"
    assert manifest["layer"]["library_path"] == str(root / "vulkan" / "libcsdm_vulkan_layer.so")
    assert manifest["layer"]["enable_environment"] == {"ENABLE_CSDM_OVERLAY": "1"}
    assert installed.vulkan_layer_installed
    assert installed.env["ENABLE_CSDM_OVERLAY"] == "1"
    assert installed.env["VK_ADD_IMPLICIT_LAYER_PATH"] == str(root / "vulkan" / "implicit_layer.d")
    assert installed.env["XDG_DATA_DIRS"] == f"{root}:/usr/share"

    plugin.uninstall_plugin(settings)
    assert not root.exists()


def test_read_plugin_log_tail(tmp_path):
    settings = make_settings(tmp_path)
    assert plugin.read_plugin_log(settings) == ""
    (settings.bin_dir / "csdm.log").write_text("a" * 50 + "\n[10000] Executing: startmovie 1-sequence\n")
    assert plugin.read_plugin_log(settings, 40).endswith("startmovie 1-sequence")


def test_game_generated_files_do_not_outlive_the_session(tmp_path):
    """Regression from the first real recording: CS2 wrote ServerConfig.vdf and _iconcache/ into the plugin
    folder (it is the first search path), which kept `game/csgo/csdm` alive after the session."""
    settings = make_settings(tmp_path)
    root = settings.plugin_install_dir
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "server.dll").write_bytes(b"MZ")
    (root / "ServerConfig.vdf").write_text('"ServerConfig" {}', encoding="utf-8")
    (root / "_iconcache").mkdir()
    (root / "_iconcache" / "x.png").write_bytes(b"x")
    (root / "movie").mkdir()
    (root / "movie" / "1-sequence00000001.tga").write_bytes(b"x")

    plugin.uninstall_plugin(settings)
    assert not (root / "bin").exists() and not (root / "ServerConfig.vdf").exists()
    assert not (root / "_iconcache").exists()
    assert (root / "movie").exists()  # raw frames are the encoder's to delete

    (root / "movie" / "1-sequence00000001.tga").unlink()
    (root / "movie").rmdir()
    plugin.remove_game_generated(settings)
    assert not root.exists()
    plugin.remove_game_generated(settings)  # idempotent
