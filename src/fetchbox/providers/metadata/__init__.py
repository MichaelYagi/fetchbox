"""Metadata providers: look up movies and series (TMDB by default)."""

from __future__ import annotations

from datetime import date, datetime
from typing import ClassVar, Literal

from pydantic import BaseModel

from fetchbox.core.providers import Provider, ProviderCategory

MediaKind = Literal["movie", "series"]
ExternalSource = Literal["imdb", "tvdb"]


class MetadataError(Exception):
    """A lookup failed. ``kind`` tells callers how to report it."""

    def __init__(
        self, kind: Literal["auth", "not_found", "rate_limited", "unavailable"], message: str
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


class TitleSummary(BaseModel):
    kind: MediaKind
    tmdb_id: int
    name: str
    original_name: str | None = None
    original_language: str | None = None  # ISO 639-1
    year: int | None = None
    overview: str = ""
    poster_url: str | None = None


class MovieDetail(TitleSummary):
    kind: MediaKind = "movie"
    imdb_id: str | None = None
    runtime: int | None = None  # minutes
    status: str | None = None  # Rumored, Planned, In Production, Post Production, Released
    release_date: date | None = None
    # Earliest per kind across countries (TMDB release types 3, 4 and 5).
    theatrical_date: date | None = None
    digital_date: date | None = None
    physical_date: date | None = None
    genres: list[str] = []
    alt_titles: list[str] = []


class EpisodeDetail(BaseModel):
    season: int
    number: int
    name: str | None = None
    air_date: date | None = None
    runtime: int | None = None


class SeasonDetail(BaseModel):
    number: int
    name: str | None = None
    air_date: date | None = None
    episode_count: int = 0
    episodes: list[EpisodeDetail] = []


class SeriesDetail(TitleSummary):
    kind: MediaKind = "series"
    imdb_id: str | None = None
    tvdb_id: int | None = None
    status: str | None = None  # Returning Series, Ended, Canceled, In Production, Planned
    ended: bool = False
    runtime: int | None = None  # typical episode runtime, minutes
    genres: list[str] = []
    alt_titles: list[str] = []
    seasons: list[SeasonDetail] = []


class MetadataProvider(Provider):
    category: ClassVar[ProviderCategory] = "metadata"

    async def search(
        self, query: str, kind: MediaKind, year: int | None = None
    ) -> list[TitleSummary]:
        raise NotImplementedError

    async def get_movie(self, ext_id: str) -> MovieDetail:
        raise NotImplementedError

    async def get_series(self, ext_id: str) -> SeriesDetail:
        raise NotImplementedError

    async def get_changes(self, kind: MediaKind, since: datetime) -> list[str]:
        raise NotImplementedError

    async def find_by_external_id(self, source: ExternalSource, value: str) -> TitleSummary | None:
        raise NotImplementedError

    async def aclose(self) -> None:
        """Release connections. Safe to call more than once."""
