"""Library check: missing files, duplicates and unknown files; forgetting missing ones."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from fetchbox.modules.library.models import MediaFile, MediaFileItem
from fetchbox.modules.storage.models import Destination
from tests.conftest import Running
from tests.pipeline import Fakes, standard_hd
from tests.test_existing_library import _dest, _job


async def test_check_and_forget(c: httpx.AsyncClient, fakes: Fakes) -> None:
    dest = await _dest(c, "movie")
    root = Path(dest["path"])
    same = b"\1" * 300_000
    for rel in ("Dune (2021) {tmdb-438631}/Dune (2021) Bluray-1080p.mkv",
                "Amélie (2001)/Amelie.2001.720p.BluRay.x264-GRP.mkv"):  # fmt: skip
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(same)  # the same file twice: a duplicate
    (root / "Stray Film (1999)").mkdir()
    (root / "Stray Film (1999)/stray.mp4").write_bytes(b"\2" * 1000)
    profile = await standard_hd(c)
    items = [
        {"folder": "Dune (2021) {tmdb-438631}", "tmdb_id": 438631, "profile_id": profile,
         "files": ["Dune (2021) {tmdb-438631}/Dune (2021) Bluray-1080p.mkv"]},
        {"folder": "Amélie (2001)", "tmdb_id": 194, "profile_id": profile,
         "files": ["Amélie (2001)/Amelie.2001.720p.BluRay.x264-GRP.mkv"]},
    ]  # fmt: skip
    res = (await _job(c, (await c.post("/library/import", json={
        "destination_id": dest["id"], "items": items})).json()["id"]))["result"]  # fmt: skip
    assert res["files"] == 2

    report: dict[str, Any] = (await _job(c, (await c.post("/library/check")).json()["id"]))[
        "result"
    ]
    # the daily check may have run first, at start, and hashed them already: either way,
    # once checked every file has its hash and a check after has nothing left to hash
    assert report["files"] == 2 and report["missing"] == [] and report["hashed"] in (0, 2)
    again = (await _job(c, (await c.post("/library/check")).json()["id"]))["result"]
    assert again["hashed"] == 0
    [dup] = report["duplicates"]
    assert sorted(x["title"] for x in dup) == ["Amélie", "Dune"]
    assert report["unknown_count"] == 1 and report["unknown"][0]["path"].endswith("stray.mp4")

    # a file disappears
    (root / "Amélie (2001)/Amelie.2001.720p.BluRay.x264-GRP.mkv").unlink()
    report = (await _job(c, (await c.post("/library/check")).json()["id"]))["result"]
    [gone] = report["missing"]
    assert gone["title"] == "Amélie" and report["duplicates"] == [] and report["hashed"] == 0
    dune_file = next(f["file_id"] for f in dup if f["title"] == "Dune")
    r = await c.post("/library/check/forget", json={"file_ids": [gone["file_id"], dune_file]})
    assert r.json() == {"forgotten": 1}  # Dune's file is still there: kept
    amelie = (await c.get(f"/titles/{gone['title_id']}")).json()
    assert (
        amelie["wanted"][0]["state"] == "missing" and amelie["wanted"][0]["next_search_at"] is None
    )
    assert (await c.get(f"/titles/{gone['title_id']}/files")).json() == []


async def _files_check(c: httpx.AsyncClient) -> dict[str, Any]:
    health = (await c.get("/system/health")).json()
    return next(x for x in health["checks"] if x["name"] == "library_files")


async def test_the_last_check_and_health(c: httpx.AsyncClient, fakes: Fakes) -> None:
    """Two files gone: Health says so; dealt with one at a time, each leaves the list."""
    assert (await c.get("/library/check/latest")).json() is None
    assert (await _files_check(c))["status"] == "ok"
    dest = await _dest(c, "movie")
    root = Path(dest["path"])
    rels = ["Dune (2021) {tmdb-438631}/Dune (2021) Bluray-1080p.mkv",
            "Amélie (2001)/Amelie.2001.720p.BluRay.x264-GRP.mkv"]  # fmt: skip
    for n, rel in enumerate(rels):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(bytes([n + 1]) * 1000)
    profile = await standard_hd(c)
    items = [
        {"folder": "Dune (2021) {tmdb-438631}", "tmdb_id": 438631, "profile_id": profile,
         "files": [rels[0]]},
        {"folder": "Amélie (2001)", "tmdb_id": 194, "profile_id": profile, "files": [rels[1]]},
    ]  # fmt: skip
    await _job(c, (await c.post("/library/import", json={
        "destination_id": dest["id"], "items": items})).json()["id"])  # fmt: skip
    for rel in rels:
        (root / rel).unlink()  # deleted by hand on the NAS
    await _job(c, (await c.post("/library/check")).json()["id"])

    latest = (await c.get("/library/check/latest")).json()
    assert sorted(m["title"] for m in latest["missing"]) == ["Amélie", "Dune"]
    assert latest["checked_at"]
    health = await _files_check(c)
    assert health["status"] == "warning" and health["message"].startswith("2 files missing")
    assert health["fix_url"] == "/settings/library#check"

    dune, amelie = sorted(latest["missing"], key=lambda m: m["title"] != "Dune")
    # Dune: searched for again; Amélie: removed from the library
    assert (await c.post("/library/check/forget", json={"file_ids": [dune["file_id"]]})).json() == {
        "forgotten": 1}  # fmt: skip
    latest = (await c.get("/library/check/latest")).json()
    assert [m["title"] for m in latest["missing"]] == ["Amélie"]
    assert (await _files_check(c))["message"].startswith("1 file missing")
    await c.post("/library/check/forget", json={"file_ids": [amelie["file_id"]]})
    assert (await c.delete(f"/titles/{amelie['title_id']}")).status_code == 204
    assert (await c.get("/library/check/latest")).json()["missing"] == []
    assert (await _files_check(c))["status"] == "ok"
    assert (await c.get(f"/titles/{dune['title_id']}")).json()["wanted"][0]["state"] == "missing"


async def test_the_check_runs_daily(server: Any) -> None:
    scheduled = server.app.state.fb.scheduler.definitions
    assert scheduled["library_check"].job_type == "library.check"


async def test_removing_a_title_forgets_its_files(c: httpx.AsyncClient, fakes: Fakes) -> None:
    """Remove: the title and the records of its files go; the files stay on disk, and the
    check then lists them as not in Fetchbox (they used to linger as records of nothing)."""
    dest = await _dest(c, "movie")
    root = Path(dest["path"])
    rel = "Dune (2021) {tmdb-438631}/Dune (2021) Bluray-1080p.mkv"
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_bytes(b"\1" * 1000)
    profile = await standard_hd(c)
    await _job(c, (await c.post("/library/import", json={"destination_id": dest["id"], "items": [
        {"folder": "Dune (2021) {tmdb-438631}", "tmdb_id": 438631, "profile_id": profile,
         "files": [rel]}]})).json()["id"])  # fmt: skip
    [dune] = (await c.get("/titles")).json()["items"]
    assert (await c.delete(f"/titles/{dune['id']}")).status_code == 204
    report = (await _job(c, (await c.post("/library/check")).json()["id"]))["result"]
    assert report["files"] == 0 and report["missing"] == []
    assert report["unknown_count"] == 1 and report["unknown"][0]["path"].endswith(".mkv")
    assert (root / rel).exists()  # on disk, untouched


async def test_search_again_leaves_a_title_another_file_still_covers(
    c: httpx.AsyncClient, fakes: Fakes, server: Running
) -> None:
    """Forgetting a missing file wants the movie again only when no other file covers it: a
    movie with two copies, one gone, keeps the other and isn't searched."""
    dest = await _dest(c, "movie")
    root = Path(dest["path"])
    folder = "Dune (2021) {tmdb-438631}"
    rels = [f"{folder}/Dune (2021) WEBRip-1080p.mkv", f"{folder}/Dune (2021) WEBDL-1080p.mkv"]
    for i, rel in enumerate(rels):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(bytes([i + 1]) * 300_000)
    items = [{"folder": folder, "tmdb_id": 438631, "profile_id": await standard_hd(c),
              "files": rels[:1]}]  # fmt: skip
    res = (await _job(c, (await c.post("/library/import", json={
        "destination_id": dest["id"], "items": items})).json()["id"]))["result"]  # fmt: skip
    title_id = res["added"][0]["title_id"]
    wanted = (await c.get(f"/titles/{title_id}")).json()["wanted"][0]["id"]
    async with server.app.state.fb.db.sessionmaker() as s:  # a second copy, also its file
        node = (await s.get(Destination, dest["id"])).node_id  # type: ignore[union-attr]
        mf = MediaFile(storage_node_id=node, destination_id=dest["id"], relative_path=rels[1],
                       size=300_000, quality={"name": "WEBDL-1080p"})  # fmt: skip
        s.add(mf)
        await s.flush()
        s.add(MediaFileItem(media_file_id=mf.id, wanted_item_id=wanted))
        await s.commit()
    assert len((await c.get(f"/titles/{title_id}/files")).json()) == 2

    (root / rels[0]).unlink()  # one copy goes
    report = (await _job(c, (await c.post("/library/check")).json()["id"]))["result"]
    [gone] = report["missing"]
    r = await c.post("/library/check/forget", json={"file_ids": [gone["file_id"]]})
    assert r.json() == {"forgotten": 1}
    movie = (await c.get(f"/titles/{title_id}")).json()
    assert movie["wanted"][0]["state"] == "acquired"  # the other copy still covers it
    [left] = (await c.get(f"/titles/{title_id}/files")).json()
    assert left["relative_path"] == rels[1]
