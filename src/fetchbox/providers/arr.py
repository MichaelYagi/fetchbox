"""Read-only client for Radarr and Sonarr (their v3 API), for migrating to Fetchbox.

Only reads: titles, files, root folders, quality profiles, indexers and download
clients. Checked against Radarr (2026) and Sonarr 4.0.20: stored secrets (indexer API
keys, client passwords) come back masked as ``********``, so they can't be migrated.
"""

from __future__ import annotations

from typing import Any, Literal

import httpx

Kind = Literal["radarr", "sonarr"]
TIMEOUT = httpx.Timeout(60.0, connect=10.0)


class ArrError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ArrClient:
    def __init__(
        self, kind: Kind, url: str, api_key: str,
        *, transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:  # fmt: skip
        self.kind = kind
        self.url = url.strip().rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self.url, timeout=TIMEOUT, transport=transport,
            headers={"X-Api-Key": api_key.strip(), "Accept": "application/json",
                     "User-Agent": "Fetchbox"},
        )  # fmt: skip

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, **params: Any) -> Any:
        try:
            r = await self._http.get(f"/api/v3{path}", params=params or None)
        except httpx.HTTPError as exc:
            raise ArrError(
                f"{self.kind.title()} unreachable at {self.url}: {type(exc).__name__}"
            ) from None
        if r.status_code == 401:
            raise ArrError(
                f"{self.kind.title()} refused the API key (Settings > General > API Key)"
            )
        if r.status_code >= 400 or "json" not in r.headers.get("content-type", ""):
            raise ArrError(f"no {self.kind.title()} API at {self.url} (HTTP {r.status_code})")
        return r.json()

    async def status(self) -> dict[str, Any]:
        s: dict[str, Any] = await self._get("/system/status")
        app = str(s.get("appName", "")).lower()
        if app and app != self.kind:
            raise ArrError(f"that's {s.get('appName')}, not {self.kind.title()}")
        return s

    async def titles(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = await self._get(
            "/movie" if self.kind == "radarr" else "/series"
        )
        return out

    async def episodes(self, series_id: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = await self._get("/episode", seriesId=series_id)
        return out

    async def episode_files(self, series_id: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = await self._get("/episodefile", seriesId=series_id)
        return out

    async def root_folders(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = await self._get("/rootfolder")
        return out

    async def quality_profiles(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = await self._get("/qualityprofile")
        return out

    async def indexers(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = await self._get("/indexer")
        return out

    async def download_clients(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = await self._get("/downloadclient")
        return out
