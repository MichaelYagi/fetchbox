from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from fetchbox.core.concurrency import VersionMixin
from fetchbox.core.db import Base, IdMixin, UTCDateTime, utcnow


class MediaServer(IdMixin, VersionMixin, Base):
    """A Plex / Jellyfin / Emby server to tell about new and changed files."""

    __tablename__ = "media_server"

    kind: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(100), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    settings: Mapped[dict[str, Any]] = mapped_column(JSON)  # secrets sealed
    # [{local, remote}]: where the server sees Fetchbox's folders under other paths
    path_mappings: Mapped[list[dict[str, str]]] = mapped_column(JSON, default=list)
    last_refresh_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
