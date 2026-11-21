"""Grabs, blocklist and history through the API."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from tests.conftest import Running
from tests.pipeline import ITEMS, Fakes, _indexer, _movie

FLUX = "Dune.2021.1080p.WEB-DL.DDP5.1.Atmos.H.264-FLUX"


async def _search(c: httpx.AsyncClient, wid: str) -> dict[str, Any]:
    r = await c.get("/releases", params={"wanted_item_id": wid})
    assert r.status_code == 200, r.text
    out: dict[str, Any] = r.json()
    return out


def _by_title(body: dict[str, Any], title: str) -> dict[str, Any]:
    return next(x for x in body["releases"] if x["candidate"]["title"] == title)


async def _setup(c: httpx.AsyncClient, fakes: Fakes) -> tuple[dict[str, Any], str]:
    movie = await _movie(c)
    await _indexer(c, fakes, "one", ITEMS)
    return movie, movie["wanted"][0]["id"]


async def test_grab_moves_item_and_records_history(
    c: httpx.AsyncClient,
    fakes: Fakes,
    server: Running,
) -> None:
    movie, wid = await _setup(c, fakes)
    sub = server.app.state.fb.bus.subscribe("release.grabbed")
    rel = _by_title(await _search(c, wid), FLUX)

    r = await c.post("/grabs", json={"release_id": rel["release_id"]})
    assert r.status_code == 201, r.text
    g = r.json()
    assert g["release_title"] == FLUX and g["info_hash"] == "a" * 40 and g["reason"] == "manual"
    assert g["wanted_item_ids"] == [wid] and g["score"] == 5 and g["actor"] == "default"
    assert g["release"]["quality"] == "WEBDL-1080p" and r.headers["location"].endswith(g["id"])
    assert (await c.get(f"/grabs/{g['id']}")).json()["id"] == g["id"]
    assert [x["id"] for x in (await c.get("/grabs", params={"wanted_item_id": wid})).json()] == [
        g["id"]
    ]

    wanted = (await c.get(f"/titles/{movie['id']}")).json()["wanted"][0]
    assert wanted["state"] == "grabbed" and wanted["etag"] == '"v2"'
    ev = sub.queue.get_nowait()
    assert ev.data["name"] == "Dune" and ev.data["wanted_item_ids"] == [wid]

    hist = (await c.get(f"/titles/{movie['id']}/history")).json()["items"]
    assert [(h["type"], h["data"]["release_title"]) for h in hist] == [("grabbed", FLUX)]
    assert (await c.get("/history", params={"type": "grabbed"})).json()["items"] == hist
    assert (await c.get("/history", params={"type": "nope"})).status_code == 422

    # Same release again: the existing grab, not a duplicate.
    again = await c.post("/grabs", json={"release_id": rel["release_id"]})
    assert again.status_code == 200 and again.json()["id"] == g["id"]

    # A different release while one is in flight: conflict.
    other = _by_title(await _search(c, wid), "Dune.2021.720p.BluRay.x264-SPARKS")
    r = await c.post("/grabs", json={"release_id": other["release_id"]})
    assert r.status_code == 409 and "already being downloaded" in r.json()["detail"]


async def test_grabbed_release_is_rejected_next_time(
    c: httpx.AsyncClient, fakes: Fakes, server: Running
) -> None:
    _, wid = await _setup(c, fakes)
    rel = _by_title(await _search(c, wid), FLUX)
    assert (await c.post("/grabs", json={"release_id": rel["release_id"]})).status_code == 201
    after = _by_title(await _search(c, wid), FLUX)
    assert [r["code"] for r in after["rejections"]] == ["already_grabbed"]


async def test_rejected_release_needs_allow_rejected(c: httpx.AsyncClient, fakes: Fakes) -> None:
    _, wid = await _setup(c, fakes)
    uhd = _by_title(await _search(c, wid), "Dune.2021.2160p.WEB-DL.HEVC-GRP")
    r = await c.post("/grabs", json={"release_id": uhd["release_id"]})
    assert r.status_code == 422 and r.json()["type"].endswith("release-rejected")
    assert "not in profile" in r.json()["errors"][0]["message"]
    r = await c.post("/grabs", json={"release_id": uhd["release_id"], "allow_rejected": True})
    assert r.status_code == 201
    assert r.json()["release"]["rejections"][0]["code"] == "quality_not_allowed"


async def test_unknown_release_is_gone(c: httpx.AsyncClient, fakes: Fakes) -> None:
    r = await c.post("/grabs", json={"release_id": "01ABC"})
    assert r.status_code == 410 and r.json()["type"].endswith("release-expired")


async def test_idempotency_key(c: httpx.AsyncClient, fakes: Fakes) -> None:
    _, wid = await _setup(c, fakes)
    rel = _by_title(await _search(c, wid), FLUX)
    h = {"Idempotency-Key": "grab-1"}
    first = await c.post("/grabs", json={"release_id": rel["release_id"]}, headers=h)
    replay = await c.post("/grabs", json={"release_id": rel["release_id"]}, headers=h)
    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()


async def test_concurrent_grabs_claim_once(c: httpx.AsyncClient, fakes: Fakes) -> None:
    _, wid = await _setup(c, fakes)
    body = await _search(c, wid)
    a = _by_title(body, FLUX)["release_id"]
    b = _by_title(body, "Dune.2021.720p.BluRay.x264-SPARKS")["release_id"]
    results = await asyncio.gather(
        c.post("/grabs", json={"release_id": a}), c.post("/grabs", json={"release_id": b})
    )
    assert sorted(r.status_code for r in results) == [201, 409]
    assert len((await c.get("/grabs", params={"wanted_item_id": wid})).json()) == 1


async def test_blocklist(c: httpx.AsyncClient, fakes: Fakes) -> None:
    movie, wid = await _setup(c, fakes)
    rel = _by_title(await _search(c, wid), FLUX)
    r = await c.post("/blocklist", json={"release_id": rel["release_id"], "reason": "bad audio"})
    assert r.status_code == 201, r.text
    entry = r.json()
    assert entry["title_id"] == movie["id"] and entry["info_hash"] == "a" * 40
    assert entry["reason"] == "bad audio" and entry["indexer_id"]
    again = await c.post("/blocklist", json={"release_id": rel["release_id"]})
    assert again.status_code == 200 and again.json()["id"] == entry["id"]

    after = _by_title(await _search(c, wid), FLUX)
    assert [x["code"] for x in after["rejections"]] == ["blocklisted"]
    body = await _search(c, wid)
    assert body["picks"] and _by_title(body, FLUX)["release_id"] not in body["picks"]

    listed = (await c.get("/blocklist", params={"title_id": movie["id"]})).json()
    assert [x["id"] for x in listed] == [entry["id"]]
    assert (await c.delete(f"/blocklist/{entry['id']}")).status_code == 204
    assert _by_title(await _search(c, wid), FLUX)["accepted"]

    # manual entries, and validation
    r = await c.post("/blocklist", json={"title_id": movie["id"], "release_title": "Some.Release"})
    assert r.status_code == 201
    assert (await c.post("/blocklist", json={"reason": "x"})).status_code == 422
    assert (await c.post("/blocklist", json={"title_id": "nope", "guid": "g"})).status_code == 422
    assert (await c.post("/blocklist", json={"info_hash": "xyz"})).status_code == 422
    # deleting the title takes its blocklist with it
    assert (await c.delete(f"/titles/{movie['id']}")).status_code == 204
    assert (await c.get("/blocklist")).json() == []


async def test_scopes(server: Running, fakes: Fakes) -> None:
    async with server.client() as root:
        ro = (
            await root.post("/auth/keys", json={"name": "ro", "scopes": ["releases:read"]})
        ).json()
    async with server.client(ro["key"]) as c:
        assert (await c.get("/blocklist")).status_code == 200
        assert (await c.post("/grabs", json={"release_id": "x"})).status_code == 403
        assert (await c.post("/blocklist", json={"guid": "g"})).status_code == 403
