"""Import an existing library: scan (read-only), review, apply (files recorded in place)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from fetchbox.modules.library.existing import folder_hints
from tests.conftest import Running
from tests.pipeline import Fakes, standard_hd
from tests.torrents import until

KB = 1024


def _write(root: Path, rel: str, size: int = 50 * KB) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\0" * size)


async def _job(c: httpx.AsyncClient, job_id: str) -> dict[str, Any]:
    out: dict[str, Any] = {}

    async def done() -> bool:
        nonlocal out
        out = (await c.get(f"/jobs/{job_id}")).json()
        return out["status"] in ("succeeded", "failed")

    await until(done, within=30)
    assert out["status"] == "succeeded", out
    return out


async def _dest(c: httpx.AsyncClient, kind: str) -> dict[str, Any]:
    out: dict[str, Any] = next(
        d for d in (await c.get("/destinations")).json() if d["media_kind"] == kind
    )
    return out


def test_folder_hints() -> None:
    assert folder_hints("Dune (2021) {tmdb-438631}") == {
        "tmdb_id": 438631, "title": "Dune", "year": 2021}  # fmt: skip
    assert folder_hints("Severance (2022) [tvdbid-371980]")["tvdb_id"] == 371980
    assert folder_hints("Amélie (2001)") == {"title": "Amélie", "year": 2001}
    h = folder_hints("Dune.2021.1080p.BluRay.x264-GRP")
    assert h["title"] == "Dune" and h["year"] == 2021
    assert folder_hints("Some Film")["year"] is None


async def test_scan_then_import_movies(c: httpx.AsyncClient, fakes: Fakes, server: Running) -> None:
    dest = await _dest(c, "movie")
    root = Path(dest["path"])
    _write(root, "Dune (2021) {tmdb-438631}/Dune (2021) Bluray-1080p.mkv", 300 * KB)
    _write(root, "Dune (2021) {tmdb-438631}/Sample/dune-sample.mkv")
    _write(root, "Dune (2021) {tmdb-438631}/Dune (2021).nfo")
    _write(root, "Amélie (2001) 720p.mkv")  # a loose file
    _write(root, "Totally Unknown Film (1999)/film.avi")
    _write(root, "Empty Folder/notes.txt")
    before = sorted(p.as_posix() for p in root.rglob("*"))

    r = await c.post("/library/scan", json={"destination_id": dest["id"]})
    assert r.status_code == 202, r.text
    scan = (await _job(c, r.json()["id"]))["result"]
    assert sorted(p.as_posix() for p in root.rglob("*")) == before  # read-only
    items = {i["folder"]: i for i in scan["items"]}
    assert sorted(items) == ["Amélie (2001) 720p.mkv", "Dune (2021) {tmdb-438631}",
                             "Totally Unknown Film (1999)"]  # fmt: skip
    dune = items["Dune (2021) {tmdb-438631}"]
    assert dune["confidence"] == "exact" and dune["match"]["tmdb_id"] == 438631
    assert [f["path"] for f in dune["files"]] == [
        "Dune (2021) {tmdb-438631}/Dune (2021) Bluray-1080p.mkv"  # no sample
    ]
    assert dune["status"] == "new" and dune["quality"] == "Bluray-1080p"
    amelie = items["Amélie (2001) 720p.mkv"]
    assert amelie["match"]["tmdb_id"] == 194 and amelie["confidence"] == "high"
    unknown = items["Totally Unknown Film (1999)"]
    assert unknown["match"] is None and unknown["confidence"] == "none"
    assert scan["counts"] == {"new": 3, "in_library": 0, "imported": 0}

    profile = await standard_hd(c)
    body = {
        "destination_id": dest["id"],
        "items": [
            {"folder": i["folder"], "files": [f["path"] for f in i["files"]],
             "tmdb_id": i["match"]["tmdb_id"], "profile_id": profile}
            for i in (dune, amelie)
        ],
    }  # fmt: skip
    r = await c.post("/library/import", json=body)
    assert r.status_code == 202, r.text
    res = (await _job(c, r.json()["id"]))["result"]
    assert [a["name"] for a in res["added"]] == ["Dune", "Amélie"] and res["files"] == 2
    assert res["skipped"] == []
    assert sorted(p.as_posix() for p in root.rglob("*")) == before  # nothing moved

    t = (await c.get(f"/titles/{res['added'][0]['title_id']}")).json()
    w = t["wanted"][0]
    assert w["state"] == "acquired" and w["cutoff_met"]  # Bluray-1080p meets Standard HD
    assert w["next_search_at"] is not None  # no search right away
    [f] = (await c.get(f"/titles/{t['id']}/files")).json()
    assert f["relative_path"] == dune["files"][0]["path"] and f["link_mode"] == "existing"
    hist = (await c.get(f"/titles/{t['id']}/history")).json()["items"]
    assert [h["type"] for h in hist] == ["imported"] and hist[0]["data"]["existing"]
    a = (await c.get(f"/titles/{res['added'][1]['title_id']}")).json()["wanted"][0]
    assert a["state"] == "acquired" and not a["cutoff_met"]  # 720p: upgradable

    # scanning again: both already imported; importing again changes nothing
    again = (await _job(c, (await c.post("/library/scan",
              json={"destination_id": dest["id"]})).json()["id"]))["result"]  # fmt: skip
    assert again["counts"] == {"new": 1, "in_library": 0, "imported": 2}
    res2 = (await _job(c, (await c.post("/library/import", json=body)).json()["id"]))["result"]
    assert res2["files"] == 0 and all(not x["new"] for x in res2["added"])


async def test_import_a_series_by_episode(c: httpx.AsyncClient, fakes: Fakes) -> None:
    dest = await _dest(c, "series")
    root = Path(dest["path"])
    show = "Severance (2022) {tvdb-371980}"
    _write(root, f"{show}/Season 01/Severance - S01E01 - Good News.mkv")
    _write(root, f"{show}/Season 01/Severance.S01E02.1080p.WEB-DL-NTb.mkv")
    _write(root, f"{show}/Season 02/Severance.S02E01E02.720p.HDTV.x264-GRP.mkv")
    _write(root, f"{show}/Extras/Behind the scenes.mkv")
    scan = (await _job(c, (await c.post("/library/scan",
            json={"destination_id": dest["id"]})).json()["id"]))["result"]  # fmt: skip
    [item] = scan["items"]
    assert item["match"]["tmdb_id"] == 95396 and item["confidence"] == "exact"  # by tvdb id
    assert len(item["files"]) == 4

    body = {"destination_id": dest["id"], "items": [
        {"folder": item["folder"], "files": [f["path"] for f in item["files"]],
         "tmdb_id": 95396, "profile_id": await standard_hd(c), "monitor_mode": "all"}]}  # fmt: skip
    res = (await _job(c, (await c.post("/library/import", json=body)).json()["id"]))["result"]
    assert res["files"] == 3  # the extra matched no episode
    tid = res["added"][0]["title_id"]
    seasons = (await c.get(f"/titles/{tid}/episodes")).json()
    eps = {(s["number"], e["number"]): e["wanted"] for s in seasons for e in s["episodes"]}
    got = {k for k, w in eps.items() if w["state"] == "acquired"}
    assert got == {(1, 1), (1, 2), (2, 1), (2, 2)}
    assert eps[(1, 3)]["state"] == "missing" and eps[(1, 3)]["monitored"]
    files = (await c.get(f"/titles/{tid}/files")).json()
    multi = next(f for f in files if "S02E01E02" in f["relative_path"])
    assert len(multi["wanted_item_ids"]) == 2


async def test_scan_needs_a_folder_and_a_known_destination(c: httpx.AsyncClient) -> None:
    r = await c.post("/library/scan", json={"destination_id": "nope"})
    assert r.status_code == 404
    r = await c.post("/library/import", json={"destination_id": "nope", "items": []})
    assert r.status_code == 422
