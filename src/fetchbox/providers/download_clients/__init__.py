"""Download clients: the built-in libtorrent engine, later qBittorrent/Transmission/Deluge.

Fetchbox, not the client, enforces queue rules (max active, priorities): it adds
torrents paused and resumes the ones that should run, so every client behaves alike.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Literal

from pydantic import BaseModel

from fetchbox.core.providers import Provider, ProviderCategory
from fetchbox.providers.indexers import TorrentSource

ClientRef = str  # the torrent's info hash (v1 hex; v2-only torrents: truncated v2)
TorrentState = Literal["metadata", "checking", "downloading", "seeding", "paused", "error"]


class DownloadClientError(Exception):
    def __init__(
        self, kind: Literal["unavailable", "not_found", "invalid_torrent", "rejected"], message: str
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass(frozen=True)
class AddOptions:
    save_path: str | None  # None: the client's own default folder (external clients)
    category: str | None = None
    paused: bool = True
    download_limit: int | None = None  # bytes/s
    upload_limit: int | None = None


@dataclass(frozen=True)
class TransferLimits:
    download: int | None = None  # bytes/s; None = unlimited
    upload: int | None = None


class ClientTorrent(BaseModel):
    ref: ClientRef
    name: str
    state: TorrentState
    progress: float  # 0..1
    size: int | None  # bytes wanted; None until metadata arrives
    downloaded: int
    uploaded: int
    download_rate: int  # bytes/s
    upload_rate: int
    seeds: int
    peers: int
    ratio: float
    seed_seconds: int
    save_path: str
    error: str | None = None
    eta_seconds: int | None = None


class ClientFile(BaseModel):
    path: str  # relative to the torrent's save path
    size: int
    progress: float


class DownloadClientProvider(Provider):
    category: ClassVar[ProviderCategory] = "download_client"
    # What it downloads: torrents, Usenet NZBs (SABnzbd, NZBGet), or direct files
    protocol: ClassVar[Literal["torrent", "usenet", "direct"]] = "torrent"

    async def start(self) -> None:
        """Called once before use (the built-in engine opens its session here)."""

    async def stop(self) -> None:
        """Called on shutdown; the built-in engine saves resume data here."""

    async def add(self, src: TorrentSource, opts: AddOptions) -> ClientRef:
        raise NotImplementedError

    async def status(self, refs: list[ClientRef] | None = None) -> list[ClientTorrent]:
        raise NotImplementedError

    async def files(self, ref: ClientRef) -> list[ClientFile]:
        raise NotImplementedError

    async def set_limits(self, ref: ClientRef | None, limits: TransferLimits) -> None:
        raise NotImplementedError

    async def pause(self, ref: ClientRef) -> None:
        raise NotImplementedError

    async def resume(self, ref: ClientRef) -> None:
        raise NotImplementedError

    async def remove(self, ref: ClientRef, delete_data: bool) -> None:
        raise NotImplementedError

    # Clients that can move a finished torrent's files into the library and keep seeding
    # them from there (the built-in engine): one file, in the library, no copies.
    can_relocate: bool = False

    async def relocate(self, ref: ClientRef, moves: dict[str, str]) -> None:
        """Move files of the torrent (its paths, as ``files`` lists them) to absolute
        paths; seeding continues from there."""
        raise NotImplementedError

    async def recheck(self, ref: ClientRef) -> None:
        raise NotImplementedError
