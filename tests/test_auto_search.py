"""Automatic search: on add, on schedule, on request, after a failed download, upgrades.

Releases are real (unseeded) torrents so a grab becomes a download that just waits,
instead of failing to add and setting off a retry search.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, update

from fetchbox.core.db import utcnow
from fetchbox.modules.library.models import MediaFile, MediaFileItem
from fetchbox.modules.media.models import WantedItem
from tests.conftest import Running
from tests.fake_torznab import GB, Item
from tests.pipeline import Fakes, _indexer, _movie
from tests.torrents import make_torrent, until

MB = 1024 * 1024
BEST = "Dune.2021.1080p.WEB-DL.DDP5.1.Atmos.H.264-FLUX"
NEXT = "Dune.2021.1080p.WEBRip.x264-SPARKS"


@pytest.fixture(autouse=True)
def fast(server: Running) -> None:
    server.app.state.downloads.poll_interval = 0.1
    server.app.state.autosearch.sweep_interval = 0.2


@pytest.fixture
def items(tmp_path: Path) -> list[Item]:
    out = []
    for name, size in ((BEST, 4 * GB), (NEXT, 3 * GB)):
        t = make_torrent(tmp_path / name, name, {"x.mkv": MB})
        out.append(Item(name, size=size, magnet=t.magnet, infohash=t.info_hash))
    out.append(Item("Dune.Part.Two.2024.1080p.WEB-DL-GRP", infohash="d" * 40))  # wrong movie
    return out


async def _grabs(c: httpx.AsyncClient, wid: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = (await c.get("/grabs", params={"wanted_item_id": wid})).json()
    return out


async def _wanted(c: httpx.AsyncClient, wid: str) -> dict[str, Any]:
    rows = (await c.get("/wanted", params={"limit": 500})).json()["items"]
    return next(w for w in rows if w["id"] == wid)


async def _job(c: httpx.AsyncClient, job_id: str) -> dict[str, Any]:
    async def done() -> bool:
        return (await c.get(f"/jobs/{job_id}")).json()["status"] in ("succeeded", "failed")

    await until(done, within=20)
    out: dict[str, Any] = (await c.get(f"/jobs/{job_id}")).json()
    return out


async def _settled(c: httpx.AsyncClient, wid: str) -> dict[str, Any]:
    """Wait until the item's search is over (it has a next search time again)."""

    async def ok() -> bool:
        w = await _wanted(c, wid)
        return w["next_search_at"] is not None and w["state"] != "searching"

    await until(ok, within=20)
    return await _wanted(c, wid)


def _at(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)


async def test_search_on_add_grabs_the_best_release(
    c: httpx.AsyncClient, fakes: Fakes, items: list[Item]
) -> None:
    await _indexer(c, fakes, "one", items)
    movie = await _movie(c, search=True)
    wid = movie["wanted"][0]["id"]

    async def grabbed() -> bool:
        return bool(await _grabs(c, wid))

    await until(grabbed, within=20)
    [g] = await _grabs(c, wid)
    assert g["release_title"] == BEST and g["reason"] == "search"
    assert g["actor"] == "automatic search"

    w = await _settled(c, wid)
    assert w["state"] in ("grabbed", "downloading") and w["last_search_at"]
    assert _at(w["next_search_at"]) > utcnow() + timedelta(hours=5)

    async def logged() -> bool:  # the activity log is written asynchronously
        msgs = [
            a["message"] for a in (await c.get("/activity", params={"limit": 50})).json()["items"]
        ]
        return any(m.startswith("Searched for Dune") for m in msgs) and any(
            m.startswith(f"Grabbed {BEST}") for m in msgs
        )

    await until(logged, within=10)


async def test_no_search_on_add_then_search_now(
    c: httpx.AsyncClient, fakes: Fakes, items: list[Item], server: Running
) -> None:
    await _indexer(c, fakes, "one", items)
    movie = await _movie(c, search=False)
    wid = movie["wanted"][0]["id"]
    w = movie["wanted"][0]
    assert _at(w["next_search_at"]) > utcnow() + timedelta(hours=5)  # the regular interval
    await server.app.state.autosearch.sweep()
    assert await _grabs(c, wid) == []

    r = await c.post(f"/wanted/{wid}/search")
    assert r.status_code == 202, r.text
    assert r.headers["Location"] == f"/api/v1/jobs/{r.json()['id']}"
    job = await _job(c, r.json()["id"])
    assert job["status"] == "succeeded", job
    assert job["result"]["grabbed"] and job["result"]["release_title"] == BEST
    assert job["result"]["accepted"] == 2 and job["result"]["results"] == 3

    # downloading now: searching again is a conflict, for the item and for its title
    r = await c.post(f"/wanted/{wid}/search")
    assert r.status_code == 409, r.text
    assert (await c.post(f"/titles/{movie['id']}/search")).status_code == 409
    assert (await c.post("/wanted/nope/search")).status_code == 404
    assert (await c.post("/titles/nope/search")).status_code == 404


