"""qBittorrent, Transmission and Deluge, against in-memory fakes of their web APIs."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from fetchbox.providers.download_clients import (
    AddOptions,
    DownloadClientError,
    DownloadClientProvider,
    TransferLimits,
)
from fetchbox.providers.download_clients.external import (
    DelugeClient,
    ExternalSettings,
    QbittorrentClient,
    TransmissionClient,
    TransmissionSettings,
)
from fetchbox.providers.indexers import TorrentSource
from tests.conftest import Running
from tests.fake_clients import FakeDeluge, FakeQbit, FakeTransmission, Store
from tests.fake_torznab import Item
from tests.pipeline import Fakes, _indexer, _movie
from tests.torrents import Made, make_torrent, until

NAME = "Dune.2021.1080p.WEB-DL.DDP5.1.Atmos.H.264-FLUX"
Kind = tuple[type[DownloadClientProvider], Callable[[Store], Any], dict[str, Any]]
KINDS: dict[str, Kind] = {
    "qbittorrent": (QbittorrentClient, FakeQbit, {"username": "admin", "password": "pw"}),
    "transmission": (TransmissionClient, FakeTransmission, {"username": "admin", "password": "pw"}),
    "deluge": (DelugeClient, FakeDeluge, {"password": "pw"}),
}


@pytest.fixture
def made(tmp_path: Path) -> Made:
    return make_torrent(tmp_path / "src", NAME, {f"{NAME}.mkv": 1000})


def _client(kind: str, store: Store, **settings: Any) -> tuple[DownloadClientProvider, Any]:
    cls, fake_cls, creds = KINDS[kind]
    fake = fake_cls(store)
    model = (TransmissionSettings if kind == "transmission" else ExternalSettings)(
        url="http://client:8080", **{**creds, **settings}
    )
    return cls(model, transport=httpx.MockTransport(fake.handle)), fake  # type: ignore[call-arg]


@pytest.mark.parametrize("kind", list(KINDS))
@pytest.mark.parametrize("as_file", [False, True], ids=["magnet", "torrent"])
async def test_lifecycle(kind: str, as_file: bool, made: Made) -> None:
    store = Store()
    c, _ = _client(kind, store)
    t = await c.test()
    assert t.ok, t.message
    src = TorrentSource(torrent=made.torrent) if as_file else TorrentSource(magnet=made.magnet)
    opts = AddOptions(save_path="/data/torrents/fetchbox-movies", category="fetchbox-movies")
    ref = await c.add(src, opts)
    assert ref == made.info_hash
    assert await c.add(src, opts) == ref  # adding twice is fine

    [st] = await c.status([ref])
    assert st.state == "paused" and st.save_path == "/data/torrents/fetchbox-movies"
    assert await c.status([]) == []
    await c.resume(ref)
    [st] = await c.status([ref])
    assert st.state == "downloading" and st.progress == 0

    store.finish(ref)
    await c.pause(ref)  # a finished torrent counts as seeding even when stopped
    [st] = await c.status(None)
    assert st.state == "seeding" and st.progress == 1.0 and st.size == 1000
    [f] = await c.files(ref)
    assert f.path == f"{NAME}/{NAME}.mkv" and f.size == 1000 and f.progress == 1.0

    await c.set_limits(ref, TransferLimits(download=2048, upload=None))
    await c.set_limits(None, TransferLimits(download=None, upload=4096))
    await c.recheck(ref)
    await c.remove(ref, True)
    assert store.limits["deleted_files"] in ("true", True)
    assert await c.status([ref]) == []
    await c.stop()


@pytest.mark.parametrize("kind", list(KINDS))
async def test_bad_credentials(kind: str) -> None:
    c, _ = _client(kind, Store(), password="wrong")
    t = await c.test()
    assert not t.ok and t.details["kind"] == "rejected"
    await c.stop()


async def test_unreachable_client() -> None:
    def down(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    c = QbittorrentClient(
        ExternalSettings(url="http://client:8080"), transport=httpx.MockTransport(down)
    )
    t = await c.test()
    assert not t.ok and t.details["kind"] == "unavailable"
    with pytest.raises(DownloadClientError, match="unreachable"):
        await c.status(None)
    await c.stop()


async def test_qbittorrent_5_and_expired_session(made: Made) -> None:
    store = Store()
    fake = FakeQbit(store, version="v5.0.2", api="2.11.2")
    c = QbittorrentClient(
        ExternalSettings(url="http://client:8080", username="admin", password="pw"),
        transport=httpx.MockTransport(fake.handle),
    )
    ref = await c.add(TorrentSource(magnet=made.magnet), AddOptions(save_path="/d"))
    fake.sid = "rotated"  # the session expired: the next call logs in again
    await c.resume(ref)
    assert "torrents/start" in store.calls and store.calls.count("auth/login") == 2
    await c.pause(ref)
    assert "torrents/stop" in store.calls
    await c.stop()


async def test_transmission_session_id_rotates(made: Made) -> None:
    store = Store()
    c, fake = _client("transmission", store)
    await c.add(TorrentSource(magnet=made.magnet), AddOptions(save_path="/d"))
    fake.session = "sess-2"
    assert len(await c.status(None)) == 1
    await c.stop()


async def test_bad_torrent_is_refused() -> None:
    c, _ = _client("qbittorrent", Store())
    with pytest.raises(DownloadClientError) as e:
        await c.add(TorrentSource(torrent=b"not a torrent"), AddOptions(save_path="/d"))
    assert e.value.kind == "invalid_torrent"
    await c.stop()


# ---- through the API -----------------------------------------------------------------------------


async def test_add_client_and_download_through_it(
    c: httpx.AsyncClient, fakes: Fakes, server: Running, made: Made, settings: Any
) -> None:
    server.app.state.downloads.poll_interval = 0.1
    server.app.state.library.sweep_interval = 0.2
    torrents_local = settings.downloads_dir / "Torrents"
    # qBittorrent saves into its own folder (Fetchbox doesn't tell it where); that folder
    # is shared, and the mapping says where Fetchbox sees it.
    store = Store(
        local_root=torrents_local, client_root="/data/torrents", default_dir="/data/torrents"
    )
    fake = FakeQbit(store)
    server.app.state.downloads.provider_kwargs = {"transport": httpx.MockTransport(fake.handle)}

    kinds = [k["kind"] for k in (await c.get("/download-clients/kinds")).json()]
    assert kinds == sorted(kinds) or set(kinds) == {"qbittorrent", "transmission", "deluge"}
    assert {"qbittorrent", "transmission", "deluge"} <= set(kinds) and "builtin" not in kinds

    body = {
        "kind": "qbittorrent",
        "name": "NAS qBittorrent",
        "settings": {"url": "http://client:8080", "username": "admin", "password": "pw"},
        "policy": {"path_mappings": [{"local": str(torrents_local), "client": "/data/torrents"}]},
    }
    assert (await c.post("/download-clients", json={**body, "kind": "nope"})).status_code == 422
    r = await c.post("/download-clients", json=body)
    assert r.status_code == 201, r.text
    qb = r.json()
    assert qb["settings"]["password"] == "********" and qb["running"]
    assert (await c.post("/download-clients", json=body)).status_code == 409
    assert (await c.post(f"/download-clients/{qb['id']}/test")).json()["ok"] is True
    builtin = next(x for x in (await c.get("/download-clients")).json() if x["kind"] == "builtin")
    r = await c.patch(f"/download-clients/{builtin['id']}", json={"enabled": False})
    assert r.status_code == 200
    # sending the mask back keeps the stored password
    r = await c.patch(f"/download-clients/{qb['id']}", json={"settings": {"password": "********"}})
    assert r.status_code == 200
    assert (await c.post(f"/download-clients/{qb['id']}/test")).json()["ok"] is True

    await _indexer(c, fakes, "one", [Item(NAME, magnet=made.magnet, infohash=made.info_hash)])
    movie = await _movie(c, search=True)

    async def in_client() -> bool:
        return made.info_hash in store.torrents

    async def recorded() -> bool:  # the client has it before Fetchbox's row is committed
        return bool((await c.get("/downloads")).json())

    await until(in_client, within=20)
    await until(recorded, within=10)
    t = store.torrents[made.info_hash]
    assert t.save_path == "/data/torrents" and t.category == "fetchbox-movies"
    [d] = (await c.get("/downloads")).json()
    assert d["client_id"] == qb["id"]
    assert d["save_path"] == str(torrents_local)  # qBittorrent's folder, as Fetchbox sees it
    clients = {x["id"]: x for x in (await c.get("/download-clients")).json()}
    assert clients[qb["id"]]["saves_to"] is None  # the client decides
    assert clients[builtin["id"]]["saves_to"].endswith("Torrents")

    async def running() -> bool:
        return not store.torrents[made.info_hash].paused

    await until(running, within=10)  # Fetchbox's queue manager resumed it
    store.finish(made.info_hash)

    async def acquired() -> bool:
        w = (await c.get(f"/titles/{movie['id']}")).json()["wanted"][0]
        return bool(w["state"] == "acquired")

    await until(acquired, within=20)
    [f] = (await c.get(f"/titles/{movie['id']}/files")).json()
    assert (settings.downloads_dir / "Movies" / f["relative_path"]).is_file()

    r = await c.delete(f"/download-clients/{builtin['id']}")
    assert r.status_code == 409  # the built-in engine stays
    r = await c.delete(f"/download-clients/{qb['id']}")
    assert r.status_code == 409  # still seeding a download
    await c.patch(f"/download-clients/{qb['id']}", json={"policy": {"seed_minutes": 0}})

    async def seeded() -> bool:
        return bool((await c.get("/downloads")).json()[0]["seeded_at"])

    await until(seeded, within=10)
    assert made.info_hash not in store.torrents
    assert (await c.delete(f"/download-clients/{qb['id']}")).status_code == 204
    [d] = (await c.get("/downloads")).json()
    assert d["client_id"] is None  # history kept
