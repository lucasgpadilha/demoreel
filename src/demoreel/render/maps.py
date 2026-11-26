"""Map calibration (world units -> radar pixels) and radar image cache.

Radar images are Valve assets, so they are **not** shipped in this repository. On first use they are
downloaded from the awpy-data project releases (which extracts them from the game files) into
``<data_dir>/cache/radars``. Set ``DEMOREEL_OFFLINE=1`` to disable the download — the 2D renderer then
draws on a plain background. You can also drop your own ``<map>.png`` (1024x1024) into that folder.

Only the numeric calibration lives here: radar pixel = ((x - pos_x) / scale, (pos_y - y) / scale).
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..config import get_settings

log = logging.getLogger("demoreel.maps")

RADAR_SIZE = 1024


@dataclass(frozen=True)
class MapInfo:
    name: str
    pos_x: float
    pos_y: float
    scale: float
    lower_below_z: float | None = None  # players below this altitude are on the lower level (Nuke, Vertigo…)

    def to_radar(self, x: float, y: float) -> tuple[float, float]:
        return (x - self.pos_x) / self.scale, (self.pos_y - y) / self.scale

    def is_lower(self, z: float) -> bool:
        return self.lower_below_z is not None and z < self.lower_below_z


_CAL: dict[str, tuple[float, float, float, float | None]] = {
    "ar_baggage": (-1316.0, 1288.0, 2.539062, -5.0),
    "ar_shoots": (-1368.0, 1952.0, 2.6875, None),
    "ar_shoots_night": (-1368.0, 1952.0, 2.6875, None),
    "cs_italy": (-2647.0, 2592.0, 4.6, None),
    "cs_office": (-1838.0, 1858.0, 4.1, None),
    "de_ancient": (-2953.0, 2164.0, 5.0, None),
    "de_ancient_night": (-2953.0, 2164.0, 5.0, None),
    "de_anubis": (-2796.0, 3328.0, 5.22, None),
    "de_cache": (-2000.0, 3250.0, 5.5, None),
    "de_dust2": (-2476.0, 3239.0, 4.4, None),
    "de_inferno": (-2087.0, 3870.0, 4.9, None),
    "de_mirage": (-3230.0, 1713.0, 5.0, None),
    "de_nuke": (-3453.0, 2887.0, 7.0, -495.0),
    "de_overpass": (-4831.0, 1781.0, 5.2, None),
    "de_train": (-2308.0, 2078.0, 4.082077, -50.0),
    "de_vertigo": (-3168.0, 1762.0, 4.0, 11700.0),
}


def map_info(name: str) -> MapInfo | None:
    cal = _CAL.get(name)
    return MapInfo(name, *cal) if cal else None


def cache_dir() -> Path:
    d = get_settings().data_dir / "cache" / "radars"
    d.mkdir(parents=True, exist_ok=True)
    return d


_download_attempted = False


def _download_radars() -> None:
    global _download_attempted
    settings = get_settings()
    if _download_attempted or settings.offline:
        return
    _download_attempted = True
    try:
        log.info("downloading radar images from %s", settings.radar_images_url)
        r = httpx.get(settings.radar_images_url, follow_redirects=True, timeout=60)
        r.raise_for_status()
        want = (settings.radar_images_sha256 or "").strip().lower()
        got = hashlib.sha256(r.content).hexdigest()
        if want and got != want:
            log.warning("radar images from %s have SHA-256 %s, not the expected %s: not used. Set "
                        "DEMOREEL_RADAR_IMAGES_SHA256 together with DEMOREEL_RADAR_IMAGES_URL (empty skips the "
                        "check); rendering without map background", settings.radar_images_url, got, want)
            return
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            for member in zf.namelist():
                name = Path(member).name
                if name.lower().endswith(".png") and name == Path(name).name:
                    (cache_dir() / name).write_bytes(zf.read(member))
    except Exception as exc:  # noqa: BLE001 - radar images are optional
        log.warning("could not download radar images (%s); rendering without map background", exc)


_MAP_NAME = re.compile(r"^[a-z0-9_]+$")


def radar_image(name: str, lower: bool = False) -> Path | None:
    """Path of the radar PNG for a map (downloading the pack on first use), or None if unavailable.

    The name comes from the demo (or a remote worker's analysis), so only plain map names are looked up: a
    name such as ``../../x`` would otherwise read any PNG on the server."""
    if not isinstance(name, str) or not _MAP_NAME.match(name):
        return None
    fname = f"{name}_lower.png" if lower else f"{name}.png"
    p = cache_dir() / fname
    if not p.exists():
        _download_radars()
    return p if p.exists() else None
