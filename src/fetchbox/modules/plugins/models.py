from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from fetchbox.core.db import Base, IdMixin, UTCDateTime, utcnow


class PluginValue(IdMixin, Base):
    """One value a plugin keeps (``await ctx.store.set("last_sync", ...)``). In the database,
    so it's in backups; removed with the plugin."""

    __tablename__ = "plugin_value"

    plugin: Mapped[str] = mapped_column(String(64), index=True)
    key: Mapped[str] = mapped_column(String(200))
    value: Mapped[Any] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (UniqueConstraint("plugin", "key", name="uq_plugin_value_plugin_key"),)
