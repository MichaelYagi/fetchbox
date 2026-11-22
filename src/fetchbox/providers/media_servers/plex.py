"""Plex Media Server: token auth (X-Plex-Token), partial scans per folder."""

from __future__ import annotations

from typing import Any, ClassVar

import httpx
from pydantic import BaseModel, Field, SecretStr, field_validator

from fetchbox.core.providers import TestResult, registry
from fetchbox.core.secrets import strip_key
from fetchbox.providers.media_servers import (
    Library,
    MediaServerError,
    MediaServerProvider,
    check_url,
    covering,
)

TIMEOUT = httpx.Timeout(20.0, connect=10.0)
KINDS = {"movie": "movie", "show": "series", "artist": "music"}


class PlexSettings(BaseModel):
    url: str = Field(description="Plex's address, e.g. http://192.168.1.10:32400")
    token: SecretStr = Field(
        description="Your Plex token: in Plex Web, open any item > Get Info > View XML; "
        "the token is the X-Plex-Token at the end of the address"
    )

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        return check_url(v)

    @field_validator("token", mode="before")
    @classmethod
    def _strip(cls, v: object) -> object:
        return strip_key(v)


class PlexProvider(MediaServerProvider):
    kind: ClassVar[str] = "plex"
    Settings: ClassVar[type[BaseModel]] = PlexSettings

    def __init__(
        self, settings: BaseModel, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        super().__init__(settings)
        assert isinstance(settings, PlexSettings)
        self.cfg = settings
        self._http = httpx.AsyncClient(
            base_url=settings.url,
            timeout=TIMEOUT,
            transport=transport,
            headers={
                "Accept": "application/json",
                "X-Plex-Token": settings.token.get_secret_value(),
                "X-Plex-Client-Identifier": "fetchbox",
                "X-Plex-Product": "Fetchbox",
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, url: str, **params: Any) -> httpx.Response:
        try:
            r = await self._http.get(url, params=params or None)
        except httpx.HTTPError as exc:
            raise MediaServerError(
                "unavailable", f"Plex unreachable at {self.cfg.url}: {type(exc).__name__}"
            ) from None
        if r.status_code in (401, 403):
            raise MediaServerError("auth", "Plex refused the token")
        if r.status_code >= 400:
            raise MediaServerError("unavailable", f"Plex returned HTTP {r.status_code}")
        return r

    async def libraries(self) -> list[Library]:
        r = await self._get("/library/sections")
        try:
            dirs = r.json()["MediaContainer"].get("Directory") or []
        except (ValueError, KeyError, TypeError):
            raise MediaServerError(
                "invalid_response", f"no Plex at {self.cfg.url} (not Plex's API)"
            ) from None
        return [
            Library(
                id=str(d["key"]),
                name=str(d.get("title") or d["key"]),
                kind=KINDS.get(d.get("type", ""), "other"),
                paths=[str(loc["path"]) for loc in d.get("Location") or [] if loc.get("path")],
            )
            for d in dirs
        ]

    async def test(self) -> TestResult:
        try:
            libs = await self.libraries()
        except MediaServerError as exc:
            return TestResult(ok=False, message=exc.message, details={"kind": exc.kind})
        return TestResult(
            ok=True,
            message=f"Plex works: {len(libs)} librar{'y' if len(libs) == 1 else 'ies'}",
            details={"libraries": [lib.model_dump() for lib in libs]},
        )

    async def refresh(self, folders: list[str]) -> list[str]:
        libs = await self.libraries()
        problems = []
        for folder in folders:
            lib = covering(libs, folder)
            if lib is None:
                problems.append(f"no Plex library holds {folder}")
                continue
            await self._get(f"/library/sections/{lib.id}/refresh", path=folder)
        return problems


registry.register(PlexProvider)
