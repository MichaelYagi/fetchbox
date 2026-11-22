"""TheTVDB (v4): only used for what TMDB lacks, absolute episode numbers.

Anime is released by absolute number (``[Group] Show - 1071``); TMDB doesn't carry
those, TheTVDB does. With a TVDB key configured, a series refresh fills each episode's
absolute number from TVDB's default order, matched by season and episode number.
Optional: everything else works without it.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

BASE = "https://api4.thetvdb.com/v4"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
TOKEN_TTL = 25 * 24 * 3600  # tokens last a month
MAX_PAGES = 40  # 500 episodes a page: enough for any series


class TvdbError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class TvdbClient:
    def __init__(
        self, api_key: str, pin: str | None = None,
        *, transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:  # fmt: skip
        self.api_key, self.pin = api_key, pin
        self._http = httpx.AsyncClient(
            base_url=BASE, timeout=TIMEOUT, transport=transport,
            headers={"Accept": "application/json", "User-Agent": "Fetchbox"},
        )  # fmt: skip
        self._token: str | None = None
        self._token_at = 0.0

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _login(self) -> str:
        if self._token and time.monotonic() - self._token_at < TOKEN_TTL:
            return self._token
        body = {"apikey": self.api_key, **({"pin": self.pin} if self.pin else {})}
        try:
            r = await self._http.post("/login", json=body)
        except httpx.HTTPError as exc:
            raise TvdbError(f"TVDB unreachable: {type(exc).__name__}") from None
        if r.status_code in (400, 401, 403):
            raise TvdbError("TVDB refused the API key (or the PIN, for subscriber keys)")
        if r.status_code >= 400:
            raise TvdbError(f"TVDB returned HTTP {r.status_code}")
        try:
            self._token = str(r.json()["data"]["token"])
        except (ValueError, KeyError, TypeError):
            raise TvdbError("TVDB sent an unexpected login reply") from None
        self._token_at = time.monotonic()
        return self._token

    async def _get(self, path: str, **params: Any) -> dict[str, Any]:
        token = await self._login()
        try:
            r = await self._http.get(
                path, params=params, headers={"Authorization": f"Bearer {token}"}
            )
        except httpx.HTTPError as exc:
            raise TvdbError(f"TVDB unreachable: {type(exc).__name__}") from None
        if r.status_code == 401:  # token expired early: once more with a new one
            self._token = None
            token = await self._login()
            r = await self._http.get(
                path, params=params, headers={"Authorization": f"Bearer {token}"}
            )
        if r.status_code == 404:
            raise TvdbError("TVDB doesn't know this series")
        if r.status_code >= 400:
            raise TvdbError(f"TVDB returned HTTP {r.status_code}")
        data: dict[str, Any] = r.json()
        return data

    async def test(self) -> str:
        await self._login()
        return "TVDB key works"

    async def absolute_numbers(self, series_id: int) -> dict[tuple[int, int], int]:
        """(season, episode) -> absolute number, from TVDB's default order."""
        out: dict[tuple[int, int], int] = {}
        for page in range(MAX_PAGES):
            doc = await self._get(f"/series/{series_id}/episodes/default", page=page)
            for e in (doc.get("data") or {}).get("episodes") or []:
                s, n, a = e.get("seasonNumber"), e.get("number"), e.get("absoluteNumber")
                if isinstance(s, int) and isinstance(n, int) and isinstance(a, int) and a > 0:
                    out[(s, n)] = a
            if not (doc.get("links") or {}).get("next"):
                break
        return out
