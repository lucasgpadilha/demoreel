"""Everything visual that depends on the brand kit: colours, type, the logo mark, sprites and the few words."""

from __future__ import annotations

import numpy as np
import skia

from ...analysis import weapons
from ...brand import Brand
from .. import icons
from . import art
from .canvas import argb, blend, shared_type

WORDS = {
    "pt": {"round": "Round {n}", "t_killed": "TRs eliminados", "ct_killed": "CTs eliminados",
           "bomb_defused": "Bomba desarmada", "bomb_exploded": "Bomba explodiu", "time": "Tempo esgotado",
           "won": "Round vencido", "play": "Jogada {i} de {n}"},
    "en": {"round": "Round {n}", "t_killed": "Ts eliminated", "ct_killed": "CTs eliminated",
           "bomb_defused": "Bomb defused", "bomb_exploded": "Bomb exploded", "time": "Time ran out",
           "won": "Round won", "play": "Play {i} of {n}"},
}

# names of the weapon in a player's hands (demo "active_weapon_name") -> icon name
_HANDS = {name.lower(): key for key, name in weapons._DISPLAY.items() if key not in ("m4a4", "bayonet", "inferno")}
_HANDS.update({"smoke grenade": "smokegrenade", "high explosive grenade": "hegrenade",
               "incendiary grenade": "incgrenade", "decoy grenade": "decoy", "c4 explosive": "c4", "c4": "c4",
               "m4a4": "m4a1", "zeus x27": "taser", "knife": "knife"})
_KNIFE_WORDS = ("knife", "bayonet", "karambit", "daggers", "dagger")


def hand_icon(weapon_name: str) -> str | None:
    """Icon name for what a player holds, or None when we do not know it (the name is then drawn as text)."""
    key = (weapon_name or "").strip().lower()
    if not key:
        return None
    if key in _HANDS:
        return _HANDS[key]
    if any(w in key for w in _KNIFE_WORDS):
        return "knife"
    return None


def map_title(map_name: str) -> str:
    """de_nuke -> Nuke."""
    base = map_name.split("/")[-1]
    for prefix in ("de_", "cs_", "ar_", "dm_", "gg_"):
        if base.startswith(prefix):
            base = base[len(prefix):]
            break
    return base.replace("_", " ").strip().title() or map_name


class Theme:
    def __init__(self, brand: Brand, width: int, height: int):
        self.brand = brand
        self.c = brand.colors
        self.ty = shared_type(brand.fonts.display, brand.fonts.body)
        self.words = WORDS["pt" if brand.locale.lower().startswith("pt") else "en"]
        self.accent = self.c.accent
        self.mark: skia.Image | None = None
        if brand.logo_path is not None:
            try:
                from ...compose.logo import load_logo

                lg = load_logo(brand.logo_path, brand.logo_background)
                x0, y0, x1, y1 = lg.bbox
                raw = skia.Image.fromarray(np.ascontiguousarray(lg.rgba[y0:y1, x0:x1]),
                                           colorType=skia.kRGBA_8888_ColorType,
                                           alphaType=skia.kUnpremul_AlphaType).withDefaultMipmaps()
                # drawn small every frame: keep it at about its drawn size, in the surface's pixel format
                mh = max(8, int(round(min(width, height) * 0.16)))
                mw = max(8, int(round(mh * raw.width() / max(1, raw.height()))))
                surface = skia.Surface(mw, mh)
                surface.getCanvas().clear(skia.ColorTRANSPARENT)
                surface.getCanvas().drawImageRect(
                    raw, skia.Rect.MakeWH(mw, mh), skia.SamplingOptions(skia.FilterMode.kLinear, skia.MipmapMode.kLinear))
                self.mark = surface.makeImageSnapshot()
                if lg.accent_color:
                    self.accent = "#{:02x}{:02x}{:02x}".format(*lg.accent_color)
            except Exception:  # noqa: BLE001 - a broken logo must not break a render
                self.mark = None
        self.side = {"CT": self.c.ct, "T": self.c.t}
        self.side_bright = {k: blend(v, self.c.text, 0.28) for k, v in self.side.items()}
        self.smoke = [art.smoke_sprite(self.c, 3), art.smoke_sprite(self.c, 8)]
        self.flame = art.flame_sprite()
        self.glow = art.glow_sprite("#ffffff")
        self.glow_warm = art.glow_sprite("#ffc070")
        self.vignette = art.vignette(width, height, self.c)
        self.spotlight = art.spotlight(self.c)
        self.linear = skia.SamplingOptions(skia.FilterMode.kLinear)

    def side_color(self, side: str | None) -> str:
        return self.side.get(side or "", self.c.muted)

    def col(self, hex_color: str, alpha: float = 1.0) -> int:
        return argb(hex_color, alpha)

    def icon(self, name: str, height: float, hex_color: str) -> skia.Image | None:
        return icons.icon(name, max(2, int(round(height))), argb(hex_color))