async def test_indexer_added_later_searches_waiting_items(
    c: httpx.AsyncClient, fakes: Fakes, items: list[Item], server: Running
) -> None:
    movie = await _movie(c, search=True)
    wid = movie["wanted"][0]["id"]
    assert await server.app.state.autosearch.sweep() == 0
    assert (await _wanted(c, wid))["next_search_at"] is None  # still due

    await _indexer(c, fakes, "one", items)

    async def grabbed() -> bool:
        return bool(await _grabs(c, wid))

    await until(grabbed, within=20)


async def test_nothing_acceptable_waits_for_the_interval(
    c: httpx.AsyncClient, fakes: Fakes, server: Running
) -> None:
    await _indexer(c, fakes, "one", [Item("Dune.2021.1080p.WEB-DL-NOPE", seeders=0)])
    movie = await _movie(c, search=True)
    wid = movie["wanted"][0]["id"]
    w = await _settled(c, wid)
    assert w["state"] == "missing" and await _grabs(c, wid) == []
    assert _at(w["next_search_at"]) > utcnow() + timedelta(hours=5)
    assert await server.app.state.autosearch.sweep() == 0


async def test_unmonitored_items_are_left_alone_but_can_be_searched(
    c: httpx.AsyncClient, fakes: Fakes, items: list[Item], server: Running
) -> None:
    profiles = (await c.get("/quality-profiles")).json()
    hd = next(p["id"] for p in profiles if p["name"] == "1080p")
    movie = (
        await c.post("/titles", json={"tmdb_id": 438631, "profile_id": hd, "monitored": False})
    ).json()
    await _indexer(c, fakes, "one", items)
    assert await server.app.state.autosearch.sweep() == 0

    job = await _job(c, (await c.post(f"/titles/{movie['id']}/search")).json()["id"])
    assert job["result"]["grabbed"], job


async def test_automatic_off_still_searches_new_titles(
    c: httpx.AsyncClient, fakes: Fakes, items: list[Item], server: Running
) -> None:
    r = await c.patch("/system/config", json={"search": {"automatic": False}})
    assert r.status_code == 200, r.text
    await _indexer(c, fakes, "one", items)
    profiles = (await c.get("/quality-profiles")).json()
    hd = next(p["id"] for p in profiles if p["name"] == "1080p")
    old = (await c.post("/titles", json={"tmdb_id": 194, "profile_id": hd, "search": False})).json()
    async with server.app.state.fb.db.sessionmaker() as s:  # overdue
        await s.execute(update(WantedItem).values(next_search_at=utcnow() - timedelta(hours=1)))
        await s.commit()
    assert await server.app.state.autosearch.sweep() == 0  # scheduled searches are off

    new = await _movie(c, search=True)  # asked for explicitly: still searched
    wid = new["wanted"][0]["id"]

    async def grabbed() -> bool:
        return bool(await _grabs(c, wid))

    await until(grabbed, within=20)
    assert (await _wanted(c, old["wanted"][0]["id"]))["last_search_at"] is None


async def test_failed_download_searches_for_an_alternative(
    c: httpx.AsyncClient, fakes: Fakes, items: list[Item], server: Running
) -> None:
    await c.patch("/system/config", json={"search": {"retry_alternatives": 1}})
    await _indexer(c, fakes, "one", items)
    movie = await _movie(c, search=True)
    wid = movie["wanted"][0]["id"]
    downloads = server.app.state.downloads

    async def download_of(title: str) -> str:
        async def found() -> bool:
            return any(d["name"] == title for d in (await c.get("/downloads")).json())

        await until(found, within=20)
        rows = (await c.get("/downloads")).json()
        return str(next(d["id"] for d in rows if d["name"] == title))

    await downloads.fail(await download_of(BEST), "stalled")
    # BEST is blocklisted now; the next best is grabbed straight away
    await downloads.fail(await download_of(NEXT), "stalled")

    w = await _settled(c, wid)  # two failures in a row > 1 retry: back to the interval
    assert w["state"] == "missing"
    assert [g["release_title"] for g in await _grabs(c, wid)] == [NEXT, BEST]
    assert _at(w["next_search_at"]) > utcnow() + timedelta(hours=5)


