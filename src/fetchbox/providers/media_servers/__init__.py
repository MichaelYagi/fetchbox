"""Media servers (Plex, Jellyfin, Emby): told to rescan folders Fetchbox changed."""

from __future__ import annotations

from typing import ClassVar, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel

from fetchbox.core.providers import Provider, ProviderCategory

LibraryKind = Literal["movie", "series", "music", "other"]


class MediaServerError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind  # auth, unavailable, config, invalid_response
        self.message = message


class Library(BaseModel):
    id: str
    name: str
    kind: LibraryKind
    paths: list[str]  # as the server sees them


def check_url(v: str) -> str:
    v = v.strip().rstrip("/")
    parts = urlsplit(v)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("must be an http(s) URL, e.g. http://192.168.1.10:8096")
    return v


def covering(libraries: list[Library], path: str) -> Library | None:
    """The library whose folder holds ``path`` (longest folder wins)."""
    norm = path.replace("\\", "/").rstrip("/")
    best: tuple[int, Library] | None = None
    for lib in libraries:
        for p in lib.paths:
            root = p.replace("\\", "/").rstrip("/")
            if (norm == root or norm.startswith(root + "/")) and (
                best is None or len(root) > best[0]
            ):
                best = (len(root), lib)
    return best[1] if best else None


class MediaServerProvider(Provider):
    category: ClassVar[ProviderCategory] = "media_server"

    async def libraries(self) -> list[Library]:
        raise NotImplementedError

    async def refresh(self, folders: list[str]) -> list[str]:
        """Rescan these folders (paths as the server sees them). Returns problems worth
        telling the user (e.g. a folder no library holds); raises MediaServerError when
        the server can't be reached or refuses."""
        raise NotImplementedError

    async def aclose(self) -> None:
        return None
