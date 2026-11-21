"""Torznab indexer: any Jackett, Prowlarr or native Torznab endpoint.

Searches by id when the indexer's caps allow it (IMDb/TMDB for movies, TVDB for TV),
falling back to a text search when the id search finds nothing. XML is parsed with the
stdlib (expat resolves no external entities); documents declaring entities are refused.
"""

from __future__ import annotations

import base64
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any, ClassVar
from urllib.parse import urljoin, urlsplit

import httpx
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

TORZNAB_NS = "{http://torznab.com/schemas/2015/feed}"
# No wildcard, so an app's own JSON API isn't mistaken for a feed.
ACCEPT = "application/rss+xml, application/xml, text/xml"
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
MAX_BODY = 20 * 1024 * 1024
MAX_TORRENT = 10 * 1024 * 1024
CAPS_TTL = 3600.0
_HASH = re.compile(r"^[0-9a-fA-F]{40}$")
_BTIH = re.compile(r"urn:btih:([0-9a-zA-Z]{32,40})", re.I)
# Newznab error codes: 100-103 account problems, 500/501 limits.
NOT_A_FEED = (
    "this address is an app's own API, not a Torznab feed. For Prowlarr, use one "
    "indexer's Torznab URL, e.g. http://localhost:9696/3 (Indexers > the indexer > Copy "
    "Torznab URL); Prowlarr has no feed for all indexers at once"
)
_AUTH_CODES = {"100", "101", "102", "103"}
_LIMIT_CODES = {"500", "501"}


