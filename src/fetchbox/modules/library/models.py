"""Library: files on disk, what they belong to, and how they are named."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from fetchbox.core.concurrency import VersionMixin
from fetchbox.core.db import Base, IdMixin, UTCDateTime, utcnow


class NamingTemplate(IdMixin, VersionMixin, Base):
    __tablename__ = "naming_template"

    name: Mapped[str] = mapped_column(String(100), unique=True)
    patterns: Mapped[dict[str, str]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class MediaFile(IdMixin, Base):
    """A file in a destination, linked to the wanted item(s) it satisfies."""

    __tablename__ = "media_file"

    storage_node_id: Mapped[str] = mapped_column(ForeignKey("storage_node.id"))
    destination_id: Mapped[str | None] = mapped_column(
        ForeignKey("destination.id", ondelete="SET NULL"), nullable=True
    )
    relative_path: Mapped[str] = mapped_column(Text)  # relative to the destination path
    size: Mapped[int] = mapped_column(BigInteger)
    mtime: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    quick_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    quality: Mapped[dict[str, Any]] = mapped_column(JSON)  # name, revision, source, resolution...
    custom_format_score: Mapped[int] = mapped_column(Integer, default=0)
    mediainfo: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    release_group: Mapped[str | None] = mapped_column(String(100), nullable=True)
    original_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    link_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)  # how it got here
    # Music: the track this file is (one file per track); null for video
    track_id: Mapped[str | None] = mapped_column(
        ForeignKey("track.id", ondelete="SET NULL"), nullable=True
    )
    imported_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "storage_node_id", "relative_path", name="uq_media_file_storage_node_id_relative_path"
        ),
        Index("ix_media_file_quick_hash", "size", "quick_hash"),
    )


class MediaFileItem(Base):
    __tablename__ = "media_file_item"

    media_file_id: Mapped[str] = mapped_column(
        ForeignKey("media_file.id", ondelete="CASCADE"), primary_key=True
    )
    wanted_item_id: Mapped[str] = mapped_column(
        ForeignKey("wanted_item.id", ondelete="CASCADE"), primary_key=True
    )

    __table_args__ = (Index("ix_media_file_item_wanted_item_id", "wanted_item_id"),)
