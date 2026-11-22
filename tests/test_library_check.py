"""Library check: missing files, duplicates and unknown files; forgetting missing ones."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

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
    assert report["files"] == 2 and report["missing"] == [] and report["hashed"] == 2
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
