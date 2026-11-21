from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class VersionInfo(BaseModel):
    version: str
    python: str
    platform: str
    libtorrent: str | None


class HostInfo(BaseModel):
    """Where Fetchbox runs: every path it shows is on this machine."""

    name: str = Field(description="Host name")
    os: str = Field(description="e.g. Linux, Windows, macOS")
    wsl_distro: str | None = Field(
        default=None, description="Set when running in WSL: paths are inside that distro"
    )
    data_dir: str
    downloads_root: str


class StatusInfo(BaseModel):
    version: str
    started_at: datetime
    uptime_seconds: float
    database: str
    schema_revision: str | None
    schema_head: str | None
    event_subscribers: int
    host: HostInfo | None = None
