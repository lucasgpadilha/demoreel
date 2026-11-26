"""Steam / CS2 detection of the in-game renderer, against fake folder trees."""

from __future__ import annotations

from pathlib import Path

from demoreel.render.ingame import settings as ingame_settings
from demoreel.render.ingame.settings import IngameSettings

LIBRARYFOLDERS = r'''
"libraryfolders"
{
	"0"
	{
		"path"		"__STEAM__"
		"label"		""
		"apps"
		{
			"228980"		"140573312"
		}
	}
	// second library, the one with CS2
	"1"
	{
		"path"		"__LIB__"
		"apps"
		{
			"730"		"73963861286"
		}
	}
}
'''


def make_cs2(library: Path) -> Path:
    root = library / "steamapps" / "common" / "Counter-Strike Global Offensive"
    (root / "game" / "csgo").mkdir(parents=True)
    (root / "game" / "csgo" / "gameinfo.gi").write_text('"GameInfo"\n{\n}\n')
    return root


def make_steam(tmp_path: Path) -> tuple[Path, Path]:
    steam, library = tmp_path / "Steam", tmp_path / "SteamLibrary"
    (steam / "steamapps").mkdir(parents=True)
    vdf = (LIBRARYFOLDERS.replace("__STEAM__", str(steam).replace("\\", "\\\\"))
           .replace("__LIB__", str(library).replace("\\", "\\\\")))
    (steam / "steamapps" / "libraryfolders.vdf").write_text(vdf)
    return steam, library


def test_parse_vdf_handles_nesting_escapes_and_comments():
    data = ingame_settings.parse_vdf(LIBRARYFOLDERS.replace("__STEAM__", r"C:\\Program Files (x86)\\Steam")
                                     .replace("__LIB__", r"D:\\Games \"fast\""))
    folders = data["libraryfolders"]
    assert folders["0"]["path"] == r"C:\Program Files (x86)\Steam"
    assert folders["1"]["path"] == 'D:\\Games "fast"'
    assert folders["1"]["apps"] == {"730": "73963861286"}
    assert ingame_settings.parse_vdf("") == {}
    assert ingame_settings.parse_vdf('"a" { "b" ') == {"a": {}}  # truncated file: no exception


def test_cs2_is_found_through_libraryfolders(tmp_path):
    steam, library = make_steam(tmp_path)
    cs2 = make_cs2(library)
    assert ingame_settings.steam_library_dirs(steam) == [library, steam]  # the library owning app 730 first
    assert ingame_settings.detect_cs2_dir(steam) == cs2
    assert ingame_settings.detect_cs2_dir(None) is None
    assert ingame_settings.detect_cs2_dir(tmp_path / "missing") is None


def test_cs2_dir_can_be_given_as_root_game_or_csgo_folder(tmp_path):
    cs2 = make_cs2(tmp_path / "lib")
    for given in (cs2, cs2 / "game", cs2 / "game" / "csgo"):
        assert ingame_settings.normalize_cs2_dir(given) == cs2
    assert ingame_settings.normalize_cs2_dir(tmp_path) is None


def test_load_reads_the_environment(tmp_path):
    steam, library = make_steam(tmp_path)
    cs2 = make_cs2(library)
    (steam / "steam.exe").write_bytes(b"")
    env = {
        "DEMOREEL_CS2_DIR": str(cs2 / "game" / "csgo"),
        "DEMOREEL_STEAM_EXE": str(steam / "steam.exe"),
        "DEMOREEL_INGAME_PLUGIN_DIR": str(tmp_path / "my plugins"),
        "DEMOREEL_INGAME_TIMEOUT_S": "900",
        "DEMOREEL_INGAME_PLUGIN_VERSION": "14168",
    }
    s = ingame_settings.load(env, data_dir=tmp_path / "data", platform="win32")
    assert s.cs2_dir == cs2 and s.steam_dir == steam and s.steam_exe == steam / "steam.exe"
    assert s.plugin_dir == tmp_path / "my plugins"
    assert s.timeout_s == 900 and s.plugin_version == "14168"
    assert s.data_dir == (tmp_path / "data" / "ingame").absolute()
    assert s.lock_path == s.data_dir / "recording.lock" and s.cfg_dir == s.data_dir / "cfg"


def test_load_auto_detects_cs2_from_the_steam_folder_and_defaults_the_plugin_dir(tmp_path):
    steam, library = make_steam(tmp_path)
    cs2 = make_cs2(library)
    (steam / "steam.exe").write_bytes(b"")
    s = ingame_settings.load({"DEMOREEL_STEAM_EXE": str(steam / "steam.exe")}, data_dir=tmp_path / "data",
                             platform="win32")
    assert s.cs2_dir == cs2
    assert s.plugin_dir == (tmp_path / "data").absolute() / "cache" / "csdm-plugin"
    assert s.timeout_s is None and s.plugin_version is None


