"""Runtime configuration. Every field can be set with a ``DEMOREEL_<NAME>`` environment variable or in ``.env``."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DEMOREEL_", env_file=".env", extra="ignore")

    data_dir: Path = Field(Path("data"), description="Where the database, demos, analyses and videos live.")
    database_url: str | None = Field(None, description="SQLAlchemy URL. Default: SQLite file inside data_dir.")
    host: str = "127.0.0.1"
    port: int = 8000
    public_url: str | None = Field(None, description="Base URL used in links returned by the API / MCP tools.")
    api_key: str | None = Field(None, description="If set, /api and /mcp require it (Bearer or X-API-Key).")
    admin_key: str | None = Field(None, description=(
        "If set, *changing* anything (POST/PATCH/PUT/DELETE on /api, and all of /mcp) requires this key, while "
        "reading stays open. Use it when the UI is shown to the public, e.g. embedded in a community's site."))
    max_upload_mb: int = 2048
    max_json_kb: int = Field(1024, description="Largest JSON body a request may carry (a demo upload has its own "
                                               "limit, max_upload_mb).")
    allowed_hosts: list[str] = Field(default_factory=list, description=(
        "Host names this server answers to besides localhost, `host` and the host of `public_url` (a LAN "
        "address, a second domain…). While no key is set, a request naming any other host is refused: that "
        "stops a web page from reaching this server through a DNS trick. [\"*\"] accepts any name."))
    cors_origins: list[str] = Field(default_factory=list, description=(
        "Other sites whose pages may call this API from the browser, e.g. [\"https://mixdavila.example\"]. "
        "This server's own names and the `ui.embed.parents` of the brand kits are always allowed; while a key "
        "is set, any site may also read (GET)."))

    workers: int = Field(1, description="Worker processes started by `demoreel serve`.")
    worker_max_file_mb: int = Field(1024, description="Largest file a remote worker may send back (a finished "
                                                      "video). See docs/workers.md.")
    default_renderer: str = Field("auto", description="'auto' = in-game POV when available, else 2D radar.")
    auto_render_top: int = Field(3, description="Automatically render the N best highlights after analysis.")
    render_width: int = 1280
    render_height: int = 720
    render_fps: int = 30

    ingame_capture_width: int = Field(1920, description="First-person footage is recorded at this size and kept; "
                                                        "16:9, 9:16 and 1:1 videos are all cut from it.")
    ingame_capture_height: int = 1080
    ingame_capture_fps: int = Field(30, description="Minimum recording frame rate. Recording time grows with it: "
                                                    "60 doubles the time of 30 but lets a later 0.5x slow-motion "
                                                    "edit reuse the footage. Plays already in slow motion are "
                                                    "recorded at the rate they need whatever this says.")
    brand: str | None = Field(None, description="Brand kit (folder in <data_dir>/brands) this instance uses for "
                                               "its videos and UI. Default: the only kit present, if any.")
    ingame_enabled: bool = Field(False, description="Let workers on this machine record with the real CS2 client.")
    import_dirs: list[Path] = Field(default_factory=list,
                                    description="Server-side folders demos may be imported from by path.")
    offline: bool = Field(False, description="Never download anything (radar images).")
    radar_images_url: str = "https://github.com/pnxenopoulos/awpy-data/releases/latest/download/images.zip"
    mcp_allowed_hosts: list[str] = Field(default_factory=list,
                                         description="Extra Host headers accepted by the MCP endpoint (and, "
                                                     "like allowed_hosts, by the rest of the server).")
    timezone: str | None = Field(None, description="IANA time zone (e.g. America/Sao_Paulo) used to group "
                                                   "matches into nights. Default: this machine's time zone.")
    publish_target: str | None = Field(None, description="Read-only mirror `demoreel publish` copies to, as "
                                                         "'[user@]host:/absolute/path/to/data' (SSH).")
    publish_restart: str | None = Field(None, description="Command run on the mirror after its database is "
                                                          "replaced, e.g. 'sudo systemctl restart demoreel'.")
    publish_renderers: list[str] | None = Field(None, description="Only videos of these renderers go to the mirror "
                                                                  "(e.g. [\"ingame\"]); default: all of them.")

    @property
    def db_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.data_dir / 'demoreel.db').resolve().as_posix()}"

    @property
    def base_url(self) -> str:
        return (self.public_url or f"http://{self.host}:{self.port}").rstrip("/")

    def path(self, *parts: str) -> Path:
        p = self.data_dir.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def keyed(self) -> bool:
        """True when a key guards something here (the whole API, or changing things)."""
        return bool(self.api_key or self.admin_key)

    def own_hosts(self) -> set[str]:
        """Host names (lowercase, no port, IPv6 in brackets) this server knows it is reached by."""
        names = {"localhost", "127.0.0.1", "[::1]", self.host, *self.allowed_hosts, *self.mcp_allowed_hosts}
        if self.public_url:
            names.add(urlparse(self.public_url).hostname or "")
        return {n for n in map(host_name, names) if n}

    def trusted_hosts(self) -> set[str] | None:
        """Host names requests may name, or None when any name goes: `["*"]` was configured, or the server
        listens on every interface and was told none of its names (nothing to compare with)."""
        names = self.own_hosts()
        if "*" in names:
            return None
        told = bool(self.public_url or self.allowed_hosts or self.mcp_allowed_hosts)
        if host_name(self.host) in {"0.0.0.0", "[::]"} and not told:
            return None
        return names


def host_name(value: str) -> str:
    """``Example.org:8000`` -> ``example.org``; ``::1`` and ``[::1]:8000`` -> ``[::1]``."""
    v = value.strip().lower()
    if v.startswith("["):
        return v.split("]", 1)[0] + "]" if "]" in v else v
    if v.count(":") > 1:  # a bare IPv6 address
        return f"[{v}]"
    return v.split(":", 1)[0]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return s
