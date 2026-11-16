"""Brand kits: the identity a community / client gets on its videos and on the web UI.

A kit is a folder ``<data_dir>/brands/<slug>/`` holding ``brand.json`` and its assets (logo, optional music).
Kits are user data — they are never part of this repository. One instance usually serves one community, so
the *active* kit is ``DEMOREEL_BRAND``, or the only kit present.

Everything visual that is client-specific derives from the kit: the UI theme, the intro/outro bumpers
(generated procedurally from the logo and colours, see ``compose/bumper.py``), the video HUD and captions.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .config import get_settings

_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


class BrandColors(BaseModel):
    background: str = "#0d0f14"
    surface: str = "#141821"
    surface_raised: str = "#1c2230"
    border: str = "#2a3242"
    text: str = "#e9edf3"
    muted: str = "#8b94a7"
    accent: str = "#f5b041"
    accent_strong: str = "#e0932a"
    positive: str = "#4cd97b"
    negative: str = "#ff4d5e"
    ct: str = "#5aa9ff"
    t: str = "#f5b041"


class BrandFonts(BaseModel):
    display: str = "Barlow Condensed"
    body: str = "Barlow"


class BumperSpec(BaseModel):
    """How the generated intro / outro look and sound."""

    style: str = Field("assemble", description="assemble | strike | none")
    intro_seconds: float = Field(2.0, ge=0, le=8)
    outro_seconds: float = Field(2.8, ge=0, le=10)
    sound: bool = True
    cta: str | None = Field(None, description="Line shown on the outro, e.g. the site address.")
    intro_vertical: bool = Field(False, description="Play the intro on 9:16 videos too. Off by default: they "
                                                    "open on the play, the brand is in the header.")


class Brand(BaseModel):
    slug: str = "demoreel"
    name: str = "demoreel"
    tagline: str | None = None
    url: str | None = None
    logo: str | None = Field(None, description="Logo file inside the kit folder (PNG with or without alpha).")
    logo_background: str | None = Field(None, description="Colour to key out when the logo has no alpha channel.")
    colors: BrandColors = Field(default_factory=BrandColors)
    fonts: BrandFonts = Field(default_factory=BrandFonts)
    locale: str = "en"
    bumper: BumperSpec = Field(default_factory=BumperSpec)
    music: str | None = Field(None, description="Optional music bed file inside the kit folder.")
    watermark: bool = True
    chat: bool = Field(True, description="Show what the lobby typed about a play (masked for slurs) at the end of "
                                         "its first-person video. A render can override it: options.chat.")
    ui: dict[str, Any] = Field(default_factory=dict, description=(
        "Options for the web UI only (never for videos): colour overrides, display type, radius, header, "
        "buttons, embed settings… See docs/branding.md."))

    @property
    def dir(self) -> Path:
        return brands_dir() / self.slug

    @property
    def logo_path(self) -> Path | None:
        if not self.logo:
            return None
        p = (self.dir / self.logo).resolve()
        return p if p.is_file() and p.is_relative_to(self.dir.resolve()) else None

    @property
    def music_path(self) -> Path | None:
        if not self.music:
            return None
        p = (self.dir / self.music).resolve()
        return p if p.is_file() and p.is_relative_to(self.dir.resolve()) else None

    def fingerprint(self) -> str:
        """Changes whenever anything that affects generated media changes (used as a cache key)."""
        import hashlib

        # `ui` never reaches a video; `intro_vertical` only decides whether a bumper is used, not how it looks;
        # `chat` is drawn per render and never cached.
        h = hashlib.sha256(self.model_dump_json(
            exclude={"ui": True, "chat": True, "bumper": {"intro_vertical"}}).encode())
        for p in (self.logo_path, self.music_path):
            if p is not None:
                h.update(str(p.stat().st_mtime_ns).encode())
        return h.hexdigest()[:16]


def brands_dir() -> Path:
    d = get_settings().data_dir / "brands"
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_brands() -> list[str]:
    return sorted(p.name for p in brands_dir().iterdir() if (p / "brand.json").is_file() and _SLUG.match(p.name))


@lru_cache(maxsize=32)
def _load(slug: str, mtime: float) -> Brand:
    data: dict[str, Any] = json.loads((brands_dir() / slug / "brand.json").read_text(encoding="utf-8"))
    data["slug"] = slug
    return Brand.model_validate(data)


def load_brand(slug: str | None) -> Brand | None:
    if not slug or not _SLUG.match(slug):
        return None
    f = brands_dir() / slug / "brand.json"
    if not f.is_file():
        return None
    return _load(slug, f.stat().st_mtime)


def active_brand() -> Brand:
    """The kit this instance uses by default; a neutral built-in identity when none is configured."""
    slug = get_settings().brand
    if not slug:
        kits = list_brands()
        slug = kits[0] if len(kits) == 1 else None
    return load_brand(slug) or Brand()


def brand_for(slug: str | None) -> Brand:
    return load_brand(slug) or active_brand()


def save_brand(brand: Brand) -> Path:
    if not _SLUG.match(brand.slug):
        raise ValueError("Brand slug must be lowercase letters, digits, '-' or '_'.")
    brand.dir.mkdir(parents=True, exist_ok=True)
    f = brand.dir / "brand.json"
    f.write_text(json.dumps(brand.model_dump(exclude={"slug"}), indent=2, ensure_ascii=False), encoding="utf-8")
    return f
