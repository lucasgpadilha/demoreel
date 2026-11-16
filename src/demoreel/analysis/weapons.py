"""Weapon naming and classification helpers.

Demo events use internal names (``ak47``, ``m4a1_silencer``, ``hkp2000``...). This module maps them to
display names and coarse classes used by the stats and highlight scoring.
"""

from __future__ import annotations

_DISPLAY = {
    "ak47": "AK-47", "m4a1": "M4A4", "m4a1_silencer": "M4A1-S", "m4a4": "M4A4", "awp": "AWP",
    "ssg08": "SSG 08", "aug": "AUG", "sg556": "SG 553", "famas": "FAMAS", "galilar": "Galil AR",
    "scar20": "SCAR-20", "g3sg1": "G3SG1",
    "deagle": "Desert Eagle", "revolver": "R8 Revolver", "usp_silencer": "USP-S", "hkp2000": "P2000",
    "glock": "Glock-18", "p250": "P250", "fiveseven": "Five-SeveN", "tec9": "Tec-9", "cz75a": "CZ75-Auto",
    "elite": "Dual Berettas",
    "mac10": "MAC-10", "mp9": "MP9", "mp7": "MP7", "mp5sd": "MP5-SD", "ump45": "UMP-45", "p90": "P90",
    "bizon": "PP-Bizon",
    "nova": "Nova", "xm1014": "XM1014", "mag7": "MAG-7", "sawedoff": "Sawed-Off", "m249": "M249",
    "negev": "Negev",
    "hegrenade": "HE Grenade", "inferno": "Fire", "molotov": "Molotov", "incgrenade": "Incendiary",
    "flashbang": "Flashbang", "smokegrenade": "Smoke", "decoy": "Decoy",
    "taser": "Zeus x27", "knife": "Knife", "bayonet": "Knife", "planted_c4": "C4", "c4": "C4",
    "world": "World", "worldspawn": "World",
}

_CLASS = {
    "rifle": {"ak47", "m4a1", "m4a1_silencer", "m4a4", "aug", "sg556", "famas", "galilar"},
    "sniper": {"awp", "ssg08", "scar20", "g3sg1"},
    "pistol": {"deagle", "revolver", "usp_silencer", "hkp2000", "glock", "p250", "fiveseven", "tec9", "cz75a", "elite"},
    "smg": {"mac10", "mp9", "mp7", "mp5sd", "ump45", "p90", "bizon"},
    "heavy": {"nova", "xm1014", "mag7", "sawedoff", "m249", "negev"},
    "grenade": {"hegrenade", "inferno", "molotov", "incgrenade", "flashbang", "smokegrenade", "decoy"},
    "equipment": {"taser"},
}

_NON_BULLET = {"hegrenade", "inferno", "molotov", "incgrenade", "flashbang", "smokegrenade", "decoy", "knife",
               "taser", "world", "worldspawn", "planted_c4", "c4"}


def normalize(weapon: str | None) -> str:
    """Lower-case internal name without the ``weapon_`` prefix; every knife variant becomes ``knife``."""
    if not weapon or not isinstance(weapon, str):
        return "world"
    w = weapon.lower().removeprefix("weapon_")
    if "knife" in w or w in {"bayonet", "karambit"} or w.startswith("bayonet"):
        return "knife"
    return w


def display_name(weapon: str | None) -> str:
    w = normalize(weapon)
    return _DISPLAY.get(w, w.replace("_", " ").title())


def weapon_class(weapon: str | None) -> str:
    w = normalize(weapon)
    if w == "knife":
        return "knife"
    for cls, names in _CLASS.items():
        if w in names:
            return cls
    return "other"


def is_bullet_weapon(weapon: str | None) -> bool:
    return normalize(weapon) not in _NON_BULLET
