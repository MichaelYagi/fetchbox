"""TMDB (themoviedb.org) metadata provider, API v3.

Accepts either a v3 API key (sent as the ``api_key`` query parameter) or a v4 read
access token (sent as ``Authorization: Bearer``). ``core.logging`` keeps the httpx
loggers at WARNING and redacts ``api_key=`` in any URL that is logged anyway.
"""

from __future__ import annotations

import asyncio
import re
from datetime import date, datetime
from typing import Any, ClassVar

import httpx
from pydantic import BaseModel, Field, SecretStr

from fetchbox.core.providers import TestResult, registry
from fetchbox.providers.metadata import (
    EpisodeDetail,
    ExternalSource,
    MediaKind,
    MetadataError,
    MetadataProvider,
    MovieDetail,
    SeasonDetail,
    SeriesDetail,
    TitleSummary,
)

API_URL = "https://api.themoviedb.org/3"
IMAGE_URL = "https://image.tmdb.org/t/p/w500"
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
MAX_RETRY_AFTER = 5.0
ENDED = {"Ended", "Canceled"}
_EXTERNAL_ID = {"imdb": re.compile(r"tt\d{1,10}"), "tvdb": re.compile(r"\d{1,10}")}


class TmdbSettings(BaseModel):
    api_key: SecretStr = Field(
        description="TMDB API key (v3) or API read access token (v4), from "
        "themoviedb.org/settings/api"
    )
    language: str = Field(
        default="en-US",
        pattern=r"^[a-z]{2}(-[A-Z]{2})?$",
        description="Language for titles and overviews",
    )
    include_adult: bool = False


def _date(v: Any) -> date | None:
    try:
        return date.fromisoformat(v) if v else None
    except (TypeError, ValueError):
        return None


def _poster(path: Any) -> str | None:
    return f"{IMAGE_URL}{path}" if isinstance(path, str) and path else None


def _release_dates(block: Any) -> dict[int, date]:
    """TMDB release type -> earliest date in any country (2 limited theatrical,
    3 theatrical, 4 digital, 5 physical)."""
    out: dict[int, date] = {}
    for country in (block or {}).get("results") or []:
        for r in country.get("release_dates") or []:
            kind, when = r.get("type"), _date((r.get("release_date") or "")[:10])
            if isinstance(kind, int) and when and (kind not in out or when < out[kind]):
                out[kind] = when
    return out


def _alt_titles(block: Any, *exclude: str | None) -> list[str]:
    """``alternative_titles`` is {"titles": [...]} for movies, {"results": [...]} for TV."""
    if not isinstance(block, dict):
        return []
    rows = block.get("titles") or block.get("results") or []
    seen = {e for e in exclude if e}
    out: list[str] = []
    for r in rows:
        t = r.get("title") if isinstance(r, dict) else None
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