class TorznabSettings(BaseModel):
    @field_validator("api_key", mode="before")
    @classmethod
    def _strip_key(cls, v: object) -> object:
        return strip_key(v)  # a pasted key often carries a trailing space or newline

    url: str = Field(
        description="Base URL, e.g. http://localhost:9696/1 (Prowlarr) or "
        "http://localhost:9117/api/v2.0/indexers/all/results/torznab (Jackett)"
    )
    api_path: str = Field(default="/api", description="Appended to the URL")
    api_key: SecretStr | None = None
    movie_categories: list[int] = Field(default=[2000], description="Newznab categories")
    tv_categories: list[int] = Field(default=[5000])
    limit: int = Field(default=100, ge=1, le=1000, description="Results per search")

    @field_validator("url")
    @classmethod
    def _http(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        parts = urlsplit(v)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("must be an http(s) URL")
        # Prowlarr's "Copy Torznab URL" ends in /api, which api_path adds again.
        return v.removesuffix("/api") if parts.path.endswith("/api") else v


@dataclass
class Caps:
    search: set[str] = field(default_factory=set)  # supported params of t=search
    movie: set[str] | None = None  # None = t=movie unavailable
    tv: set[str] | None = None
    categories: dict[int, str] = field(default_factory=dict)
    max_limit: int | None = None


def _params(el: ET.Element | None) -> set[str] | None:
    if el is None or el.get("available", "yes").lower() != "yes":
        return None
    return {p.strip().lower() for p in (el.get("supportedParams") or "q").split(",") if p.strip()}


def parse_caps(root: ET.Element) -> Caps:
    searching = root.find("searching")
    caps = Caps()
    if searching is not None:
        caps.search = _params(searching.find("search")) or {"q"}
        caps.movie = _params(searching.find("movie-search"))
        caps.tv = _params(searching.find("tv-search"))
    for cat in root.iter("category"):
        caps.categories[int(cat.get("id", "0"))] = cat.get("name", "")
        for sub in cat.iter("subcat"):
            caps.categories[int(sub.get("id", "0"))] = sub.get("name", "")
    limits = root.find("limits")
    if limits is not None and (mx := limits.get("max")) and mx.isdigit():
        caps.max_limit = int(mx)
    return caps


def _int(v: str | None) -> int | None:
    try:
        return int(float(v)) if v not in (None, "") else None
    except ValueError:
        return None


def _float(v: str | None) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def _date(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        return parsedate_to_datetime(v)
    except (TypeError, ValueError):
        return None


def info_hash_from_magnet(magnet: str | None) -> str | None:
    if not magnet:
        return None
    m = _BTIH.search(magnet)
    if not m:
        return None
    h = m.group(1)
    if len(h) == 32:  # base32 form
        try:
            return base64.b32decode(h.upper()).hex()
        except ValueError:
            return None
    return h.lower()


def parse_item(item: ET.Element) -> ReleaseCandidate | None:
    title = (item.findtext("title") or "").strip()
    if not title:
        return None
    attrs: dict[str, list[str]] = {}
    for a in item.iter(f"{TORZNAB_NS}attr"):
        if (name := a.get("name")) is not None:
            attrs.setdefault(name.lower(), []).append(a.get("value", ""))

    def attr(name: str) -> str | None:
        vals = attrs.get(name)
        return vals[0] if vals else None

    enclosure = item.find("enclosure")
    link = (item.findtext("link") or "").strip() or None
    enc_url = enclosure.get("url") if enclosure is not None else None
    download = enc_url or link
    magnet = attr("magneturl")
    if download and download.startswith("magnet:"):
        magnet, download = magnet or download, None
    info_hash = attr("infohash")
    if not (info_hash and _HASH.match(info_hash)):
        info_hash = info_hash_from_magnet(magnet)
    size = _int(item.findtext("size")) or _int(attr("size"))
    if size is None and enclosure is not None:
        size = _int(enclosure.get("length"))
    seeders = _int(attr("seeders"))
    peers = _int(attr("peers"))
    leechers = _int(attr("leechers"))
    if leechers is None and peers is not None and seeders is not None:
        leechers = max(peers - seeders, 0)

    flags: set[str] = set()
    down = _float(attr("downloadvolumefactor"))
    if down == 0:
        flags.add("freeleech")
    elif down is not None and 0 < down < 1:
        flags.add("halfleech")
    up = _float(attr("uploadvolumefactor"))
    if up is not None and up >= 2:
        flags.add("doubleupload")
    for tag in attrs.get("tag", []):
        t = tag.strip().lower()
        if t in {"freeleech", "internal", "scene", "nuked", "halfleech", "doubleupload"}:
            flags.add(t)

    cats = [c for c in (_int(v) for v in attrs.get("category", [])) if c is not None]
    if not cats:
        cats = [c for c in (_int(el.text) for el in item.iter("category")) if c is not None]

    return ReleaseCandidate(
        title=title,
        guid=(item.findtext("guid") or "").strip() or None,
        info_hash=info_hash.lower() if info_hash else None,
        download_url=download,
        magnet_url=magnet,
        info_url=(item.findtext("comments") or "").strip() or None,
        size=size if size and size > 0 else None,
        seeders=seeders,
        leechers=leechers,
        published_at=_date(item.findtext("pubDate")),
        categories=tuple(dict.fromkeys(cats)),
        indexer_flags=frozenset(flags),
    )


def _snippet(body: bytes, content_type: str | None) -> str:
    """What came back, for the error message: type and the first bytes, keys redacted."""
    text = body[:160].decode("utf-8", "replace").strip()
    text = re.sub(r"(?i)(api_?key[\"'=:\s]+)[\w-]+", r"\1***", text)
    text = " ".join(text.split())
    return f"{content_type or 'no content type'}, starting {text!r}" if text else "an empty body"


def _parse_xml(body: bytes, content_type: str | None = None) -> ET.Element:
    head = body[:4096].lstrip()
    if head.startswith((b"<!DOCTYPE html", b"<!doctype html", b"<html")):
        raise IndexerError(
            "invalid_response",
            "the URL answered with a web page, not a Torznab feed: use the indexer's "
            "Torznab URL (Prowlarr: Indexers > the indexer > Copy Torznab URL)",
        )
    if b"<!ENTITY" in body[:4096] or b"<!DOCTYPE" in body[:4096]:
        raise IndexerError("invalid_response", "indexer returned XML with a DTD; refused")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise IndexerError(
            "invalid_response",
            f"indexer returned something other than Torznab XML ({exc}): "
            f"{_snippet(body, content_type)}",
        ) from None
    if root.tag == "error":
        code = root.get("code", "")
        desc = root.get("description") or "unknown error"
        if code in _AUTH_CODES:
            raise IndexerError("auth", f"indexer refused the API key: {desc}")
        if code in _LIMIT_CODES:
            raise IndexerError("rate_limited", f"indexer limit reached: {desc}")
        raise IndexerError("unavailable", f"indexer error {code}: {desc}")
    return root


@dataclass(frozen=True)
class _Response:
    status: int
    headers: httpx.Headers
    body: bytes


class TorznabProvider(IndexerProvider):
    kind: ClassVar[str] = "torznab"
    Settings: ClassVar[type[BaseModel]] = TorznabSettings
    capabilities: ClassVar[frozenset[str]] = frozenset(
        {"search.imdb", "search.tmdb", "search.tvdb", "search.season", "rss"}
    )

    def __init__(
        self, settings: BaseModel, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        super().__init__(settings)
        assert isinstance(settings, TorznabSettings)
        self.cfg = settings
        self._endpoint = settings.url + "/" + settings.api_path.strip("/")
        self._http = httpx.AsyncClient(
            timeout=TIMEOUT,
            transport=transport,
            # Caps come back as application/xml, results as application/rss+xml; Prowlarr
            # answers 406 to an Accept header that rules out what it sends.
            headers={"User-Agent": "Fetchbox", "Accept": ACCEPT},
        )
        self._caps: tuple[float, Caps] | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _fetch(
        self, url: str, params: dict[str, Any] | None, cap: int, *, follow: bool
    ) -> _Response:
        try:
            async with self._http.stream("GET", url, params=params, follow_redirects=follow) as r:
                body = bytearray()
                async for chunk in r.aiter_bytes():
                    body += chunk
                    if len(body) > cap:
                        raise IndexerError("invalid_response", "indexer response is too large")
                return _Response(r.status_code, r.headers, bytes(body))
        except httpx.TimeoutException:
            raise IndexerError("unavailable", "indexer timed out") from None
        except httpx.HTTPError as exc:
            raise IndexerError(
                "unavailable", f"indexer unreachable: {type(exc).__name__}"
            ) from None

    async def _api(self, **params: Any) -> ET.Element:
        query = {k: v for k, v in params.items() if v not in (None, "")}
        if self.cfg.api_key is not None:
            query["apikey"] = self.cfg.api_key.get_secret_value()
        r = await self._fetch(self._endpoint, query, MAX_BODY, follow=True)
        if r.status in (401, 403):
            hint = (
                "check the API key (Prowlarr: Settings > General; Jackett: top of its page) "
                "and that the URL is one indexer's Torznab feed without /api, e.g. "
                "http://localhost:9696/3 for Prowlarr"
            )
            if self.cfg.api_key is None:
                hint = "no API key is set; " + hint
            raise IndexerError("auth", f"indexer refused the request (HTTP {r.status}): {hint}")
        if r.status == 429:
            retry = _float(r.headers.get("Retry-After"))
            raise IndexerError("rate_limited", "indexer rate limit hit", retry_after=retry)
        ctype = (r.headers.get("content-type") or "").lower()
        if r.status == 406 or (r.status < 400 and "json" in ctype):
            # Seen with Prowlarr's main address: its /api is Prowlarr's own JSON API (406
            # to an XML Accept header), not a feed. Every indexer has its own.
            raise IndexerError("config", NOT_A_FEED)
        if r.status == 404:
            raise IndexerError(
                "config",
                "nothing at this URL (HTTP 404): for Prowlarr, check the indexer number in "
                "http://host:9696/<number> (Indexers > the indexer > Copy Torznab URL)",
            )
        if r.status >= 400:
            # Torznab servers often put an <error> document in a 4xx/5xx body.
            if r.body.lstrip().startswith(b"<"):
                _parse_xml(r.body, r.headers.get("content-type"))
            raise IndexerError("unavailable", f"indexer returned HTTP {r.status}")
        return _parse_xml(r.body, r.headers.get("content-type"))

    async def caps(self, *, fresh: bool = False) -> Caps:
        if not fresh and self._caps and self._caps[0] > time.monotonic():
            return self._caps[1]
        caps = parse_caps(await self._api(t="caps"))
        self._caps = (time.monotonic() + CAPS_TTL, caps)
        return caps

    async def _items(self, **params: Any) -> list[ReleaseCandidate]:
        root = await self._api(extended=1, **params)
        channel = root.find("channel")
        items = channel.iter("item") if channel is not None else root.iter("item")
        return [c for c in (parse_item(i) for i in items) if c is not None]

    def _limit(self, caps: Caps) -> int:
        return min(self.cfg.limit, caps.max_limit or self.cfg.limit)

    # ---- Provider ---------------------------------------------------------------------------

    async def test(self) -> TestResult:
        try:
            caps = await self.caps(fresh=True)
            await self._items(t="search", cat=self._cats("movie"), limit=1)
        except IndexerError as exc:
            return TestResult(ok=False, message=exc.message, details={"kind": exc.kind})
        movie = sorted(caps.movie or ())
        tv = sorted(caps.tv or ())
        return TestResult(
            ok=True,
            message="Torznab endpoint works",
            details={"movie_search": movie, "tv_search": tv, "categories": len(caps.categories)},
        )

    # ---- IndexerProvider --------------------------------------------------------------------

    def _cats(self, kind: str) -> str:
        cats = self.cfg.movie_categories if kind == "movie" else self.cfg.tv_categories
        return ",".join(str(c) for c in cats)

    async def search(self, q: SearchQuery) -> list[ReleaseCandidate]:
        caps = await self.caps()
        limit = self._limit(caps)
        text = q.terms[0] if q.terms else ""
        if q.kind == "movie":
            cat = self._cats("movie")
            supported = caps.movie
            if supported is not None:
                ids: dict[str, str] = {}
                if "imdbid" in supported and q.ids.get("imdb"):
                    ids["imdbid"] = q.ids["imdb"].removeprefix("tt")
                elif "tmdbid" in supported and q.ids.get("tmdb"):
                    ids["tmdbid"] = q.ids["tmdb"]
                if ids:
                    found = await self._items(t="movie", cat=cat, limit=limit, **ids)
                    if found:
                        return found
                term = f"{text} {q.year}" if q.year and "year" not in supported else text
                return await self._items(
                    t="movie",
                    cat=cat,
                    limit=limit,
                    q=term,
                    year=q.year if "year" in supported else None,
                )
            return await self._items(
                t="search", cat=cat, limit=limit, q=f"{text} {q.year or ''}".strip()
            )

        cat = self._cats("series")
        supported = caps.tv
        season: Any = q.season
        ep: Any = q.episode
        if q.air_date is not None:  # daily shows: season = year, ep = month/day
            season, ep = q.air_date.year, f"{q.air_date.month:02d}/{q.air_date.day:02d}"
        if supported is not None:
            extra = {"season": season, "ep": ep}
            if "tvdbid" in supported and q.ids.get("tvdb"):
                found = await self._items(
                    t="tvsearch", cat=cat, limit=limit, tvdbid=q.ids["tvdb"], **extra
                )
                if found:
                    return found
            return await self._items(t="tvsearch", cat=cat, limit=limit, q=text, **extra)
        suffix = ""
        if q.season is not None:
            suffix = f" S{q.season:02d}" + (f"E{q.episode:02d}" if q.episode is not None else "")
        return await self._items(t="search", cat=cat, limit=limit, q=text + suffix)

    async def rss(self) -> list[ReleaseCandidate]:
        caps = await self.caps()
        cats = dict.fromkeys([*self.cfg.movie_categories, *self.cfg.tv_categories])
        return await self._items(t="search", cat=",".join(map(str, cats)), limit=self._limit(caps))

    async def resolve(self, c: ReleaseCandidate) -> TorrentSource:
        if c.magnet_url:
            return TorrentSource(magnet=c.magnet_url)
        url = c.download_url
        for _ in range(5):
            if not url:
                break
            if url.startswith("magnet:"):
                return TorrentSource(magnet=url)
            r = await self._fetch(url, None, MAX_TORRENT, follow=False)
            if 300 <= r.status < 400:
                location = r.headers.get("location")
                url = urljoin(url, location) if location else None
                continue
            if r.status >= 400:
                raise IndexerError("unavailable", f"torrent download failed (HTTP {r.status})")
            if not r.body.startswith(b"d"):
                raise IndexerError("invalid_response", "download is not a .torrent file")
            return TorrentSource(torrent=r.body)
        raise IndexerError("invalid_response", "release has no usable download link")


registry.register(TorznabProvider)