async def test_upgrade_search(
    c: httpx.AsyncClient, fakes: Fakes, items: list[Item], server: Running
) -> None:
    """An acquired item below the cutoff is searched for upgrades; once its file is gone,
    it isn't (a locked item is never silently re-grabbed)."""
    movie = await _movie(c, search=False)
    wid = movie["wanted"][0]["id"]
    db = server.app.state.fb.db
    node = (await c.get("/storage/nodes")).json()[0]["id"]
    async with db.sessionmaker() as s:
        f = MediaFile(
            storage_node_id=node,
            relative_path="Dune (2021)/Dune.mkv",
            size=GB,
            quality={"name": "WEBDL-720p", "revision": 1},
        )
        s.add(f)
        await s.flush()
        s.add(MediaFileItem(media_file_id=f.id, wanted_item_id=wid))
        await s.execute(
            update(WantedItem)
            .where(WantedItem.id == wid)
            .values(state="acquired", lock="acquired", cutoff_met=False, next_search_at=None)
        )
        await s.commit()
    await _indexer(c, fakes, "one", items)

    async def grabbed() -> bool:
        return bool(await _grabs(c, wid))

    await until(grabbed, within=20)
    [g] = await _grabs(c, wid)
    assert g["release_title"] == BEST and g["reason"] == "upgrade"  # 720p BluRay isn't better

    # The download fails: the item still has its 720p file, so it is acquired, not missing.
    async def downloading() -> bool:
        return any(d["name"] == BEST for d in (await c.get("/downloads")).json())

    await until(downloading, within=20)
    did = next(d["id"] for d in (await c.get("/downloads")).json() if d["name"] == BEST)
    await c.patch("/system/config", json={"search": {"retry_alternatives": 0}})
    await server.app.state.downloads.fail(did, "stalled")
    w = await _settled(c, wid)
    assert w["state"] == "acquired" and w["lock"] == "acquired"

    # File gone: no more upgrade searches for it.
    async with db.sessionmaker() as s:
        await s.execute(delete(MediaFile))
        await s.execute(update(WantedItem).values(next_search_at=None))
        await s.commit()
    job = await server.app.state.autosearch.search(wid, reason="automatic")
    assert job == {"skipped": "acquired but the file is missing"}
    assert len(await _grabs(c, wid)) == 1


async def test_crash_mid_search_is_recovered(c: httpx.AsyncClient, server: Running) -> None:
    movie = await _movie(c, search=False)
    wid = movie["wanted"][0]["id"]
    async with server.app.state.fb.db.sessionmaker() as s:
        await s.execute(update(WantedItem).values(state="searching"))
        await s.commit()
    assert await server.app.state.autosearch.recover() == 1
    w = await _wanted(c, wid)
    assert w["state"] == "missing" and w["next_search_at"] is None


async def test_a_grab_that_never_started_is_not_retried_after_its_alternative(
    c: httpx.AsyncClient, fakes: Fakes, items: list[Item], server: Running
) -> None:
    """The best release can't even be added (an unreadable .torrent); the alternative is grabbed.
    The failed grab has no download row, but it must not be picked up again once the
    alternative's grab puts the item back in "grabbed"."""
    broken = Item(BEST, size=4 * GB, link="http://one/dl/0")  # a .torrent with no info
    await _indexer(c, fakes, "one", [broken, items[1]])
    movie = await _movie(c, search=True)
    wid = movie["wanted"][0]["id"]

    async def alternative_downloading() -> bool:
        return any(d["name"] == NEXT for d in (await c.get("/downloads")).json())

    await until(alternative_downloading, within=30)
    assert await server.app.state.downloads.sweep() == 0  # the stale grab stays put
    await asyncio.sleep(1)
    hist = (await c.get(f"/titles/{movie['id']}/history")).json()["items"]
    assert [h["type"] for h in hist] == ["grabbed", "failed", "grabbed"]
    w = await _wanted(c, wid)
    assert w["state"] == "downloading"
