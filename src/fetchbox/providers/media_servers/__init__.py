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


def parent_of(path: str) -> str:
    """The folder holding ``path``, in the path's own style (``/`` or ``\\``)."""
    sep = "\\" if "\\" in path and "/" not in path else "/"
    head = path.rstrip(sep).rsplit(sep, 1)[0]
    return head or sep


MAC_DISK = ["volumes", "macintosh hd"]  # on a Mac, /Volumes/Macintosh HD/X is /X


def _names(path: str) -> list[str]:
    """The folder names in ``path``, without a Mac's /Volumes/Macintosh HD in front."""
    parts = [x for x in path.replace("\\", "/").split("/") if x]
    return parts[2:] if [x.casefold() for x in parts[:2]] == MAC_DISK else parts


def _key(names: list[str]) -> list[str]:
    return [x.casefold() for x in names]


def same_path(a: str, b: str) -> bool:
    """The same place, written either way: ``/`` or ``\\``, a trailing separator, other
    capitals (a Mac's and Windows' disks don't mind), /Volumes/Macintosh HD in front."""
    return _key(_names(a)) == _key(_names(b))


def place(libraries: list[Library], path: str) -> tuple[Library, str] | None:
    """The library whose folder holds ``path`` (the longest folder wins), and ``path``
    written the way that library writes its folder, which is the spelling to ask the
    server about. A folder written exactly the same wins over one with other capitals
    or /Volumes/Macintosh HD in front."""
    norm = path.replace("\\", "/").rstrip("/")
    exact: tuple[int, Library] | None = None
    for lib in libraries:
        for p in lib.paths:
            root = p.replace("\\", "/").rstrip("/")
            if (norm == root or norm.startswith(root + "/")) and (
                exact is None or len(root) > exact[0]
            ):
                exact = (len(root), lib)
    if exact:
        return exact[1], path
    mine = _names(path)
    near: tuple[int, Library, str] | None = None
    for lib in libraries:
        for root in lib.paths:
            theirs = _key(_names(root))
            n = len(theirs)
            if n and _key(mine[:n]) == theirs and (near is None or n > near[0]):
                near = (n, lib, root)
    if near is None:
        return None
    n, lib, root = near
    sep = "\\" if "\\" in root and "/" not in root else "/"
    return lib, root.rstrip("/\\") + "".join(sep + x for x in mine[n:])


def covering(libraries: list[Library], path: str) -> Library | None:
    """The library whose folder holds ``path`` (see ``place``)."""
    found = place(libraries, path)
    return found[0] if found else None


class MediaServerProvider(Provider):
    category: ClassVar[ProviderCategory] = "media_server"

    async def libraries(self) -> list[Library]:
        raise NotImplementedError

    async def refresh(self, folders: list[str]) -> list[str]:
        """Rescan these folders (paths as the server sees them). Returns problems worth
        telling the user (e.g. a folder no library holds); raises MediaServerError when
        the server can't be reached or refuses."""
        raise NotImplementedError

    async def files(self, library: Library, limit: int) -> list[str]:
        """Up to ``limit`` media file paths in this library, as the server sees them (to
        line up with the files Fetchbox knows). Empty: the server can't say."""
        return []

    async def exists(self, path: str) -> bool | None:
        """Does the server see this file or folder? None: it can't tell (never raises)."""
        return None

    async def aclose(self) -> None:
        return None
