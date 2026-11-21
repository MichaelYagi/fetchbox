from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from fetchbox.core.concurrency import VersionMixin
from fetchbox.core.db import Base, UTCDateTime, utcnow


class ConfigRow(VersionMixin, Base):
    """Runtime config edited through the API; a single row (id "system")."""

    __tablename__ = "app_config"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
