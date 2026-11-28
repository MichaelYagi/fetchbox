"""plex.tv: signing in with Plex, and an account's watchlist.

Signing in is Plex's own PIN flow, so Fetchbox never sees a password: Fetchbox asks for a
PIN, the person approves it on plex.tv (``auth_url``), and the PIN then carries the
account's token. The watchlist is what the account bookmarked with "+ Watchlist" in any
Plex app, read from Plex's Discover service; each title's TMDB, IMDb and TVDB ids come
from its ``Guid`` list (read for each new title when the listing doesn't carry them).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlencode

import httpx

PLEX_TV = "https://plex.tv"
DISCOVER = "https://discover.provider.plex.tv"
APP_AUTH = "https://app.plex.tv/auth"
PRODUCT = "Fetchbox"
PAGE = 100
TIMEOUT = httpx.Timeout(20.0, connect=10.0)


class PlexTvError(Exception):
    def __init__(self, kind: Literal["auth", "unavailable", "invalid_response"], message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass(frozen=True)
class Pin:
    id: int
    code: str
    token: str | None  # set once the person approved it on plex.tv
    expires_in: int | None = None


@dataclass(frozen=True)
class PlexUser:
    id: str
    username: str


@dataclass(frozen=True)
class WatchItem:
    key: str  # Plex's ratingKey: the item's id on the watchlist
    kind: Literal["movie", "series"]
    title: str
    year: int | None
    guid: str
    tmdb_id: int | None = None
    imdb_id: str | None = None
    tvdb_id: int | None = None


def ids_of(guids: list[dict[str, Any]]) -> dict[str, Any]:
    """{"tmdb_id": 438631, "imdb_id": "tt1160419", "tvdb_id": ...} from Plex's Guid list
    (``[{"id": "tmdb://438631"}, {"id": "imdb://tt1160419"}]``)."""
    out: dict[str, Any] = {}
    for g in guids:
        scheme, _, value = str(g.get("id") or "").partition("://")
        if scheme == "tmdb" and value.isdigit():
            out["tmdb_id"] = int(value)
        elif scheme == "tvdb" and value.isdigit():
            out["tvdb_id"] = int(value)
        elif scheme == "imdb" and value.startswith("tt"):
            out["imdb_id"] = value
    return out


class PlexTv:
    def __init__(
        self, client_id: str, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.client_id = client_id
        self._http = httpx.AsyncClient(
            timeout=TIMEOUT,
            transport=transport,
            headers={
                "Accept": "application/json",
                "X-Plex-Product": PRODUCT,
                "X-Plex-Client-Identifier": client_id,
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, url: str, *, token: str | None = None, **params: Any) -> Any:
        headers = {"X-Plex-Token": token} if token else {}
        return await self._call("GET", url, headers=headers, params=params)

    async def _call(self, method: str, url: str, **kw: Any) -> Any:
        try:
            r = await self._http.request(method, url, **kw)
        except httpx.TimeoutException:
            raise PlexTvError("unavailable", "plex.tv timed out") from None
        except httpx.HTTPError as exc:
            raise PlexTvError("unavailable", f"plex.tv unreachable: {type(exc).__name__}") from None
        if r.status_code == 401:
            raise PlexTvError("auth", "Plex no longer accepts this sign-in: sign in again")
        if r.status_code >= 400:
            raise PlexTvError("unavailable", f"plex.tv returned HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError:
            raise PlexTvError("invalid_response", "plex.tv sent something unexpected") from None

    # ---- signing in --------------------------------------------------------------------------

    async def create_pin(self) -> Pin:
        d = await self._call("POST", f"{PLEX_TV}/api/v2/pins", params={"strong": "true"})
        return Pin(id=int(d["id"]), code=str(d["code"]), token=None, expires_in=d.get("expiresIn"))

    def auth_url(self, pin: Pin) -> str:
        """Where the person approves Fetchbox (Plex's sign-in page)."""
        q = urlencode({"clientID": self.client_id, "code": pin.code,
                       "context[device][product]": PRODUCT})  # fmt: skip
        return f"{APP_AUTH}#?{q}"

    async def check_pin(self, pin_id: int) -> Pin:
        d = await self._get(f"{PLEX_TV}/api/v2/pins/{pin_id}")
        return Pin(id=int(d["id"]), code=str(d["code"]), token=d.get("authToken") or None)

    async def user(self, token: str) -> PlexUser:
        d = await self._get(f"{PLEX_TV}/api/v2/user", token=token)
        name = d.get("username") or d.get("title") or d.get("email") or "Plex account"
        return PlexUser(id=str(d.get("uuid") or d.get("id")), username=str(name))

    # ---- the watchlist -----------------------------------------------------------------------

    async def watchlist(self, token: str) -> list[WatchItem]:
        """Every movie and show on the account's watchlist, newest first."""
        out: list[WatchItem] = []
        start = 0
        while True:
            d = await self._get(
                f"{DISCOVER}/library/sections/watchlist/all", token=token,
                includeGuids=1, sort="watchlistedAt:desc",
                **{"X-Plex-Container-Start": start, "X-Plex-Container-Size": PAGE},
            )  # fmt: skip
            mc = (d or {}).get("MediaContainer") or {}
            rows = mc.get("Metadata") or []
            for m in rows:
                kind = {"movie": "movie", "show": "series"}.get(str(m.get("type")))
                if kind is None or not m.get("ratingKey"):
                    continue  # (an episode, a person: not something to download)
                out.append(
                    WatchItem(
                        key=str(m["ratingKey"]),
                        kind=kind,  # type: ignore[arg-type]
                        title=str(m.get("title") or ""),
                        year=m.get("year"),
                        guid=str(m.get("guid") or ""),
                        **ids_of(m.get("Guid") or []),
                    )
                )
            start += len(rows)
            total = mc.get("totalSize")
            if not rows or (isinstance(total, int) and start >= total):
                return out

    async def ids(self, token: str, key: str) -> dict[str, Any]:
        """A watchlist title's TMDB/IMDb/TVDB ids, when the listing didn't carry them."""
        d = await self._get(f"{DISCOVER}/library/metadata/{key}", token=token)
        rows = ((d or {}).get("MediaContainer") or {}).get("Metadata") or []
        return ids_of(rows[0].get("Guid") or []) if rows else {}
