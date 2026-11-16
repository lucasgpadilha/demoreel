"""The game's own HUD icons (weapons, kill-feed marks), taken from a local CS2 install.

Players read a kill feed by its icons, so videos should use the real ones. They are Valve's artwork, so they
are **not** shipped with this project: on first use they are extracted from the CS2 files on this machine
(``game/csgo/pak01_dir.vpk``) into ``<data_dir>/cache/icons`` as plain SVG. A server without the game can be
given that folder (copy it, or point ``DEMOREEL_ICONS_DIR`` at one). With no icons available every caller
falls back to text — nothing fails.

``icon("ak47", 28)`` returns a white ``skia.Image`` 28 px tall, or None.
"""

from __future__ import annotations

import logging
import os
import re
import struct
from functools import lru_cache
from pathlib import Path

import skia

from ..config import get_settings

log = logging.getLogger("demoreel.icons")

# kill-feed marks: our name -> file name in panorama/images/hud/deathnotice
MARKS = {"headshot": "icon_headshot", "wallbang": "penetrate", "smoke": "smoke_kill", "blind": "blind_kill",
         "noscope": "noscope", "airborne": "inairkill", "suicide": "icon_suicide", "domination": "domination",
         "revenge": "revenge"}
# analysis weapon names that are filed under a different icon name
ALIASES = {"world": "icon_suicide", "worldspawn": "icon_suicide", "inferno": "molotov", "planted_c4": "c4",
           "knife": "knife", "m4a4": "m4a1", "incgrenade": "incgrenade"}
_WANTED = ("panorama/images/icons/equipment", "panorama/images/hud/deathnotice")
_extracted = False


def icons_dir() -> Path:
    env = os.environ.get("DEMOREEL_ICONS_DIR")
    d = Path(env) if env else get_settings().data_dir / "cache" / "icons"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cs2_csgo_dir() -> Path | None:
    try:
        from .ingame import settings as ingame_settings

        s = ingame_settings.load()
        return s.csgo_dir if s.cs2_dir is not None and s.csgo_dir.is_dir() else None
    except Exception:  # noqa: BLE001 - icon lookup must never break a render
        return None


def extract_from_game(csgo_dir: Path, out: Path) -> int:
    """Copy the wanted SVGs out of the game's VPK archives. Read-only on the game folder. Returns the count."""
    index = csgo_dir / "pak01_dir.vpk"
    d = index.read_bytes()
    sig, ver, tree_size = struct.unpack_from("<III", d, 0)
    if sig != 0x55AA1234 or ver not in (1, 2):
        raise ValueError("unexpected VPK header")
    pos = 28 if ver == 2 else 12
    data_base, tree_end = pos + tree_size, pos + tree_size

    def cstr(p: int) -> tuple[str, int]:
        e = d.index(b"\0", p)
        return d[p:e].decode("utf-8", "replace"), e + 1

    wanted: list[tuple[str, int, int, int, bytes]] = []
    while pos < tree_end:
        ext, pos = cstr(pos)
        if not ext:
            break
        while True:
            path, pos = cstr(pos)
            if not path:
                break
            while True:
                name, pos = cstr(pos)
                if not name:
                    break
                _crc, preload, arch, off, length, _term = struct.unpack_from("<IHHIIH", d, pos)
                pos += 18
                pre = d[pos:pos + preload]
                pos += preload
                if ext == "vsvg_c" and path in _WANTED:
                    wanted.append((name, arch, off, length, pre))
    n = 0
    handles: dict[int, object] = {}
    try:
        for name, arch, off, length, pre in wanted:
            if arch == 0x7FFF:
                raw = pre + d[data_base + off: data_base + off + length]
            else:
                f = handles.get(arch)
                if f is None:
                    f = handles[arch] = open(csgo_dir / f"pak01_{arch:03d}.vpk", "rb")  # noqa: SIM115
                f.seek(off)
                raw = pre + f.read(length)
            # A compiled .vsvg_c carries the original SVG text verbatim after its resource header.
            i, j = raw.find(b"<svg"), raw.rfind(b"</svg>")
            if i < 0 or j < 0:
                continue
            safe = re.sub(r"[^a-z0-9_]", "_", name.lower())
            (out / f"{safe}.svg").write_bytes(raw[i:j + 6])
            n += 1
    finally:
        for f in handles.values():
            f.close()
    return n


def _ensure() -> None:
    global _extracted
    if _extracted:
        return
    _extracted = True
    out = icons_dir()
    if any(out.glob("*.svg")):
        return
    if os.environ.get("DEMOREEL_ICONS_FROM_GAME", "1").strip().lower() in {"0", "false", "no", "off"}:
        return  # tests and locked-down servers: never read the game's files
    csgo = _cs2_csgo_dir()
    if csgo is None:
        log.info("no CS2 install found: kill feeds will use text instead of icons")
        return
    try:
        log.info("extracted %d HUD icons from %s", extract_from_game(csgo, out), csgo)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not extract HUD icons from the game files: %s", exc)


def available() -> bool:
    _ensure()
    return any(icons_dir().glob("*.svg"))


def icon_name(weapon_or_mark: str) -> str:
    key = weapon_or_mark.lower().removeprefix("weapon_")
    return MARKS.get(key) or ALIASES.get(key) or key


@lru_cache(maxsize=512)
def icon(name: str, height: int, color: int = 0xFFFFFFFF) -> skia.Image | None:
    """Rasterised icon ``height`` px tall in ``color`` (ARGB int), or None if we do not have it."""
    _ensure()
    path = icons_dir() / f"{re.sub(r'[^a-z0-9_]', '_', icon_name(name))}.svg"
    if not path.is_file() or height < 2:
        return None
    try:
        dom = skia.SVGDOM.MakeFromStream(skia.MemoryStream(path.read_bytes()))
        if dom is None:
            return None
        size = dom.containerSize()
        if size.height() <= 0 or size.width() <= 0:
            return None
        k = height / size.height()
        w = max(1, int(round(size.width() * k)))
        surface = skia.Surface(w, int(height))
        cv = surface.getCanvas()
        cv.clear(skia.ColorTRANSPARENT)
        cv.scale(k, k)
        dom.render(cv)
        # The game's icons are drawn in white/grey; flatten them to one colour through their alpha.
        tinted = skia.Surface(w, int(height))
        tc = tinted.getCanvas()
        tc.clear(skia.ColorTRANSPARENT)
        tc.drawImage(surface.makeImageSnapshot(), 0, 0, skia.SamplingOptions(),
                     skia.Paint(ColorFilter=skia.ColorFilters.Blend(color, skia.BlendMode.kSrcIn)))
        return tinted.makeImageSnapshot()
    except Exception as exc:  # noqa: BLE001
        log.debug("icon %s failed: %s", name, exc)
        return None
