"""Storage backends: where files live. ``local`` covers the host's disks and OS mounts."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar

from pydantic import BaseModel

from fetchbox.core.providers import Provider, ProviderCategory

PARTIAL_SUFFIX = ".fetchbox-partial"


class StorageError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind  # not_found, exists, permission, no_space, cross_device, verify, io
        self.message = message


@dataclass(frozen=True)
class FileStat:
    path: str
    is_dir: bool
    size: int
    mtime: datetime
    device: int  # filesystem id: equal devices can hardlink


@dataclass(frozen=True)
class DiskUsage:
    total: int
    used: int
    free: int


class AccessReport(BaseModel):
    exists: bool
    is_dir: bool = False
    readable: bool = False
    writable: bool = False
    deletable: bool = False
    hardlink_with: bool | None = None  # can hardlink from the probed source path
    same_fs_as: bool | None = None
    error: str | None = None


class StorageBackend(Provider):
    category: ClassVar[ProviderCategory] = "storage"

    async def stat(self, path: str) -> FileStat | None:
        raise NotImplementedError

    def list(self, path: str, recursive: bool = False) -> AsyncIterator[FileStat]:
        raise NotImplementedError

    async def usage(self, path: str) -> DiskUsage:
        raise NotImplementedError

    async def check_access(self, path: str, *, link_source: str | None = None) -> AccessReport:
        raise NotImplementedError

    async def move(self, src: str, dst: str) -> None:
        raise NotImplementedError

    async def copy(self, src: str, dst: str, verify: bool = True) -> None:
        raise NotImplementedError

    async def hardlink(self, src: str, dst: str) -> None:
        raise NotImplementedError

    async def symlink(self, src: str, dst: str) -> None:
        raise NotImplementedError

    async def delete(self, path: str, *, recycle_dir: str | None = None) -> str | None:
        """Delete, or move into ``recycle_dir`` when given. Returns the recycled path."""
        raise NotImplementedError