class TmdbProvider(MetadataProvider):
    kind: ClassVar[str] = "tmdb"
    Settings: ClassVar[type[BaseModel]] = TmdbSettings
    capabilities: ClassVar[frozenset[str]] = frozenset(
        {"search.movie", "search.series", "find.imdb", "find.tvdb", "changes"}
    )

    def __init__(
        self,
        settings: BaseModel,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str = API_URL,
    ) -> None:
        super().__init__(settings)
        assert isinstance(settings, TmdbSettings)
        self.cfg = settings
        key = settings.api_key.get_secret_value().strip()
        headers = {"Accept": "application/json"}
        self._params: dict[str, str] = {"language": settings.language}
        if key.startswith("eyJ"):  # v4 token (a JWT)
            headers["Authorization"] = f"Bearer {key}"
        else:
            self._params["api_key"] = key
        self._http = httpx.AsyncClient(
            base_url=base_url, headers=headers, timeout=TIMEOUT, transport=transport
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, **params: Any) -> dict[str, Any]:
        query = {**self._params, **{k: v for k, v in params.items() if v is not None}}
        for attempt in range(2):
            try:
                r = await self._http.get(path, params=query)
            except httpx.HTTPError as exc:
                raise MetadataError(
                    "unavailable", f"TMDB unreachable: {type(exc).__name__}"
                ) from None
            if r.status_code == 429 and attempt == 0:
                try:
                    wait = float(r.headers.get("Retry-After", "1"))
                except ValueError:
                    wait = 1.0
                await asyncio.sleep(min(max(wait, 0.0), MAX_RETRY_AFTER))
                continue
            break
        if r.status_code == 401:
            raise MetadataError("auth", "TMDB rejected the API key")
        if r.status_code == 404:
            raise MetadataError("not_found", f"TMDB has no {path}")
        if r.status_code == 429:
            raise MetadataError("rate_limited", "TMDB rate limit hit; try again shortly")
        if r.status_code >= 400:
            raise MetadataError("unavailable", f"TMDB returned HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError:
            raise MetadataError("unavailable", "TMDB returned invalid JSON") from None
        if not isinstance(data, dict):
            raise MetadataError("unavailable", "TMDB returned an unexpected response")
        return data

    # ---- Provider ---------------------------------------------------------------------------

    async def test(self) -> TestResult:
        try:
            await self._get("/configuration")
        except MetadataError as exc:
            return TestResult(ok=False, message=exc.message, details={"kind": exc.kind})
        return TestResult(ok=True, message="TMDB key works")

    # ---- MetadataProvider -------------------------------------------------------------------

    def _summary(self, kind: MediaKind, r: dict[str, Any]) -> TitleSummary:
        movie = kind == "movie"
        released = _date(r.get("release_date" if movie else "first_air_date"))
        return TitleSummary(
            kind=kind,
            tmdb_id=int(r["id"]),
            name=r.get("title" if movie else "name") or "",
            original_name=r.get("original_title" if movie else "original_name"),
            original_language=r.get("original_language") or None,
            year=released.year if released else None,
            overview=r.get("overview") or "",
            poster_url=_poster(r.get("poster_path")),
        )

    async def search(
        self, query: str, kind: MediaKind, year: int | None = None
    ) -> list[TitleSummary]:
        if kind == "movie":
            data = await self._get(
                "/search/movie",
                query=query,
                year=year,
                include_adult=str(self.cfg.include_adult).lower(),
            )
        else:
            data = await self._get(
                "/search/tv",
                query=query,
                first_air_date_year=year,
                include_adult=str(self.cfg.include_adult).lower(),
            )
        return [self._summary(kind, r) for r in data.get("results", []) if r.get("id")]

    async def get_movie(self, ext_id: str) -> MovieDetail:
        r = await self._get(
            f"/movie/{int(ext_id)}", append_to_response="alternative_titles,release_dates"
        )
        dates = _release_dates(r.get("release_dates"))
        base = self._summary("movie", r)
        return MovieDetail(
            **base.model_dump(),
            imdb_id=r.get("imdb_id") or None,
            runtime=r.get("runtime") or None,
            status=r.get("status"),
            release_date=_date(r.get("release_date")),
            theatrical_date=dates.get(3) or dates.get(2),
            digital_date=dates.get(4),
            physical_date=dates.get(5),
            genres=[g["name"] for g in r.get("genres", []) if g.get("name")],
            alt_titles=_alt_titles(r.get("alternative_titles"), base.name, base.original_name),
        )

    async def get_series(self, ext_id: str) -> SeriesDetail:
        tv_id = int(ext_id)
        r = await self._get(f"/tv/{tv_id}", append_to_response="alternative_titles,external_ids")
        base = self._summary("series", r)
        ids = r.get("external_ids") or {}
        runtimes = [x for x in r.get("episode_run_time") or [] if isinstance(x, int) and x > 0]
        numbers = [
            s["season_number"]
            for s in r.get("seasons", [])
            if isinstance(s.get("season_number"), int)
        ]
        seasons = await asyncio.gather(*(self._season(tv_id, n) for n in numbers))
        return SeriesDetail(
            **base.model_dump(),
            imdb_id=ids.get("imdb_id") or None,
            tvdb_id=ids.get("tvdb_id") or None,
            status=r.get("status"),
            ended=r.get("status") in ENDED,
            runtime=min(runtimes) if runtimes else None,
            genres=[g["name"] for g in r.get("genres", []) if g.get("name")],
            alt_titles=_alt_titles(r.get("alternative_titles"), base.name, base.original_name),
            seasons=list(seasons),
        )

    async def _season(self, tv_id: int, number: int) -> SeasonDetail:
        s = await self._get(f"/tv/{tv_id}/season/{number}")
        episodes = [
            EpisodeDetail(
                season=number,
                number=e["episode_number"],
                name=e.get("name"),
                air_date=_date(e.get("air_date")),
                runtime=e.get("runtime") or None,
            )
            for e in s.get("episodes", [])
            if isinstance(e.get("episode_number"), int)
        ]
        return SeasonDetail(
            number=number,
            name=s.get("name"),
            air_date=_date(s.get("air_date")),
            episode_count=len(episodes),
            episodes=episodes,
        )

    async def get_changes(self, kind: MediaKind, since: datetime) -> list[str]:
        path = "/movie/changes" if kind == "movie" else "/tv/changes"
        out: list[str] = []
        page, pages = 1, 1
        while page <= pages and page <= 50:
            data = await self._get(path, start_date=since.date().isoformat(), page=page)
            out += [str(r["id"]) for r in data.get("results", []) if r.get("id")]
            pages = int(data.get("total_pages") or 1)
            page += 1
        return out

    async def find_by_external_id(self, source: ExternalSource, value: str) -> TitleSummary | None:
        if not _EXTERNAL_ID[source].fullmatch(value):
            raise MetadataError("not_found", f"{value!r} is not a valid {source} id")
        data = await self._get(f"/find/{value}", external_source=f"{source}_id")
        for kind, key in (("movie", "movie_results"), ("series", "tv_results")):
            rows = data.get(key) or []
            if rows:
                return self._summary(kind, rows[0])  # type: ignore[arg-type]
        return None


registry.register(TmdbProvider)
