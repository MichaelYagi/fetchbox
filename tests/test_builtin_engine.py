"""The built-in libtorrent engine, downloading real torrents over loopback."""

from __future__ import annotations

from pathlib import Path

import pytest

from fetchbox.core.providers import registry
from fetchbox.providers.download_clients import AddOptions, DownloadClientError, TransferLimits
from fetchbox.providers.download_clients.builtin import BuiltinClient
from tests.torrents import digest, engine, make_torrent, seeder, until

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
        assert digest(got) == digest(made.payload["movie.mkv"])
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


def _loopback() -> str:
    import psutil

    for name, addrs in psutil.net_if_addrs().items():
        stats = psutil.net_if_stats().get(name)
        if stats and stats.isup and any(a.address == "127.0.0.1" for a in addrs):
            return name
    pytest.skip("no loopback interface with 127.0.0.1")


async def test_bound_to_an_interface_with_kill_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bound to an interface (here loopback, standing in for a VPN's), torrents only use
    it; when it goes down every torrent pauses, and resumes when it's back."""
    from fetchbox.providers.download_clients import builtin
    from fetchbox.providers.download_clients.builtin import BuiltinSettings
    from tests.torrents import LOOPBACK

    iface = _loopback()
    made = make_torrent(tmp_path / "seed", "Bound.2021.1080p", {"movie.mkv": MB})
    seed = await seeder(made)
    leech = BuiltinClient(
        BuiltinSettings.model_validate({**LOOPBACK, "interface": iface, "lsd": True,
                                        "upnp": True}),
    )  # fmt: skip
    await leech.start()
    try:
        s = leech._s()
        pack = s.get_settings()
        assert pack["outgoing_interfaces"] == "127.0.0.1"
        assert pack["listen_interfaces"].startswith("127.0.0.1:")
        assert not pack["enable_lsd"] and not pack["enable_upnp"]  # nothing announced locally
        assert (await leech.test()).ok
        ref = await leech.add(made.source(), AddOptions(save_path=str(tmp_path / "dl"),
                                                        paused=False))  # fmt: skip
        leech.connect_peer(ref, "127.0.0.1", seed.listen_port or 0)
        await until(lambda: _done(leech, ref))

        monkeypatch.setattr(builtin, "interface_address", lambda name: None)  # VPN drops
        leech.check_interface()
        assert s.is_paused() and leech.blocked and iface in leech.blocked
        res = await leech.test()
        assert not res.ok and "kill switch" in res.message

        monkeypatch.setattr(builtin, "interface_address", lambda name: "127.0.0.1")  # back
        leech.check_interface()
        assert not s.is_paused() and leech.blocked is None and (await leech.test()).ok
    finally:
        await leech.stop()
        await seed.stop()


async def test_missing_interface_starts_paused() -> None:
    from fetchbox.providers.download_clients.builtin import BuiltinSettings
    from tests.torrents import LOOPBACK

    c = BuiltinClient(BuiltinSettings.model_validate({**LOOPBACK, "interface": "no-such-vpn0"}))
    await c.start()
    try:
        assert c._s().is_paused()
        res = await c.test()
        assert not res.ok and "no-such-vpn0" in res.message
    finally:
        await c.stop()
    off = BuiltinClient(BuiltinSettings.model_validate(
        {**LOOPBACK, "interface": "no-such-vpn0", "kill_switch": False}))  # fmt: skip
    await off.start()
    try:
        assert not off._s().is_paused() and (await off.test()).ok
    finally:
        await off.stop()


async def test_files_moved_into_the_library_seed_from_there_across_a_restart(
    tmp_path: Path,
) -> None:
    """A finished torrent's file is moved into the library (a rename on the same drive);
    the engine seeds from it there, a new peer can download from it, and the new path
    survives a restart (resume data)."""
    made = make_torrent(tmp_path / "seedroot", "Movie.2021", {"Movie.2021.mkv": 400_000,
                                                             "info.nfo": 60})  # fmt: skip
    first = await seeder(made)
    state = tmp_path / "state"
    e = engine(state)
    await e.start()
    inc = tmp_path / "lib" / "Movies" / ".incomplete"
    ref = await e.add(made.source(), AddOptions(save_path=str(inc), paused=False))

    async def done() -> bool:
        e.connect_peer(ref, "127.0.0.1", first.listen_port or 0)
        return all(f.progress >= 1 for f in await e.files(ref))

    await until(done, within=30)
    await first.stop()  # from here on, only the library copy exists
    dst = tmp_path / "lib" / "Movies" / "Movie (2021)" / "Movie (2021).mkv"
    await e.relocate(ref, {"Movie.2021/Movie.2021.mkv": str(dst)})
    assert digest(dst.read_bytes()) == digest(made.payload["Movie.2021.mkv"])
    assert not (inc / "Movie.2021" / "Movie.2021.mkv").exists()
    assert str(dst) in [f.path for f in await e.files(ref)]
    with pytest.raises(DownloadClientError, match="isn't a file"):
        await e.relocate(ref, {"nope.mkv": str(dst)})
    await e.stop()  # writes the resume data

    e2 = engine(state)
    await e2.start()
    try:
        assert str(dst) in [f.path for f in await e2.files(ref)]  # still known after a restart
        leech = engine()
        await leech.start()
        try:
            r2 = await leech.add(made.source(), AddOptions(save_path=str(tmp_path / "other"),
                                                           paused=False))  # fmt: skip

            async def got() -> bool:
                leech.connect_peer(r2, "127.0.0.1", e2.listen_port or 0)
                return all(f.progress >= 1 for f in await leech.files(r2))

            await until(got, within=40)  # downloaded from the library file
        finally:
            await leech.stop()
    finally:
        await e2.stop()
