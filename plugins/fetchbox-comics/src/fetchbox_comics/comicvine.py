"""ComicVine's API (comicvine.gamespot.com/api): series ("volumes") and their issues.

Plain plugin code, except for one piece of Fetchbox: `ctx.http`, the HTTP client every
plugin is given (Fetchbox's timeouts, retries for GETs, a User-Agent naming the plugin,
and per-host rate limits), passed in from Comics' methods.
"""

from __future__ import annotations

from datetime import date
from typing import Any

API = "https://comicvine.gamespot.com/api"
HOST = "comicvine.gamespot.com"
PAGE = 100
VOLUME = "4050"  # ComicVine's resource prefix for volumes: a series' ref is "4050-49901"


class ComicVineError(Exception):
    pass


def _date(v: Any) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def _year(v: Any) -> int | None:
    try:
        return int(str(v)[:4]) if v else None
    except ValueError:
        return None


class ComicVine:
    def __init__(self, ctx: Any, api_key: str) -> None:
        self.ctx = ctx
        self.api_key = api_key
        # Fetchbox: declared once, kept by every ctx.http request to the host: no sleeping
        # between requests here. ComicVine allows each app about one request a second.
        ctx.http.limit(HOST, per_second=1)

    async def _get(self, path: str, **params: Any) -> Any:
        """One request; ComicVine's own error codes become messages people can act on
        (Fetchbox shows them in the Test button's result and in Health)."""
        # Fetchbox: ctx.http.get retries a failed connection or a 429/5xx answer by itself
        r = await self.ctx.http.get(
            f"{API}{path}", params={"api_key": self.api_key, "format": "json", **params}
        )
        if r.status_code in (401, 403):
            raise ComicVineError("ComicVine refused the API key")
        r.raise_for_status()
        body = r.json()
        code = body.get("status_code")
        if code == 100:
            raise ComicVineError("ComicVine refused the API key")
        if code == 107:
            raise ComicVineError("ComicVine's rate limit: try again in an hour")
        if code != 1:
            raise ComicVineError(f"ComicVine: {body.get('error') or code}")
        return body

    async def check(self) -> str:
        """For the settings' Test button: the cheapest request that needs the key."""
        await self._get("/types/")
        return "The ComicVine key works"

    async def search(self, text: str) -> list[dict[str, Any]]:
        """Series ("volumes") by name, only the fields the Add page shows."""
        body = await self._get(
            "/search/", query=text, resources="volume", limit=20,
            field_list="id,name,start_year,image,deck,count_of_issues,publisher",
        )  # fmt: skip
        return [r for r in body.get("results") or [] if isinstance(r.get("id"), int)]

    async def volume(self, ref: str) -> dict[str, Any]:
        """One series by its ref ("4050-49901")."""
        body = await self._get(
            f"/volume/{ref}/", field_list="id,name,start_year,deck,image,publisher"
        )
        return dict(body.get("results") or {})

    async def issues(self, volume_id: int) -> list[dict[str, Any]]:
        """Every issue of a series, a page (100) at a time; a long run is several requests,
        kept to the rate limit by ctx.http."""
        out: list[dict[str, Any]] = []
        while True:
            body = await self._get(
                "/issues/", filter=f"volume:{volume_id}", limit=PAGE, offset=len(out),
                field_list="id,issue_number,name,cover_date,store_date",
            )  # fmt: skip
            page = body.get("results") or []
            out += page
            if not page or len(out) >= int(body.get("number_of_total_results") or 0):
                return out
