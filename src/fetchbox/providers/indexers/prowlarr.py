"""Prowlarr: give its address and API key once; every indexer it has is searched.

Prowlarr has no single feed for all its indexers, only one Torznab feed per indexer
(``<address>/<id>/api``). This provider reads the list from Prowlarr's own API
(``/api/v1/indexer``, refreshed every 10 minutes, so indexers added or disabled in
Prowlarr follow along), and searches the enabled ones together: torrent indexers through
their Torznab feed, Usenet ones through their Newznab feed (same address and API). One indexer
failing doesn't fail the search; only all of them failing does.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any, ClassVar
from urllib.parse import urlsplit

import httpx
import structlog
from pydantic import BaseModel, Field, SecretStr, field_validator

from fetchbox.core.providers import TestResult, registry
from fetchbox.core.secrets import strip_key
from fetchbox.providers.indexers import (
    IndexerError,
    IndexerProvider,
    ReleaseCandidate,
    SearchQuery,
    TorrentSource,
)
from fetchbox.providers.indexers.torznab import (
    NewznabProvider,
    TorznabProvider,
    TorznabSettings,
)

log = structlog.get_logger(__name__)

LIST_TTL = 600.0
# Each Prowlarr indexer gets this long: well inside the 30 s a whole source gets, so one
# slow tracker is left out on its own instead of taking every other indexer's results with
# it (seen: LimeTorrents timing out at 30 s while The Pirate Bay answered in 0.3 s).
CHILD_DEADLINE = 20.0
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
# A pasted indexer feed (".../3" or ".../3/api") still means "this Prowlarr".
_FEED_SUFFIX = re.compile(r"/\d+(/api)?/?$")


class ProwlarrSettings(BaseModel):
    url: str = Field(description="Prowlarr's address, e.g. http://192.168.1.10:9696")
    api_key: SecretStr = Field(description="Prowlarr: Settings > General > Security > API Key")
    movie_categories: list[int] = Field(default=[2000], description="Newznab categories")
    tv_categories: list[int] = Field(default=[5000])
    music_categories: list[int] = Field(default=[3000])

    @field_validator("api_key", mode="before")
    @classmethod
    def _strip_key(cls, v: object) -> object:
        return strip_key(v)  # a pasted key often carries a trailing space or newline

    @field_validator("url")
    @classmethod
    def _base(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        parts = urlsplit(v)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("must be an http(s) URL")
        return _FEED_SUFFIX.sub("", v)


@dataclass
class _Child:
    id: int
    name: str
    provider: TorznabProvider
    protocol: str = "torrent"


class ProwlarrProvider(IndexerProvider):
    kind: ClassVar[str] = "prowlarr"
    Settings: ClassVar[type[BaseModel]] = ProwlarrSettings
    capabilities: ClassVar[frozenset[str]] = frozenset(
        {"search.imdb", "search.tmdb", "search.tvdb", "search.season", "rss"}
    )

    def __init__(
        self, settings: BaseModel, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        super().__init__(settings)
        assert isinstance(settings, ProwlarrSettings)
        self.cfg = settings
        self._transport = transport
        self._http = httpx.AsyncClient(
            timeout=TIMEOUT,
            transport=transport,
            headers={
                "User-Agent": "Fetchbox",
                "Accept": "application/json",
                "X-Api-Key": settings.api_key.get_secret_value(),
            },
        )
        self._children: dict[int, _Child] = {}
        # None until listed: the monotonic clock counts from boot, so a 0 here would read
        # as "listed just now" for the first 10 minutes after the machine starts
        self._listed_at: float | None = None
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        for c in self._children.values():
            await c.provider.aclose()
        self._children.clear()
        await self._http.aclose()

    async def _list(self) -> list[dict[str, Any]]:
        try:
            r = await self._http.get(f"{self.cfg.url}/api/v1/indexer")
        except httpx.TimeoutException:
            raise IndexerError("unavailable", "Prowlarr timed out") from None
        except httpx.HTTPError as exc:
            raise IndexerError(
                "unavailable", f"Prowlarr unreachable at {self.cfg.url}: {type(exc).__name__}"
            ) from None
        if r.status_code in (401, 403):
            raise IndexerError(
                "auth",
                "Prowlarr refused the API key: copy it from Prowlarr's Settings > General > "
                "Security > API Key",
            )
        if r.status_code == 404 or "json" not in r.headers.get("content-type", ""):
            raise IndexerError(
                "config",
                f"no Prowlarr at {self.cfg.url} (HTTP {r.status_code}): use the address you "
                "open Prowlarr at, e.g. http://192.168.1.10:9696",
            )
        if r.status_code >= 400:
            raise IndexerError("unavailable", f"Prowlarr returned HTTP {r.status_code}")
        rows = r.json()
        if not isinstance(rows, list):
            raise IndexerError("invalid_response", "Prowlarr's indexer list is not a list")
        return [x for x in rows if isinstance(x, dict)]

    async def indexers(self, *, fresh: bool = False) -> list[_Child]:
        """Prowlarr's enabled indexers that can search, torrent and Usenet."""
        async with self._lock:
            if (
                not fresh
                and self._listed_at is not None
                and self._listed_at > time.monotonic() - LIST_TTL
            ):
                return list(self._children.values())
            rows = await self._list()
            wanted = {
                int(x["id"]): (str(x.get("name") or f"indexer {x['id']}"), str(x["protocol"]))
                for x in rows
                if x.get("enable")
                and x.get("protocol") in ("torrent", "usenet")
                and x.get("supportsSearch", True)
                and isinstance(x.get("id"), int)
            }
            for gone in set(self._children) - set(wanted):
                await self._children.pop(gone).provider.aclose()
            for iid, (name, protocol) in wanted.items():
                if iid in self._children and self._children[iid].protocol == protocol:
                    self._children[iid].name = name
                    continue
                if iid in self._children:  # (its protocol changed: a new feed)
                    await self._children.pop(iid).provider.aclose()
                feed = NewznabProvider if protocol == "usenet" else TorznabProvider
                child = feed(
                    TorznabSettings(
                        url=f"{self.cfg.url}/{iid}",
                        api_key=self.cfg.api_key,
                        movie_categories=self.cfg.movie_categories,
                        tv_categories=self.cfg.tv_categories,
                        music_categories=self.cfg.music_categories,
                    ),
                    transport=self._transport,
                )
                self._children[iid] = _Child(iid, name, child, protocol)
            self._listed_at = time.monotonic()
            return list(self._children.values())

    async def _all(self, call: Any) -> list[ReleaseCandidate]:
        children = await self.indexers()
        if not children:
            raise IndexerError("config", "Prowlarr has no enabled indexers: add some in Prowlarr")

        async def one(c: _Child) -> list[ReleaseCandidate]:
            try:
                found: list[ReleaseCandidate] = await asyncio.wait_for(
                    call(c.provider), CHILD_DEADLINE
                )
            except TimeoutError:
                raise IndexerError("unavailable", f"no answer in {CHILD_DEADLINE:g}s") from None
            return found

        results = await asyncio.gather(*(one(c) for c in children), return_exceptions=True)
        found: list[ReleaseCandidate] = []
        errors: list[str] = []
        for c, res in zip(children, results, strict=True):
            if isinstance(res, BaseException):
                if not isinstance(res, IndexerError):
                    raise res
                errors.append(f"{c.name}: {res.message}")
            else:
                found += res
        if errors and len(errors) == len(children):
            raise IndexerError("unavailable", "every Prowlarr indexer failed: " + "; ".join(errors))
        if errors:  # the others' results still count
            log.warning(
                "prowlarr.indexers_failed", failed=errors, answered=len(children) - len(errors)
            )
        return found

    # ---- Provider / IndexerProvider ---------------------------------------------------------

    async def test(self) -> TestResult:
        try:
            children = await self.indexers(fresh=True)
        except IndexerError as exc:
            return TestResult(ok=False, message=exc.message, details={"kind": exc.kind})
        if not children:
            return TestResult(
                ok=False,
                message="Prowlarr has no enabled indexers: add some in Prowlarr",
                details={"kind": "config"},
            )
        names = sorted(c.name for c in children)
        return TestResult(
            ok=True,
            message=f"Prowlarr works: {len(names)} indexer(s): {', '.join(names)}",
            details={"indexers": names},
        )

    async def search(self, q: SearchQuery) -> list[ReleaseCandidate]:
        return await self._all(lambda p: p.search(q))

    async def rss(self) -> list[ReleaseCandidate]:
        return await self._all(lambda p: p.rss())

    async def resolve(self, c: ReleaseCandidate) -> TorrentSource:
        """Prowlarr's download links (``/<id>/download?...``) work through any of its
        feeds: they redirect to a magnet, or serve the .torrent or the NZB."""
        children = await self.indexers()
        if not children:
            raise IndexerError("config", "Prowlarr has no enabled indexers")
        return await children[0].provider.resolve(c)


registry.register(ProwlarrProvider)
