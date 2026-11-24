"""Jellyfin and Emby (same API family): API key auth, path-based change notices."""

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
    parent_of,
    same_path,
)

TIMEOUT = httpx.Timeout(20.0, connect=10.0)
KINDS = {"movies": "movie", "tvshows": "series", "music": "music"}
ITEM_TYPES = {"movie": "Movie", "series": "Episode", "music": "Audio"}


class JellyfinSettings(BaseModel):
    url: str = Field(description="The server's address, e.g. http://192.168.1.10:8096")
    api_key: SecretStr = Field(
        description="Dashboard > API Keys (Jellyfin) or Settings > Advanced > API Keys (Emby): "
        "add one named Fetchbox"
    )

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        return check_url(v)

    @field_validator("api_key", mode="before")
    @classmethod
    def _strip(cls, v: object) -> object:
        return strip_key(v)


class JellyfinProvider(MediaServerProvider):
    kind: ClassVar[str] = "jellyfin"
    Settings: ClassVar[type[BaseModel]] = JellyfinSettings
    product = "Jellyfin"

    def __init__(
        self, settings: BaseModel, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        super().__init__(settings)
        assert isinstance(settings, JellyfinSettings)
        self.cfg = settings
        self._http = httpx.AsyncClient(
            base_url=settings.url,
            timeout=TIMEOUT,
            transport=transport,
            headers={
                "Accept": "application/json",
                **self._auth(settings.api_key.get_secret_value()),
            },
        )

    def _auth(self, key: str) -> dict[str, str]:
        # Jellyfin 10.11 refuses the legacy X-Emby-Token header by default.
        return {"Authorization": f'MediaBrowser Client="Fetchbox", Token="{key}"'}

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, path: str, **kw: Any) -> httpx.Response:
        try:
            r = await self._http.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise MediaServerError(
                "unavailable",
                f"{self.product} unreachable at {self.cfg.url}: {type(exc).__name__}",
            ) from None
        if r.status_code in (401, 403):
            raise MediaServerError("auth", f"{self.product} refused the API key")
        if r.status_code >= 400:
            raise MediaServerError("unavailable", f"{self.product} returned HTTP {r.status_code}")
        return r

    async def libraries(self) -> list[Library]:
        r = await self._call("GET", "/Library/VirtualFolders")
        try:
            rows = r.json()
            return [
                Library(
                    id=str(x.get("ItemId") or x["Name"]),
                    name=str(x["Name"]),
                    kind=KINDS.get(x.get("CollectionType") or "", "other"),
                    paths=[str(p) for p in x.get("Locations") or []],
                )
                for x in rows
            ]
        except (ValueError, KeyError, TypeError):
            raise MediaServerError(
                "invalid_response", f"no {self.product} at {self.cfg.url} (not its API)"
            ) from None

    async def test(self) -> TestResult:
        try:
            info = (await self._call("GET", "/System/Info")).json()
            libs = await self.libraries()
        except (MediaServerError, ValueError) as exc:
            msg = exc.message if isinstance(exc, MediaServerError) else "unexpected reply"
            return TestResult(ok=False, message=msg)
        name = info.get("ServerName") or self.product
        return TestResult(
            ok=True,
            message=f"{self.product} {info.get('Version', '')} works ({name}): "
            f"{len(libs)} librar{'y' if len(libs) == 1 else 'ies'}",
            details={"libraries": [lib.model_dump() for lib in libs]},
        )

    async def files(self, library: Library, limit: int) -> list[str]:
        kind = ITEM_TYPES.get(library.kind)
        if kind is None:
            return []
        params = {"ParentId": library.id, "Recursive": "true", "IncludeItemTypes": kind,
                  "Fields": "Path", "Limit": limit}  # fmt: skip
        r = await self._call("GET", "/Items", params=params)
        try:
            return [str(x["Path"]) for x in r.json().get("Items") or [] if x.get("Path")]
        except (ValueError, KeyError, TypeError, AttributeError):
            return []

    async def exists(self, path: str) -> bool | None:
        params = {"path": parent_of(path), "includeFiles": "true", "includeDirectories": "true"}
        try:
            r = await self._http.get("/Environment/DirectoryContents", params=params)
            if r.status_code != 200:
                return None
            seen = [str(x.get("Path", "")) for x in r.json()]
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            return None
        return any(same_path(x, path) for x in seen)

    async def refresh(self, folders: list[str]) -> list[str]:
        libs = await self.libraries()
        problems = [
            f"no {self.product} library holds {f}" for f in folders if not covering(libs, f)
        ]
        wanted = [f for f in folders if covering(libs, f)]
        if wanted:
            await self._call(
                "POST",
                "/Library/Media/Updated",
                json={"Updates": [{"Path": f, "UpdateType": "Created"} for f in wanted]},
            )
        return problems


class EmbyProvider(JellyfinProvider):
    kind: ClassVar[str] = "emby"
    product = "Emby"

    def _auth(self, key: str) -> dict[str, str]:
        return {"X-Emby-Token": key}


registry.register(JellyfinProvider)
registry.register(EmbyProvider)
