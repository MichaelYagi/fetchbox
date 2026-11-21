"""Configuration loading.

Precedence (highest first): CLI flags -> environment (FETCHBOX_*) -> config.toml in the
data dir -> DB settings (applied later by the system module) -> defaults.

The data dir is resolved first (CLI, then FETCHBOX_DATA_DIR, then the platform default)
because it decides where config.toml lives.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

CONFIG_FILENAME = "config.toml"


def default_data_dir() -> Path:
    """Per-OS data directory from the spec's deployment table."""
    home = Path.home()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
        return Path(base) / "Fetchbox"
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Fetchbox"
    xdg = os.environ.get("XDG_DATA_HOME") or str(home / ".local" / "share")
    return Path(xdg) / "fetchbox"


def default_downloads_root() -> Path:
    """The OS Downloads folder plus "Fetchbox" (spec: zero-config default)."""
    home = Path.home()
    if sys.platform != "win32" and sys.platform != "darwin":
        xdg = os.environ.get("XDG_DOWNLOAD_DIR")
        if xdg:
            return Path(xdg).expanduser() / "Fetchbox"
    return home / "Downloads" / "Fetchbox"


def resolve_data_dir(cli_value: str | os.PathLike[str] | None = None) -> Path:
    raw = cli_value or os.environ.get("FETCHBOX_DATA_DIR")
    return Path(raw).expanduser().resolve() if raw else default_data_dir()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FETCHBOX_", extra="ignore")

    data_dir: Path = Field(default_factory=default_data_dir)
    host: str = "127.0.0.1"
    port: int = Field(default=8787, ge=1, le=65535)
    db_url: str | None = None
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    log_format: Literal["auto", "json", "console"] = "auto"
    run_migrations: bool = True
    downloads_root: Path | None = Field(
        default=None,
        description="Where torrents and the default library go; default ~/Downloads/Fetchbox",
    )
    seed_indexers: bool = Field(
        default=True,
        description="On first run, add an indexer for each built-in definition needing no account",
    )
    torrent_isolated: bool = Field(
        default=False,
        description="Built-in engine on loopback only, no DHT/LSD/UPnP (tests, development)",
    )
    tmdb_api_key: SecretStr | None = Field(
        default=None, description="Overrides the TMDB key stored in /system/config"
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        toml_path = _TOML_PATH_CTX.get("path")
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings]
        if toml_path is not None and toml_path.is_file():
            sources.append(TomlConfigSettingsSource(settings_cls, toml_file=toml_path))
        return tuple(sources)

    @property
    def database_url(self) -> str:
        if self.db_url:
            return self.db_url
        return f"sqlite+aiosqlite:///{(self.data_dir / 'fetchbox.db').as_posix()}"

    @property
    def downloads_dir(self) -> Path:
        return self.downloads_root or default_downloads_root()

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def is_loopback(self) -> bool:
        return self.host in {"127.0.0.1", "::1", "localhost"}


# pydantic-settings builds sources at class level; pass the toml path through here.
_TOML_PATH_CTX: dict[str, Path | None] = {"path": None}


def load_settings(data_dir: str | os.PathLike[str] | None = None, **cli: Any) -> Settings:
    """Build settings. `cli` holds only flags the user actually passed (None values dropped)."""
    resolved = resolve_data_dir(data_dir)
    overrides = {k: v for k, v in cli.items() if v is not None}
    overrides["data_dir"] = resolved
    _TOML_PATH_CTX["path"] = resolved / CONFIG_FILENAME
    try:
        return Settings(**overrides)
    finally:
        _TOML_PATH_CTX["path"] = None
