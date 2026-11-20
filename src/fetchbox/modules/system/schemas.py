from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class VersionInfo(BaseModel):
    version: str
    python: str
    platform: str
    libtorrent: str | None


class StatusInfo(BaseModel):
    version: str
    started_at: datetime
    uptime_seconds: float
    database: str
    schema_revision: str | None
    schema_head: str | None
    event_subscribers: int
