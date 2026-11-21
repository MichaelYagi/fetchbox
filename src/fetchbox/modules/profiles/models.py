from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from fetchbox.core.concurrency import VersionMixin
from fetchbox.core.db import Base, IdMixin, UTCDateTime, utcnow


class QualityProfile(IdMixin, VersionMixin, Base):
    __tablename__ = "quality_profile"

    name: Mapped[str] = mapped_column(String(100), unique=True)
    ladder: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    cutoff: Mapped[str] = mapped_column(String(100))
    upgrade_allowed: Mapped[bool] = mapped_column(Boolean, default=True)
    min_format_score: Mapped[int] = mapped_column(Integer, default=0)
    cutoff_format_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    upgrade_min_score_increment: Mapped[int] = mapped_column(Integer, default=1)
    format_scores: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    languages: Mapped[list[str]] = mapped_column(JSON, default=list)
    sizes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class CustomFormat(IdMixin, VersionMixin, Base):
    __tablename__ = "custom_format"

    name: Mapped[str] = mapped_column(String(100), unique=True)
    conditions: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    default_score: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
