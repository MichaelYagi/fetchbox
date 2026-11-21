from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from fetchbox.core.concurrency import VersionMixin
from fetchbox.core.db import Base, IdMixin, UTCDateTime, utcnow


class Indexer(IdMixin, VersionMixin, Base):
    """A configured torrent source. ``settings`` holds the provider's settings, sealed."""

    __tablename__ = "indexer"

    definition_id: Mapped[str | None] = mapped_column(
        ForeignKey("indexer_definition.id"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(100), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=25)
    settings: Mapped[dict[str, Any]] = mapped_column(JSON)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    disabled_until: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class IndexerDefinition(IdMixin, Base):
    """A YAML indexer definition: built in (synced from the package) or added by a user."""

    __tablename__ = "indexer_definition"

    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    version: Mapped[str] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(10))  # builtin | user
    yaml: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
