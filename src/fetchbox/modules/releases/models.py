"""Grabs (a decision to download a release for wanted items) and the blocklist."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from fetchbox.core.db import Base, IdMixin, UTCDateTime, utcnow

REASONS = ("manual", "rss", "search", "upgrade")


class Grab(IdMixin, Base):
    __tablename__ = "grab"

    release: Mapped[dict[str, Any]] = mapped_column(JSON)  # snapshot of what was chosen
    indexer_id: Mapped[str | None] = mapped_column(
        ForeignKey("indexer.id", ondelete="SET NULL"), nullable=True
    )
    info_hash: Mapped[str | None] = mapped_column(String(40), nullable=True)
    release_title: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(String(10))
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actor: Mapped[str] = mapped_column(String(100))
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True, unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        CheckConstraint("reason IN ('manual', 'rss', 'search', 'upgrade')", name="reason"),
        Index("ix_grab_info_hash", "info_hash"),
    )


class GrabItem(Base):
    __tablename__ = "grab_item"

    grab_id: Mapped[str] = mapped_column(
        ForeignKey("grab.id", ondelete="CASCADE"), primary_key=True
    )
    wanted_item_id: Mapped[str] = mapped_column(
        ForeignKey("wanted_item.id", ondelete="CASCADE"), primary_key=True
    )

    __table_args__ = (Index("ix_grab_item_wanted_item_id", "wanted_item_id"),)


class BlocklistEntry(IdMixin, Base):
    __tablename__ = "blocklist"

    title_id: Mapped[str | None] = mapped_column(
        ForeignKey("title.id", ondelete="CASCADE"), nullable=True
    )
    info_hash: Mapped[str | None] = mapped_column(String(40), nullable=True)
    guid: Mapped[str | None] = mapped_column(Text, nullable=True)
    release_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    indexer_id: Mapped[str | None] = mapped_column(
        ForeignKey("indexer.id", ondelete="SET NULL"), nullable=True
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        CheckConstraint(
            "info_hash IS NOT NULL OR guid IS NOT NULL OR release_title IS NOT NULL",
            name="identifies",
        ),
        Index("ix_blocklist_title_id", "title_id"),
        Index("ix_blocklist_info_hash", "info_hash"),
    )
