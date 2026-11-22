"""Indexer providers: search for torrents (Torznab first, YAML definitions later)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict

from fetchbox.core.providers import Provider, ProviderCategory


class IndexerError(Exception):
    """A request to an indexer failed. ``kind`` decides backoff and how it's reported."""

    def __init__(
        self,
        kind: Literal["auth", "rate_limited", "unavailable", "invalid_response", "config"],
        message: str,
        *,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retry_after = retry_after


@dataclass(frozen=True)
class SearchQuery:
    """What an indexer is asked for; indexers ignore the fields they can't use."""

    kind: Literal["movie", "series", "music"]
    terms: tuple[str, ...]  # music: "artist album"
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    absolute: int | None = None
    air_date: date | None = None
    ids: dict[str, str] = field(default_factory=dict)  # tmdb / imdb / tvdb


class ReleaseCandidate(BaseModel):
    """One search result as an indexer reported it."""

    model_config = ConfigDict(frozen=True)

    title: str
    source_id: str = ""  # filled in by the sources module
    source_name: str = ""
    priority: int = 25  # indexer priority; lower wins
    guid: str | None = None
    info_hash: str | None = None
    download_url: str | None = None  # .torrent (may redirect to a magnet)
    magnet_url: str | None = None
    info_url: str | None = None  # the indexer's details page
    size: int | None = None  # bytes
    seeders: int | None = None
    leechers: int | None = None
    published_at: datetime | None = None
    categories: tuple[int, ...] = ()
    indexer_flags: frozenset[str] = frozenset()


@dataclass(frozen=True)
class TorrentSource:
    """What a download client is given: a magnet link or .torrent bytes, or a direct
    HTTP(S) file (``url``, fetched by the built-in ``direct`` client into ``name``/)."""

    magnet: str | None = None
    torrent: bytes | None = None
    url: str | None = None
    name: str | None = None  # direct downloads: folder name
    size: int | None = None  # direct downloads: expected bytes, if known


class IndexerProvider(Provider):
    category: ClassVar[ProviderCategory] = "indexer"

    async def search(self, q: SearchQuery) -> list[ReleaseCandidate]:
        raise NotImplementedError

    async def rss(self) -> list[ReleaseCandidate]:
        raise NotImplementedError

    async def resolve(self, c: ReleaseCandidate) -> TorrentSource:
        raise NotImplementedError

    async def aclose(self) -> None:
        """Release connections. Safe to call more than once."""
