from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, Boolean, CheckConstraint, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from fetchbox.core.concurrency import VersionMixin
from fetchbox.core.db import Base, IdMixin, UTCDateTime, utcnow

LINK_MODES = ("hardlink_or_copy", "hardlink_only", "copy", "move", "symlink")


class StorageNode(IdMixin, VersionMixin, Base):
    """A place files live. Phase 1 has the host's own ``local`` node."""

    __tablename__ = "storage_node"

    kind: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(100), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="online")
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Destination(IdMixin, VersionMixin, Base):
    """A root folder on a node for movies or TV, with how files get there."""

    __tablename__ = "destination"

    node_id: Mapped[str] = mapped_column(ForeignKey("storage_node.id"))
    name: Mapped[str] = mapped_column(String(100), unique=True)
    path: Mapped[str] = mapped_column(Text)
    media_kind: Mapped[str] = mapped_column(String(10))
    naming_template_id: Mapped[str | None] = mapped_column(
        ForeignKey("naming_template.id", ondelete="SET NULL"), nullable=True
    )
    link_mode: Mapped[str] = mapped_column(String(20), default="hardlink_or_copy")
    free_space_min: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # bytes
    recycle: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="unknown")
    status_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # The mount (NAS share, other drive) the folder lives on, learned while it's mounted:
    # if it later isn't, the folder underneath is an empty local one, never written to.
    mount_point: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        CheckConstraint("media_kind IN ('movie', 'series', 'music')", name="media_kind"),
        CheckConstraint(
            "link_mode IN ('hardlink_or_copy', 'hardlink_only', 'copy', 'move', 'symlink')",
            name="link_mode",
        ),
    )
