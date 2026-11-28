"""Trakt: a user's public watchlist (movies and shows), with each title's TMDB id.

Trakt's API needs a Client ID: the one of an API app anyone can make for free at
trakt.tv/oauth/applications. With it, any public profile's watchlist can be read (Trakt
profiles are public unless made private); a private one needs signing in, not done here.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from fetchbox.providers.media_servers.plex_tv import WatchItem

API = "https://api.trakt.tv"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)


class TraktError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind  # auth, not_found, private, unavailable, invalid_response
        self.message = message


class Trakt:
    def __init__(self, client_id: str, *, transport: httpx.AsyncBaseTransport | None = None):
        self._http = httpx.AsyncClient(
            base_url=API,
            timeout=TIMEOUT,
            transport=transport,
            headers={
                "Content-Type": "application/json",
                "trakt-api-version": "2",
                "trakt-api-key": client_id,
                "User-Agent": "Fetchbox",
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, user: str) -> Any:
        try:
            r = await self._http.get(path)
        except httpx.TimeoutException:
            raise TraktError("unavailable", "Trakt timed out") from None
        except httpx.HTTPError as exc:
            raise TraktError("unavailable", f"Trakt unreachable: {type(exc).__name__}") from None
        if r.status_code == 403:
            raise TraktError(
                "auth",
                "Trakt refused the Client ID: copy it from your API app at "
                "trakt.tv/oauth/applications",
            )
        if r.status_code == 401:
            raise TraktError(
                "private", f"{user}'s Trakt profile is private: make it public to follow it"
            )
        if r.status_code == 404:
            raise TraktError("not_found", f"there's no Trakt user {user!r}")
        if r.status_code >= 400:
            raise TraktError("unavailable", f"Trakt returned HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError:
            raise TraktError("invalid_response", "Trakt sent something unexpected") from None

    async def watchlist(self, username: str) -> list[WatchItem]:
        """The user's watchlisted movies and shows, newest first."""
        user = quote(username.strip(), safe="")
        out: list[WatchItem] = []
        for kind, key in (("movies", "movie"), ("shows", "show")):
            rows = await self._get(f"/users/{user}/watchlist/{kind}", username)
            if not isinstance(rows, list):
                raise TraktError("invalid_response", "Trakt's watchlist is not a list")
            for row in rows:
                m = row.get(key) if isinstance(row, dict) else None
                if not isinstance(m, dict):
                    continue
                ids = m.get("ids") or {}
                trakt_id = ids.get("trakt")
                if trakt_id is None:
                    continue
                out.append(
                    WatchItem(
                        key=f"{key}:{trakt_id}",
                        kind="movie" if key == "movie" else "series",
                        title=str(m.get("title") or ""),
                        year=m.get("year"),
                        guid=f"trakt://{key}/{trakt_id}",
                        tmdb_id=ids.get("tmdb"),
                        imdb_id=ids.get("imdb"),
                        tvdb_id=ids.get("tvdb"),
                    )
                )
        return out
