"""Grab -> download through the built-in engine, end to end over loopback."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import update

from fetchbox.core.db import utcnow
from fetchbox.modules.downloads.models import Download
from fetchbox.providers.download_clients.builtin import BuiltinClient
from tests.conftest import Running
from tests.fake_torznab import Item
from tests.pipeline import Fakes, _indexer, _movie
from tests.torrents import Made, make_torrent, seeder, until

NAME = "Dune.2021.1080p.WEB-DL.DDP5.1.Atmos.H.264-FLUX"
MB = 1024 * 1024


@pytest.fixture
async def made(tmp_path: Path) -> Made:
    return make_torrent(tmp_path / "seedroot", NAME, {f"{NAME}.mkv": 2 * MB, f"{NAME}.nfo": 50})


@pytest.fixture
async def seed(made: Made) -> AsyncIterator[BuiltinClient]:
    s = await seeder(made)
    yield s
    await s.stop()


@pytest.fixture(autouse=True)
def fast_poll(server: Running) -> None:
    server.app.state.downloads.poll_interval = 0.1


def engine_of(server: Running) -> BuiltinClient:
    return next(c for c in server.app.state.downloads.clients.values()
                if isinstance(c, BuiltinClient))  # fmt: skip


async def _grab(c: httpx.AsyncClient, fakes: Fakes, item: Item, **search: Any) -> dict[str, Any]:
    movie = await _movie(c)
    await _indexer(c, fakes, "one", [item])
    wid = movie["wanted"][0]["id"]
    body = (await c.get("/releases", params={"wanted_item_id": wid})).json()
    rel = body["releases"][0]
    r = await c.post("/grabs", json={"release_id": rel["release_id"], "allow_rejected": True})
    assert r.status_code == 201, r.text
    return {"movie": movie, "wanted_id": wid, "grab": r.json()}


async def _download(c: httpx.AsyncClient, status: str | None = None) -> dict[str, Any] | None:
    rows = (await c.get("/downloads")).json()
    for d in rows:
        if status is None or d["status"] == status:
            out: dict[str, Any] = d
            return out
    return None


async def _wanted_state(c: httpx.AsyncClient, movie_id: str) -> str:
    return str((await c.get(f"/titles/{movie_id}")).json()["wanted"][0]["state"])


async def test_grab_downloads_to_completion(
    c: httpx.AsyncClient, fakes: Fakes, server: Running, made: Made, seed: BuiltinClient,
    settings: Any,
) -> None:  # fmt: skip
    sub = server.app.state.fb.bus.subscribe("download.*")
    g = await _grab(c, fakes, Item(NAME, magnet=made.magnet, infohash=made.info_hash))

    await until(lambda: _has(c, "downloading"), within=20)
    d = await _download(c)
    assert d is not None and d["info_hash"] == made.info_hash and d["grab_id"] == g["grab"]["id"]
    assert await _wanted_state(c, g["movie"]["id"]) == "downloading"
    engine_of(server).connect_peer(made.info_hash, "127.0.0.1", seed.listen_port or 0)

    await until(lambda: _has(c, "completed"), within=30)
    d = await _download(c, "completed")
    assert d is not None and d["progress"] == 1.0 and d["completed_at"] and d["size"] == 2 * MB + 50
    assert d["live"] is not None and d["live"]["state"] == "seeding"
    root = settings.downloads_dir / "Torrents" / "fetchbox-movies"
    assert d["save_path"] == str(root)
    got = (root / NAME / f"{NAME}.mkv").read_bytes()
    assert got == (made.root / NAME / f"{NAME}.mkv").read_bytes()

    types = []
    while not sub.queue.empty():
        types.append(sub.queue.get_nowait().type)
    assert {"download.queued", "download.started", "download.completed"} <= set(types)
    # import isn't built yet: the item waits in "downloading" for it
    assert await _wanted_state(c, g["movie"]["id"]) == "downloading"


async def _has(c: httpx.AsyncClient, status: str) -> bool:
    return await _download(c, status) is not None


async def test_unusable_torrent_fails_and_blocklists(
    c: httpx.AsyncClient, fakes: Fakes, server: Running
) -> None:
    # The fake indexer's .torrent link serves bytes that aren't a valid torrent.
    g = await _grab(c, fakes, Item(NAME, infohash="f" * 40, link="http://one/dl/0"))
    await until(lambda: _state_is(c, g["movie"]["id"], "missing"), within=20)
    assert await _download(c) is None
    blocked = (await c.get("/blocklist")).json()
    assert [b["info_hash"] for b in blocked] == ["f" * 40]
    assert "could not add the torrent" in blocked[0]["reason"]
    hist = (await c.get(f"/titles/{g['movie']['id']}/history")).json()["items"]
    assert [h["type"] for h in hist] == ["failed", "grabbed"]


async def _state_is(c: httpx.AsyncClient, movie_id: str, state: str) -> bool:
    return await _wanted_state(c, movie_id) == state


async def test_queue_limit_pause_resume_and_remove(
    c: httpx.AsyncClient, fakes: Fakes, server: Running, tmp_path: Path
) -> None:
    # Two torrents nobody seeds: they stay running, so the queue limit is visible.
    a = make_torrent(tmp_path / "a", "Dune.2021.1080p.WEB-DL-AAA", {"a.mkv": MB})
    b = make_torrent(tmp_path / "b", "Dune.2021.720p.WEB-DL-BBB", {"b.mkv": MB})
    client = (await c.get("/download-clients")).json()[0]
    r = await c.patch(f"/download-clients/{client['id']}", json={"policy": {"max_active": 1}})
    assert r.status_code == 200 and r.json()["policy"]["max_active"] == 1

    movie = await _movie(c)
    wid = movie["wanted"][0]["id"]
    items = [Item(t.name, magnet=t.magnet, infohash=t.info_hash) for t in (a, b)]
    await _indexer(c, fakes, "one", items)
    body = (await c.get("/releases", params={"wanted_item_id": wid})).json()
    first = body["releases"][0]["release_id"]
    assert (await c.post("/grabs", json={"release_id": first})).status_code == 201
    await until(lambda: _has(c, "downloading"), within=20)
    running = await _download(c, "downloading")
    assert running is not None

    # pause -> paused in the client; resume -> back to queued, then running again
    r = await c.post(f"/downloads/{running['id']}/pause")
    assert r.status_code == 200 and r.json()["status"] == "paused"
    assert (await c.post(f"/downloads/{running['id']}/pause")).status_code == 409
    r = await c.post(f"/downloads/{running['id']}/resume")
    assert r.json()["status"] == "queued"
    await until(lambda: _has(c, "downloading"), within=10)

    # removing returns the item to missing and can blocklist the release
    r = await c.delete(f"/downloads/{running['id']}", params={"blocklist": True})
    assert r.status_code == 204
    assert (await c.get(f"/downloads/{running['id']}")).json()["status"] == "removed"
    assert await _wanted_state(c, movie["id"]) == "missing"
    assert len((await c.get("/blocklist")).json()) == 1
    assert await engine_of(server).status() == []

    # the other release can now be grabbed and runs alone under max_active=1
    body = (await c.get("/releases", params={"wanted_item_id": wid})).json()
    other = next(x for x in body["releases"] if x["accepted"])
    assert (await c.post("/grabs", json={"release_id": other["release_id"]})).status_code == 201
    await until(lambda: _count(c, "downloading", 1), within=20)


async def _count(c: httpx.AsyncClient, status: str, n: int) -> bool:
    return len([d for d in (await c.get("/downloads")).json() if d["status"] == status]) == n


async def test_max_active_holds_the_second_download(
    c: httpx.AsyncClient, fakes: Fakes, server: Running, tmp_path: Path
) -> None:
    svc = server.app.state.downloads
    client = (await c.get("/download-clients")).json()[0]
    await c.patch(f"/download-clients/{client['id']}", json={"policy": {"max_active": 1}})
    torrents = [make_torrent(tmp_path / n, f"Dune.2021.1080p.WEB-DL-{n}", {"x.mkv": MB})
                for n in ("ONE", "TWO")]  # fmt: skip
    movie = await _movie(c)
    amelie = (
        await c.post("/titles", json={"tmdb_id": 194, "profile_id": movie["profile_id"]})
    ).json()
    items = [Item(t.name, magnet=t.magnet, infohash=t.info_hash) for t in torrents]
    await _indexer(c, fakes, "one", items)
    for title, t in ((movie, torrents[0]), (amelie, torrents[1])):
        wid = title["wanted"][0]["id"]
        rel = next(x for x in (await c.get("/releases", params={"wanted_item_id": wid})).json()["releases"]  # noqa: E501
                   if x["candidate"]["info_hash"] == t.info_hash)  # fmt: skip
        r = await c.post("/grabs", json={"release_id": rel["release_id"], "wanted_item_ids": [wid],
                                         "allow_rejected": True})  # fmt: skip
        assert r.status_code == 201, r.text
    await until(lambda: _count(c, "queued", 1), within=20)
    await until(lambda: _count(c, "downloading", 1), within=20)
    for _ in range(5):
        await svc.poll()
    assert await _count(c, "queued", 1)  # still held back

    # reorder puts the waiting one first, but running downloads aren't preempted
    queued = await _download(c, "queued")
    running = await _download(c, "downloading")
    assert queued and running
    r = await c.post("/downloads/reorder", json={"ids": [queued["id"], running["id"]]})
    assert r.status_code == 200 and r.json()[0]["id"] == queued["id"]
    assert (await c.post("/downloads/pause-all")).json()["count"] == 2
    assert (await c.post("/downloads/resume-all")).json()["count"] == 2
    await until(lambda: _count(c, "downloading", 1), within=10)
    assert (await _download(c, "downloading") or {})["id"] == queued["id"]  # higher priority


async def test_stalls_requeue_then_fail(
    c: httpx.AsyncClient, fakes: Fakes, server: Running, tmp_path: Path
) -> None:
    t = make_torrent(tmp_path / "s", NAME, {"x.mkv": MB})
    client = (await c.get("/download-clients")).json()[0]
    await c.patch(f"/download-clients/{client['id']}",
                  json={"policy": {"stall_minutes": 5, "stall_limit": 2}})  # fmt: skip
    g = await _grab(c, fakes, Item(NAME, magnet=t.magnet, infohash=t.info_hash))
    await until(lambda: _has(c, "downloading"), within=20)
    svc = server.app.state.downloads

    async def age(download_id: str) -> None:
        async with svc.db.sessionmaker() as s:
            await s.execute(update(Download).where(Download.id == download_id)
                            .values(last_progress_at=utcnow() - timedelta(minutes=10)))  # fmt: skip
            await s.commit()

    d = await _download(c, "downloading")
    assert d is not None
    await age(d["id"])
    await svc.poll()
    after = (await c.get(f"/downloads/{d['id']}")).json()
    assert after["stalls"] == 1  # requeued (and possibly restarted by the same pass)
    await until(lambda: _has(c, "downloading"), within=10)
    await age(d["id"])
    await svc.poll()
    failed = (await c.get(f"/downloads/{d['id']}")).json()
    assert failed["status"] == "failed" and "stalled 2 times" in failed["error"]
    assert await _wanted_state(c, g["movie"]["id"]) == "missing"
    assert [b["info_hash"] for b in (await c.get("/blocklist")).json()] == [t.info_hash]


async def test_clients_limits_and_health(c: httpx.AsyncClient, server: Running) -> None:
    [client] = (await c.get("/download-clients")).json()
    assert client["kind"] == "builtin" and client["running"] and client["name"] == "Built-in"
    assert client["policy"]["max_active"] == 3 and client["settings"]["dht"] is True
    assert (await c.post(f"/download-clients/{client['id']}/test")).json()["ok"]

    assert (await c.get("/downloads/limits")).json() == {"download": None, "upload": None}
    r = await c.put("/downloads/limits", json={"download": 500_000, "upload": 100_000})
    assert r.json() == {"download": 500_000, "upload": 100_000}
    assert (await c.get("/download-clients")).json()[0]["running"]  # restarted with the limits

    r = await c.patch(f"/download-clients/{client['id']}", json={"policy": {"max_active": 0}})
    assert r.status_code == 422 and r.json()["errors"][0]["field"].startswith("body.policy")

    health = (await c.get("/system/health")).json()
    engine = next(x for x in health["checks"] if x["name"] == "torrent_engine")
    assert engine["status"] == "ok" and "listening on port" in engine["message"]

    r = await c.patch(f"/download-clients/{client['id']}", json={"enabled": False})
    assert r.json()["running"] is False
    health = (await c.get("/system/health")).json()
    engine = next(x for x in health["checks"] if x["name"] == "torrent_engine")
    assert engine["status"] == "error"


async def test_scopes(server: Running) -> None:
    async with server.client() as root:
        ro = (
            await root.post("/auth/keys", json={"name": "ro", "scopes": ["downloads:read"]})
        ).json()
    async with server.client(ro["key"]) as c:
        assert (await c.get("/downloads")).status_code == 200
        assert (await c.post("/downloads/pause-all")).status_code == 403
        assert (await c.put("/downloads/limits", json={})).status_code == 403
