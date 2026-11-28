from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from fetchbox.core.db import Base, IdMixin, UTCDateTime, utcnow


class PlexAccount(IdMixin, Base):
    """A Plex account whose watchlist is followed, signed in with Plex (no password)."""

    __tablename__ = "plex_account"

    plex_id: Mapped[str] = mapped_column(String(64), unique=True)
    username: Mapped[str] = mapped_column(String(200))
    token: Mapped[str] = mapped_column(Text)  # sealed with the secrets key
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class TraktUser(IdMixin, Base):
    """A Trakt user whose (public) watchlist is followed."""

    __tablename__ = "trakt_user"

    username: Mapped[str] = mapped_column(String(100), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class WatchlistItem(IdMixin, Base):
    """A title seen on a watchlist (a Plex account's or a Trakt user's), and what became
    of it. Kept, so a title removed in Fetchbox isn't added again while it's still on the
    watchlist."""

    __tablename__ = "watchlist_item"

    account_id: Mapped[str | None] = mapped_column(
        ForeignKey("plex_account.id", ondelete="CASCADE"), nullable=True
    )
    trakt_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("trakt_user.id", ondelete="CASCADE"), nullable=True
    )
    plex_key: Mapped[str] = mapped_column(String(64))  # the item's id there (Plex or Trakt)
    kind: Mapped[str] = mapped_column(String(10))  # movie, series
    name: Mapped[str] = mapped_column(String(500))
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tmdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # added, waiting (for approval), in_library (already there), dismissed, skipped
    state: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    title_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("account_id", "plex_key", name="uq_watchlist_item_account_id_plex_key"),
        UniqueConstraint(
            "trakt_user_id", "plex_key", name="uq_watchlist_item_trakt_user_id_plex_key"
        ),
        Index("ix_watchlist_item_state", "state"),
    )
