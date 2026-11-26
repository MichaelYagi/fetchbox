from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, String, Text, UniqueConstraint
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


class PluginRecord(Base):
    """A plugin Fetchbox has found: whether it may run, its settings and its last error.
    One found but never approved (copied into the plugins folder by hand) stays off."""

    __tablename__ = "plugin"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # the plugin's id
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="installed")  # file | installed
    # per contribution id: its settings, Secret fields encrypted
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)  # why it isn't running
    found_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
