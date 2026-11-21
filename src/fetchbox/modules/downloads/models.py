from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from fetchbox.core.concurrency import VersionMixin
from fetchbox.core.db import Base, IdMixin, UTCDateTime, utcnow

STATUSES = ("queued", "downloading", "paused", "completed", "failed", "removed")
ACTIVE = ("queued", "downloading", "paused")


class DownloadClient(IdMixin, VersionMixin, Base):
    """The built-in engine or an external client. ``policy`` holds queue rules."""

    __tablename__ = "download_client"

    kind: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(100), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=1)
    settings: Mapped[dict[str, Any]] = mapped_column(JSON)
    media_kinds: Mapped[list[str]] = mapped_column(JSON, default=list)  # empty = all
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class Download(IdMixin, VersionMixin, Base):
    """The client-side job for a grab."""

    __tablename__ = "download"

    grab_id: Mapped[str] = mapped_column(ForeignKey("grab.id"))
    client_id: Mapped[str] = mapped_column(ForeignKey("download_client.id"))
    client_ref: Mapped[str] = mapped_column(String(64))  # info hash
    name: Mapped[str] = mapped_column(Text)
    media_kind: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(20), default="queued")
    queue_priority: Mapped[int] = mapped_column(Integer, default=0)  # higher runs first
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    save_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    seed_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)  # stalls so far
    last_progress_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    removed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # Import of a completed download: pending -> importing -> imported | needs_attention
    # (retried at import_retry_at) | skipped.
    import_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    import_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    import_attempts: Mapped[int] = mapped_column(Integer, default=0)
    import_retry_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    imported_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # Seeding goal met (policy seed_ratio / seed_minutes): the torrent was stopped.
    seeded_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'downloading', 'paused', 'completed', 'failed', 'removed')",
            name="status",
        ),
        UniqueConstraint("client_id", "client_ref", name="uq_download_client_id_client_ref"),
        Index("ix_download_status", "status"),
        Index("ix_download_grab_id", "grab_id"),
    )
