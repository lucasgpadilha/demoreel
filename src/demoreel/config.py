"""Runtime configuration. Every field can be set with a ``DEMOREEL_<NAME>`` environment variable or in ``.env``."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

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
                                         description="Extra Host headers accepted by the MCP endpoint.")
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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return s
