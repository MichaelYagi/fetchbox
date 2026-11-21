"""Make real torrents and a local seeder for download tests (no network beyond loopback)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import libtorrent as lt

from fetchbox.providers.download_clients.builtin import BuiltinClient, BuiltinSettings
from fetchbox.providers.indexers import TorrentSource

LOOPBACK = {"listen_interfaces": "127.0.0.1:0", "dht": False, "lsd": False, "upnp": False}


@dataclass
class Made:
    torrent: bytes
    info_hash: str
    root: Path  # the directory holding the torrent's top folder
    name: str

    @property
    def magnet(self) -> str:
        return lt.make_magnet_uri(lt.torrent_info(lt.bdecode(self.torrent)))

    def source(self, *, magnet: bool = False) -> TorrentSource:
        return TorrentSource(magnet=self.magnet) if magnet else TorrentSource(torrent=self.torrent)


def make_torrent(root: Path, name: str, files: dict[str, int]) -> Made:
    """Write ``files`` (relative path -> size in bytes) under root/name and hash them."""
    top = root / name
    for rel, size in files.items():
        p = top / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(os.urandom(size))
    fs = lt.file_storage()
    for rel, size in sorted(files.items()):
        fs.add_file(f"{name}/{rel}", size)
    ct = lt.create_torrent(fs, 16 * 1024)
    lt.set_piece_hashes(ct, str(root))
    data = lt.bencode(ct.generate())
    ti = lt.torrent_info(lt.bdecode(data))
    return Made(data, str(ti.info_hashes().v1), root, name)


def engine(state_dir: Path | None = None) -> BuiltinClient:
    return BuiltinClient(BuiltinSettings.model_validate(LOOPBACK), state_dir=state_dir)


async def seeder(made: Made) -> BuiltinClient:
    from fetchbox.providers.download_clients import AddOptions

    s = engine()
    await s.start()
    await s.add(made.source(), AddOptions(save_path=str(made.root), paused=False))
    await until(lambda: _state(s, made.info_hash, "seeding"), within=15)
    return s


async def _state(c: BuiltinClient, ref: str, state: str) -> bool:
    st = await c.status([ref])
    return bool(st) and st[0].state == state


async def until(cond: Callable[[], Awaitable[bool]], *, within: float = 30.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while not await cond():
        if loop.time() > deadline:
            raise TimeoutError("condition not met in time")
        await asyncio.sleep(0.1)
