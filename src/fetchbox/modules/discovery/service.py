"""Metadata lookup through the configured provider (TMDB), with a small TTL cache."""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any

import httpx
import structlog

from fetchbox.core.errors import ProblemError
from fetchbox.core.providers import HealthStatus
from fetchbox.modules.system.config import ConfigService, SystemConfig
from fetchbox.providers.metadata import (
    MediaKind,
    MetadataError,
    MetadataProvider,
    MovieDetail,
    SeriesDetail,
    TitleSummary,
)
from fetchbox.providers.metadata.tmdb import TmdbProvider, TmdbSettings
from fetchbox.providers.metadata.tvdb import TvdbClient, TvdbError

DETAIL_TTL = 3600.0
log = structlog.get_logger(__name__)
SEARCH_TTL = 300.0
CACHE_SIZE = 512
FIX_URL = "/settings/general#tmdb"


class _TTLCache:
    def __init__(self, size: int) -> None:
        self.size = size
        self._data: OrderedDict[Any, tuple[float, Any]] = OrderedDict()

    def get(self, key: Any) -> Any | None:
        hit = self._data.get(key)
        if hit is None or hit[0] < time.monotonic():
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return hit[1]

    def put(self, key: Any, value: Any, ttl: float) -> None:
        self._data[key] = (time.monotonic() + ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self.size:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()


def problem(exc: MetadataError) -> ProblemError:
    status, title = {
        "not_found": (404, "Not Found"),
        "rate_limited": (503, "Service Unavailable"),
        "auth": (502, "Bad Gateway"),
        "unavailable": (502, "Bad Gateway"),
    }[exc.kind]
    return ProblemError(status, title, exc.message, slug=f"metadata-{exc.kind.replace('_', '-')}")


NOT_CONFIGURED = (
    "No TMDB API key is set. Get a free key at themoviedb.org/settings/api and save it "
    'with PATCH /system/config {"tmdb": {"api_key": "..."}}'
)


class DiscoveryService:
    def __init__(self, config: ConfigService) -> None:
        self.config = config
        self.transport: httpx.AsyncBaseTransport | None = None  # tests inject a mock
        self._provider: MetadataProvider | None = None
        self._tvdb: TvdbClient | None = None
        self._cache = _TTLCache(CACHE_SIZE)
        config.on_change(self._reset)

    async def _reset(self, _cfg: SystemConfig) -> None:
        await self.aclose()
        self._cache.clear()  # language may have changed

    async def aclose(self) -> None:
        if self._provider is not None:
            await self._provider.aclose()
            self._provider = None
        if self._tvdb is not None:
            await self._tvdb.aclose()
            self._tvdb = None

    def tvdb(self) -> TvdbClient | None:
        """The TVDB client when a TVDB key is set (optional), else None."""
        cfg = self.config.current.tvdb
        if cfg.api_key is None:
            return None
        if self._tvdb is None:
            self._tvdb = TvdbClient(
                cfg.api_key.get_secret_value(),
                cfg.pin.get_secret_value() if cfg.pin else None,
                transport=self.transport,
            )
        return self._tvdb

    async def absolute_numbers(self, tvdb_id: int | None) -> dict[tuple[int, int], int]:
        """Absolute episode numbers from TVDB; empty without a key, an id, or on errors
        (logged: a refresh never fails over this)."""
        client = self.tvdb()
        if client is None or not tvdb_id:
            return {}
        key = ("absolute", tvdb_id)
        if (hit := self._cache.get(key)) is not None:
            return dict(hit)
        try:
            out = await client.absolute_numbers(tvdb_id)
        except TvdbError as exc:
            log.warning("tvdb.failed", tvdb_id=tvdb_id, error=exc.message)
            return {}
        self._cache.put(key, out, DETAIL_TTL)
        return out

    @property
    def configured(self) -> bool:
        return self.config.current.tmdb.api_key is not None

    def provider(self) -> MetadataProvider:
        if self._provider is None:
            tmdb = self.config.current.tmdb
            if tmdb.api_key is None:
                raise ProblemError(
                    503, "Metadata Not Configured", NOT_CONFIGURED, slug="metadata-not-configured"
                )
            settings = TmdbSettings(
                api_key=tmdb.api_key, language=tmdb.language, include_adult=tmdb.include_adult
            )
            self._provider = TmdbProvider(settings, transport=self.transport)
        return self._provider

    async def search(self, q: str, kind: MediaKind, year: int | None) -> list[TitleSummary]:
        key = ("search", kind, q.strip().lower(), year)
        if (hit := self._cache.get(key)) is not None:
            return list(hit)
        try:
            out = await self.provider().search(q, kind, year)
        except MetadataError as exc:
            raise problem(exc) from None
        self._cache.put(key, out, SEARCH_TTL)
        return out

    async def movie(self, tmdb_id: int, *, fresh: bool = False) -> MovieDetail:
        key = ("movie", tmdb_id)
        if not fresh and (hit := self._cache.get(key)) is not None:
            return hit  # type: ignore[no-any-return]
        try:
            out = await self.provider().get_movie(str(tmdb_id))
        except MetadataError as exc:
            raise problem(exc) from None
        self._cache.put(key, out, DETAIL_TTL)
        return out

    async def series(self, tmdb_id: int, *, fresh: bool = False) -> SeriesDetail:
        key = ("series", tmdb_id)
        if not fresh and (hit := self._cache.get(key)) is not None:
            return hit  # type: ignore[no-any-return]
        try:
            out = await self.provider().get_series(str(tmdb_id))
        except MetadataError as exc:
            raise problem(exc) from None
        self._cache.put(key, out, DETAIL_TTL)
        return out

    async def health(self) -> HealthStatus:
        if not self.configured:
            return HealthStatus(
                status="warning",
                message="No TMDB API key: searching for and adding titles is unavailable",
            )
        res = await self.provider().test()
        return HealthStatus(status="ok" if res.ok else "error", message=res.message)
