"""The built-in libtorrent engine, downloading real torrents over loopback."""

from __future__ import annotations

from pathlib import Path

import pytest

from fetchbox.core.providers import registry
from fetchbox.providers.download_clients import AddOptions, DownloadClientError, TransferLimits
from fetchbox.providers.download_clients.builtin import BuiltinClient
from tests.torrents import engine, make_torrent, seeder, until

MB = 1024 * 1024


async def _done(c: BuiltinClient, ref: str) -> bool:
    st = await c.status([ref])
    return bool(st) and st[0].state == "seeding"


@pytest.mark.parametrize("magnet", [False, True], ids=["torrent", "magnet"])
async def test_download_from_a_local_seeder(tmp_path: Path, magnet: bool) -> None:
    made = make_torrent(tmp_path / "seed", "Movie.2021.1080p", {"movie.mkv": 2 * MB, "a.nfo": 100})
    seed = await seeder(made)
    leech = engine(tmp_path / "state")
    await leech.start()
    try:
        ref = await leech.add(
            made.source(magnet=magnet), AddOptions(save_path=str(tmp_path / "dl"))
        )
        assert ref == made.info_hash
        st = (await leech.status([ref]))[0]
        assert st.state == "paused"  # added paused: the queue manager starts it
        await leech.resume(ref)
        leech.connect_peer(ref, "127.0.0.1", seed.listen_port or 0)
        await until(lambda: _done(leech, ref))

        st = (await leech.status([ref]))[0]
        assert st.progress == 1.0 and st.size == 2 * MB + 100 and st.name == "Movie.2021.1080p"
        assert st.downloaded >= 2 * MB and st.error is None
        got = (tmp_path / "dl" / "Movie.2021.1080p" / "movie.mkv").read_bytes()
        assert got == (made.root / "Movie.2021.1080p" / "movie.mkv").read_bytes()
        files = {f.path.replace("\\", "/"): f for f in await leech.files(ref)}
        assert set(files) == {"Movie.2021.1080p/movie.mkv", "Movie.2021.1080p/a.nfo"}
        assert all(f.progress == 1.0 for f in files.values())

        # adding the same torrent again is a no-op
        assert await leech.add(made.source(), AddOptions(save_path=str(tmp_path / "dl"))) == ref
        assert len(await leech.status()) == 1
    finally:
        await leech.stop()
        await seed.stop()


async def test_resume_data_survives_a_restart(tmp_path: Path) -> None:
    made = make_torrent(tmp_path / "seed", "Show", {"e1.mkv": MB})
    seed = await seeder(made)
    state = tmp_path / "state"
    first = engine(state)
    await first.start()
    ref = await first.add(made.source(), AddOptions(save_path=str(tmp_path / "dl"), paused=False))
    first.connect_peer(ref, "127.0.0.1", seed.listen_port or 0)
    await until(lambda: _done(first, ref))
    await first.stop()
    await seed.stop()
    assert (state / f"{ref}.fastresume").exists()

    second = engine(state)  # no seeder any more: it must not need to download again
    await second.start()
    try:
        await until(lambda: _done(second, ref), within=15)
        assert (await second.status([ref]))[0].progress == 1.0
    finally:
        await second.stop()


async def test_pause_remove_and_errors(tmp_path: Path) -> None:
    made = make_torrent(tmp_path / "seed", "X", {"x.bin": 64 * 1024})
    c = engine(tmp_path / "state")
    await c.start()
    try:
        ref = await c.add(made.source(), AddOptions(save_path=str(tmp_path / "dl"), paused=False))
        assert (await c.status([ref]))[0].state != "paused"
        await c.pause(ref)
        await until(lambda: _paused(c, ref), within=5)
        await c.set_limits(ref, TransferLimits(download=1000))
        await c.set_limits(None, TransferLimits(upload=5000))
        await c.recheck(ref)

        await c.remove(ref, delete_data=True)
        assert await c.status() == []
        assert not (tmp_path / "state" / f"{ref}.fastresume").exists()
        with pytest.raises(DownloadClientError) as exc:
            await c.pause(ref)
        assert exc.value.kind == "not_found"
        with pytest.raises(DownloadClientError) as exc:
            from fetchbox.providers.indexers import TorrentSource

            await c.add(TorrentSource(torrent=b"not bencode"), AddOptions(save_path=str(tmp_path)))
        assert exc.value.kind == "invalid_torrent"
        assert (await c.test()).ok
    finally:
        await c.stop()
    assert not (await c.test()).ok
    with pytest.raises(DownloadClientError):
        await c.status()


async def _paused(c: BuiltinClient, ref: str) -> bool:
    return (await c.status([ref]))[0].state == "paused"


def test_registered() -> None:
    assert registry.get("download_client", "builtin") is BuiltinClient
