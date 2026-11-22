"""MusicBrainz: artists, their albums (release groups), editions (releases) and tracks.
Cover art comes from the Cover Art Archive by release-group id.

MusicBrainz asks every client for at most one request a second, from all of an app's
requests together, and a User-Agent naming the app and how to reach its maintainers;
it answers 503 when a client goes faster. One lock spaces every request here, and a 503
is retried after a pause. No key is needed.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import httpx

import fetchbox

BASE = "https://musicbrainz.org/ws/2"
COVERS = "https://coverartarchive.org/release-group"
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
INTERVAL = 1.05  # seconds between requests (their limit: 1 a second)
RETRIES = 4


def user_agent() -> str:
    return f"Fetchbox/{fetchbox.__version__} ( https://github.com/MichaelYagi/fetchbox )"


def cover_url(release_group_id: str, size: int = 250) -> str:
    """The front cover (Cover Art Archive redirects to the image; 250, 500 or 1200)."""
    return f"{COVERS}/{release_group_id}/front-{size}"


class MusicBrainzError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _date(raw: str | None) -> date | None:
    """MusicBrainz dates are YYYY, YYYY-MM or YYYY-MM-DD."""
    if not raw:
        return None
    parts = ([*raw.split("-"), "1", "1"])[:3]
    try:
        return date(int(parts[0]), int(parts[1]) or 1, int(parts[2]) or 1)
    except ValueError:
        return None


@dataclass
class ArtistResult:
    id: str
    name: str
    sort_name: str
    type: str | None
    country: str | None
    disambiguation: str
    begin_year: int | None
    score: int = 100


@dataclass
class AlbumResult:
    """A release group: "the album", whatever its editions."""

    id: str
    title: str
    primary_type: str | None  # Album, Single, EP, Broadcast, Other
    secondary_types: list[str]  # Compilation, Live, Remix, Soundtrack, ...
    release_date: date | None
    disambiguation: str = ""

    @property
    def kind(self) -> str:
        """album / ep / single / live / compilation / other: what monitoring filters on."""
        sec = {s.lower() for s in self.secondary_types}
        if "live" in sec:
            return "live"
        if "compilation" in sec:
            return "compilation"
        if sec:
            return "other"  # remix, soundtrack, demo, interview, mixtape, ...
        return {"album": "album", "ep": "ep", "single": "single"}.get(
            (self.primary_type or "").lower(), "other"
        )


@dataclass
class TrackResult:
    disc: int
    number: int  # position on its disc
    title: str
    length_ms: int | None
    recording_id: str


@dataclass
class EditionResult:
    """A release: one edition of an album (country, date, format, track list)."""

    id: str
    title: str
    date: date | None
    country: str | None
    status: str | None
    formats: list[str]
    tracks: list[TrackResult] = field(default_factory=list)

    @property
    def track_count(self) -> int:
        return len(self.tracks)

    @property
    def length_ms(self) -> int:
        return sum(t.length_ms or 0 for t in self.tracks)

    @property
    def discs(self) -> int:
        return max((t.disc for t in self.tracks), default=1)


class MusicBrainzClient:
    _lock = asyncio.Lock()  # every client in the process: their limit is per app
    _last = 0.0

    def __init__(
        self, *, transport: httpx.AsyncBaseTransport | None = None, interval: float = INTERVAL
    ) -> None:
        self.interval = interval
        self._http = httpx.AsyncClient(
            base_url=BASE,
            timeout=TIMEOUT,
            transport=transport,
            follow_redirects=True,
            headers={"Accept": "application/json", "User-Agent": user_agent()},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, **params: Any) -> dict[str, Any]:
        params["fmt"] = "json"
        for attempt in range(RETRIES):
            async with MusicBrainzClient._lock:
                wait = MusicBrainzClient._last + self.interval - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    r = await self._http.get(path, params=params)
                except httpx.HTTPError as exc:
                    raise MusicBrainzError(
                        f"MusicBrainz unreachable: {type(exc).__name__}"
                    ) from None
                finally:
                    MusicBrainzClient._last = time.monotonic()
            if r.status_code == 503 and attempt < RETRIES - 1:  # too fast / busy: back off
                await asyncio.sleep(self.interval * (2**attempt))
                continue
            if r.status_code == 404:
                raise MusicBrainzError("MusicBrainz doesn't know this")
            if r.status_code >= 400:
                raise MusicBrainzError(f"MusicBrainz returned HTTP {r.status_code}")
            try:
                data: dict[str, Any] = r.json()
            except ValueError:
                raise MusicBrainzError("MusicBrainz sent an unexpected reply") from None
            return data
        raise MusicBrainzError("MusicBrainz is busy: try again in a minute")

    # ---- artists ------------------------------------------------------------------------------

    @staticmethod
    def _artist(a: dict[str, Any]) -> ArtistResult:
        begin = _date((a.get("life-span") or {}).get("begin"))
        return ArtistResult(
            id=a["id"],
            name=a.get("name") or "?",
            sort_name=a.get("sort-name") or a.get("name") or "?",
            type=a.get("type"),
            country=a.get("country"),
            disambiguation=a.get("disambiguation") or "",
            begin_year=begin.year if begin else None,
            score=int(a.get("score") or 100),
        )

    async def search_artists(self, query: str, limit: int = 10) -> list[ArtistResult]:
        d = await self._get("/artist", query=query, limit=limit)
        return [self._artist(a) for a in d.get("artists") or []]

    async def artist(self, artist_id: str) -> ArtistResult:
        return self._artist(await self._get(f"/artist/{artist_id}"))

    # ---- albums -------------------------------------------------------------------------------

    async def albums(self, artist_id: str) -> list[AlbumResult]:
        """Every release group of the artist (paged: 100 a request)."""
        out: list[AlbumResult] = []
        offset = 0
        while True:
            d = await self._get("/release-group", artist=artist_id, limit=100, offset=offset)
            groups = d.get("release-groups") or []
            for g in groups:
                out.append(
                    AlbumResult(
                        id=g["id"],
                        title=g.get("title") or "?",
                        primary_type=g.get("primary-type"),
                        secondary_types=list(g.get("secondary-types") or []),
                        release_date=_date(g.get("first-release-date")),
                        disambiguation=g.get("disambiguation") or "",
                    )
                )
            offset += len(groups)
            if not groups or offset >= int(d.get("release-group-count") or 0):
                return out

    async def editions(self, album_id: str) -> list[EditionResult]:
        """The album's official releases with their track lists."""
        out: list[EditionResult] = []
        offset = 0
        while True:
            d = await self._get(
                "/release",
                **{"release-group": album_id},
                inc="media recordings",
                status="official",
                limit=100,
                offset=offset,
            )
            releases = d.get("releases") or []
            for r in releases:
                tracks = [
                    TrackResult(
                        disc=int(m.get("position") or i),
                        number=int(t.get("position") or n),
                        title=t.get("title") or (t.get("recording") or {}).get("title") or "?",
                        length_ms=t.get("length") or (t.get("recording") or {}).get("length"),
                        recording_id=(t.get("recording") or {}).get("id") or "",
                    )
                    for i, m in enumerate(r.get("media") or [], 1)
                    for n, t in enumerate(m.get("tracks") or [], 1)
                ]
                out.append(
                    EditionResult(
                        id=r["id"],
                        title=r.get("title") or "?",
                        date=_date(r.get("date")),
                        country=r.get("country"),
                        status=r.get("status"),
                        formats=[m.get("format") or "?" for m in r.get("media") or []],
                        tracks=tracks,
                    )
                )
            offset += len(releases)
            if not releases or offset >= int(d.get("release-count") or 0):
                return out


def pick_edition(editions: list[EditionResult]) -> EditionResult | None:
    """The edition to want by default: the most common track count among digital/CD
    releases (the "standard" album), earliest first."""
    if not editions:
        return None
    digital = [e for e in editions if any(f in ("CD", "Digital Media") for f in e.formats)]
    pool = digital or editions
    counts: dict[int, int] = {}
    for e in pool:
        counts[e.track_count] = counts.get(e.track_count, 0) + 1
    common = max(counts, key=lambda c: (counts[c], -c))
    same = [e for e in pool if e.track_count == common]
    return min(same, key=lambda e: (e.date or date.max, e.id))
