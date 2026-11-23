"""Titles (movies, series, music artists), seasons, episodes, albums, tracks and wanted
items.

The wanted item is the unit Fetchbox acquires: one per movie, one per episode, one per
album. All three go through the same states, queue, import and history.
Seasons and episodes are created by the series pipeline (Phase 2); the tables exist
now so the schema matches the spec.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from fetchbox.core.concurrency import VersionMixin
from fetchbox.core.db import Base, IdMixin, UTCDateTime, utcnow

KINDS = ("movie", "series", "music")
ALBUM_KINDS = ("album", "ep", "single", "live", "compilation", "other")
STATES = ("missing", "searching", "grabbed", "downloading", "importing", "acquired")
LOCKS = ("acquired", "manual")


class Title(IdMixin, VersionMixin, Base):
    __tablename__ = "title"

    kind: Mapped[str] = mapped_column(String(10))
    tmdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tvdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    imdb_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # MusicBrainz artist id (music)
    mbid: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(500))
    sort_name: Mapped[str] = mapped_column(String(500))
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    profile_id: Mapped[str] = mapped_column(ForeignKey("quality_profile.id"))
    # TODO(phase 1, storage): FK to destination once that table exists.
    destination_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    # Its own folder inside the library folder ("The Bear", "Artist"): remembered when
    # the first file is filed or found there, then used whatever the naming says.
    # None: not decided yet (the naming gives it).
    folder: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    monitor_mode: Mapped[str] = mapped_column(String(20), default="all")
    # Movies: when automatic search starts (announced, in_cinemas, released).
    minimum_availability: Mapped[str] = mapped_column(String(20), default="released")
    # "metadata" is reserved by SQLAlchemy's declarative base, hence the attribute name.
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    added_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    refreshed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    __table_args__ = (
        CheckConstraint("kind IN ('movie', 'series', 'music')", name="kind"),
        UniqueConstraint("kind", "tmdb_id", name="uq_title_kind_tmdb_id"),
        Index("ix_title_sort_name", "sort_name"),
    )


class Season(IdMixin, Base):
    __tablename__ = "season"

    title_id: Mapped[str] = mapped_column(ForeignKey("title.id", ondelete="CASCADE"))
    number: Mapped[int] = mapped_column(Integer)
    monitored: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (UniqueConstraint("title_id", "number", name="uq_season_title_id_number"),)


class Episode(IdMixin, Base):
    __tablename__ = "episode"

    season_id: Mapped[str] = mapped_column(ForeignKey("season.id", ondelete="CASCADE"))
    number: Mapped[int] = mapped_column(Integer)
    absolute_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    air_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    name: Mapped[str | None] = mapped_column(String(500), nullable=True)

    __table_args__ = (UniqueConstraint("season_id", "number", name="uq_episode_season_id_number"),)


class Album(IdMixin, Base):
    """A release group of a music artist ("the album", whatever its editions)."""

    __tablename__ = "album"

    title_id: Mapped[str] = mapped_column(ForeignKey("title.id", ondelete="CASCADE"))
    mbid: Mapped[str] = mapped_column(String(36))  # MusicBrainz release group
    name: Mapped[str] = mapped_column(String(500))
    kind: Mapped[str] = mapped_column(String(15), default="album")
    secondary_types: Mapped[list[str]] = mapped_column(JSON, default=list)
    release_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # The edition (MusicBrainz release) whose track list is wanted; null until chosen
    edition_mbid: Mapped[str | None] = mapped_column(String(36), nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)

    __table_args__ = (
        UniqueConstraint("title_id", "mbid", name="uq_album_title_id_mbid"),
        CheckConstraint(
            "kind IN ('album', 'ep', 'single', 'live', 'compilation', 'other')", name="kind"
        ),
    )


class Track(IdMixin, Base):
    """A track of an album's chosen edition."""

    __tablename__ = "track"

    album_id: Mapped[str] = mapped_column(ForeignKey("album.id", ondelete="CASCADE"))
    disc: Mapped[int] = mapped_column(Integer, default=1)
    number: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(500))
    length_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recording_mbid: Mapped[str | None] = mapped_column(String(36), nullable=True)

    __table_args__ = (
        UniqueConstraint("album_id", "disc", "number", name="uq_track_album_id_disc_number"),
    )


class WantedItem(IdMixin, VersionMixin, Base):
    __tablename__ = "wanted_item"

    title_id: Mapped[str] = mapped_column(ForeignKey("title.id", ondelete="CASCADE"))
    episode_id: Mapped[str | None] = mapped_column(
        ForeignKey("episode.id", ondelete="CASCADE"), nullable=True
    )
    album_id: Mapped[str | None] = mapped_column(
        ForeignKey("album.id", ondelete="CASCADE"), nullable=True
    )
    state: Mapped[str] = mapped_column(String(20), default="missing")
    monitored: Mapped[bool] = mapped_column(Boolean, default=True)
    lock: Mapped[str | None] = mapped_column(String(10), nullable=True)
    cutoff_met: Mapped[bool] = mapped_column(Boolean, default=False)
    last_search_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    next_search_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        CheckConstraint(
            "state IN ('missing', 'searching', 'grabbed', 'downloading', 'importing', 'acquired')",
            name="state",
        ),
        CheckConstraint("lock IS NULL OR lock IN ('acquired', 'manual')", name="lock"),
        UniqueConstraint("title_id", "episode_id", name="uq_wanted_item_title_id_episode_id"),
        UniqueConstraint("title_id", "album_id", name="uq_wanted_item_title_id_album_id"),
        Index("ix_wanted_search", "monitored", "lock", "state", "next_search_at"),
        Index("ix_wanted_item_title_id", "title_id"),
    )


HISTORY_TYPES = ("grabbed", "imported", "failed", "deleted", "upgraded", "unlocked")


class HistoryEvent(IdMixin, Base):
    """Immutable record per wanted item (and its title, kept after the item is gone)."""

    __tablename__ = "history_event"

    wanted_item_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    title_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    type: Mapped[str] = mapped_column(String(20))
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        Index("ix_history_event_title_id", "title_id", "created_at"),
        Index("ix_history_event_wanted_item_id", "wanted_item_id"),
    )