def test_load_never_raises_on_garbage(tmp_path):
    env = {"DEMOREEL_CS2_DIR": "\x00::not a path::", "DEMOREEL_STEAM_EXE": str(tmp_path / "nope" / "steam.exe"),
           "DEMOREEL_INGAME_TIMEOUT_S": "soon", "DEMOREEL_INGAME_PLUGIN_DIR": "  "}
    s = ingame_settings.load(env, data_dir=tmp_path / "data", platform="linux")
    assert isinstance(s, IngameSettings)
    assert s.timeout_s is None
    assert s.plugin_dir == (tmp_path / "data").absolute() / "cache" / "csdm-plugin"


def test_linux_detection_and_runtime_script(tmp_path):
    home = tmp_path / "home"
    steam = home / ".local" / "share" / "Steam"
    (steam / "steamapps" / "common" / "SteamLinuxRuntime_sniper").mkdir(parents=True)
    entry = steam / "steamapps" / "common" / "SteamLinuxRuntime_sniper" / "_v2-entry-point"
    entry.write_text("#!/bin/sh\n")
    assert ingame_settings.detect_steam_dir("linux", home=home) == steam.resolve()
    assert ingame_settings.find_linux_runtime_script(steam) == entry
    assert ingame_settings.find_linux_runtime_script(None) is None
    assert ingame_settings.detect_steam_dir("linux", home=tmp_path / "empty") is None


def test_paths_inside_the_game_per_platform(tmp_path):
    win = IngameSettings(cs2_dir=tmp_path, steam_dir=None, steam_exe=None, plugin_dir=tmp_path, data_dir=tmp_path,
                         platform="win32")
    lin = IngameSettings(cs2_dir=tmp_path, steam_dir=None, steam_exe=None, plugin_dir=tmp_path, data_dir=tmp_path,
                         platform="linux")
    assert win.game_executable == tmp_path / "game" / "bin" / "win64" / "cs2.exe"
    assert lin.game_executable == tmp_path / "game" / "cs2.sh"
    assert win.bin_dir.name == "win64" and lin.bin_dir.name == "linuxsteamrt64"
    assert win.gameinfo_path == tmp_path / "game" / "csgo" / "gameinfo.gi"
    assert win.plugin_install_dir == tmp_path / "game" / "csgo" / "csdm"


def make_hlae(root: Path, version: str, *, hook: bool = True) -> Path:
    exe = root / version / "HLAE.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    if hook:
        (exe.parent / "x64").mkdir()
        (exe.parent / "x64" / "AfxHookSource2.dll").write_bytes(b"MZ")
    return exe


def test_hlae_is_chosen_by_version_number_and_only_where_it_can_hook_cs2(tmp_path):
    root = tmp_path / "data" / "cache" / "hlae"
    make_hlae(root, "2.193.2")
    make_hlae(root, "2.193.9")
    newest = make_hlae(root, "2.193.10")  # sorted as text it would lose to 2.193.9
    make_hlae(root, "backup")  # a name that is not a version never wins
    make_hlae(root, "2.194.0", hook=False)  # a partial extraction cannot record CS2

    s = ingame_settings.load({}, data_dir=tmp_path / "data", platform="win32")
    assert s.hlae_exe == newest.absolute() and s.hlae_available and s.hlae_version == "2.193.10"
    found = ingame_settings.hlae_installs(s.hlae_root)
    assert [h.version for h in found] == ["2.193.10", "2.193.9", "2.193.2", "backup", "2.194.0"]
    assert [h.usable for h in found] == [True, True, True, True, False]

    assert ingame_settings.version_key("2.193.10") > ingame_settings.version_key("2.193.9")
    assert ingame_settings.version_key("latest") < ingame_settings.version_key("0.1")


def test_hlae_without_its_hook_is_not_available_so_auto_records_with_the_game(tmp_path):
    root = tmp_path / "data" / "cache" / "hlae"
    make_hlae(root, "2.190.0", hook=False)
    s = ingame_settings.load({}, data_dir=tmp_path / "data", platform="win32")
    assert s.hlae_exe is None and not s.hlae_available

    given = make_hlae(tmp_path / "elsewhere", "hlae-nightly", hook=False)
    s = ingame_settings.load({"DEMOREEL_INGAME_HLAE_EXE": str(given)}, data_dir=tmp_path / "data",
                             platform="win32")
    assert s.hlae_exe == given and not s.hlae_available  # named on purpose, but it still cannot record CS2
    assert s.hlae_version is None  # "hlae-nightly" has no version number


def test_doctor_line_says_which_hlae_is_used_and_what_else_is_there():
    from demoreel.cli import _hlae_line

    details = {"hlae": "C:/hlae/2.193.10/HLAE.exe", "hlae_version": "2.193.10",
               "hlae_installs": [{"version": "2.193.10", "path": "C:/hlae/2.193.10/HLAE.exe", "usable": True},
                                 {"version": "2.193.9", "path": "C:/hlae/2.193.9/HLAE.exe", "usable": True},
                                 {"version": "old", "path": "C:/hlae/old/HLAE.exe", "usable": False}]}
    line = _hlae_line(details)
    assert line.startswith("2.193.10 at C:/hlae/2.193.10/HLAE.exe")
    assert "also found: 2.193.9, old (no x64/AfxHookSource2.dll" in line
    assert "not installed" in _hlae_line({"hlae": None, "hlae_installs": []})
