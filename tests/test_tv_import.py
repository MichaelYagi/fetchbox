"""TV import: a season pack, a multi-episode file, a missing episode, episode naming."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from fetchbox.modules.library.service import file_episodes, release_episodes
from fetchbox.modules.releases.parser import parse_release
from fetchbox.providers.download_clients.builtin import BuiltinClient
from tests.conftest import Running
from tests.fake_torznab import Item
from tests.pipeline import Fakes, _indexer, standard_hd
from tests.torrents import Made, digest, make_torrent, seeder, until

PACK = "Severance.S01.1080p.WEB-DL.DDP5.1.H.264-NTb"
KB = 1024


def _files() -> dict[str, int]:
    files = {
        f"Severance.S01E0{n}.1080p.WEB-DL.DDP5.1.H.264-NTb.mkv": (100 + n) * KB
        for n in (1, 2, 3, 4, 7, 8)
    }
    files["Severance.S01E05E06.1080p.WEB-DL.DDP5.1.H.264-NTb.mkv"] = 300 * KB
    files["Sample/severance.s01e01.sample.mkv"] = 20 * KB
    files[f"{PACK}.nfo"] = 1 * KB
    return files  # no E09: it isn't in this pack


@pytest.fixture
def made(tmp_path: Path) -> Made:
    return make_torrent(tmp_path / "seedroot", PACK, _files())


@pytest.fixture
async def seed(made: Made) -> AsyncIterator[BuiltinClient]:
    s = await seeder(made)
    yield s
    await s.stop()


@pytest.fixture(autouse=True)
def fast(server: Running) -> None:
    server.app.state.downloads.poll_interval = 0.1
    server.app.state.library.sweep_interval = 0.2


def test_episode_numbers_from_file_names() -> None:
    assert file_episodes("Pack/Show.S01E05E06.720p.mkv", {}, {}) == [(1, 5), (1, 6)]
    assert file_episodes("S02E03.mkv", {}, {}) == [(2, 3)]
    assert file_episodes("Show - 1x04 - Title.mkv", {}, {}) == [(1, 4)]
    assert file_episodes(
        "Show.2024.05.17.mkv", {parse_release("x.2024.05.17").air_date: (3, 9)}, {}
    ) == [(3, 9)]
    assert file_episodes("[Group] Show - 13 [1080p].mkv", {}, {13: (1, 13)}) == [(1, 13)]
    assert file_episodes("Extras/Behind the scenes.mkv", {}, {}) == []
    assert release_episodes(parse_release("Show.S01E02.720p-G"), {}, {}) == [(1, 2)]


async def test_season_pack_import(
    c: httpx.AsyncClient,
    fakes: Fakes,
    server: Running,
    made: Made,
    seed: BuiltinClient,
    settings: Any,
) -> None:
    await _indexer(c, fakes, "one", [Item(PACK, magnet=made.magnet, infohash=made.info_hash,
                                         size=2_000_000_000, category=5040)])  # fmt: skip
    body = {"kind": "series", "tmdb_id": 95396, "monitor_mode": "all",
            "profile_id": await standard_hd(c), "search": False}  # fmt: skip
    r = await c.post("/titles", json=body)
    t = r.json()
    res = (await c.get("/releases", params={"title_id": t["id"], "season": 1})).json()
    [rel] = [x for x in res["releases"] if x["candidate"]["title"] == PACK]
    assert rel["accepted"], rel["rejections"]
    assert (await c.post("/grabs", json={"release_id": rel["release_id"]})).status_code == 201

    async def downloading() -> bool:
        return any(d["status"] == "downloading" for d in (await c.get("/downloads")).json())

    await until(downloading, within=20)
    engine = next(x for x in server.app.state.downloads.clients.values()
                  if isinstance(x, BuiltinClient))  # fmt: skip
    engine.connect_peer(made.info_hash, "127.0.0.1", seed.listen_port or 0)

    async def imported() -> bool:
        # A single loopback connect sometimes fails on Windows: keep offering the seeder.
        engine.connect_peer(made.info_hash, "127.0.0.1", seed.listen_port or 0)
        imports = (await c.get("/imports")).json()
        return bool(imports) and imports[0]["status"] == "imported"

    await until(imported, within=90)
    seasons = (await c.get(f"/titles/{t['id']}/episodes")).json()
    s1 = {e["number"]: e["wanted"] for e in seasons[0]["episodes"]}
    assert all(s1[n]["state"] == "acquired" and s1[n]["lock"] == "acquired" for n in range(1, 9))
    assert s1[9]["state"] == "missing" and s1[9]["next_search_at"] is None  # search again

    files = {f["relative_path"]: f for f in (await c.get(f"/titles/{t['id']}/files")).json()}
    base = "Severance (2022) {tvdb-371980}/Season 01/"
    assert sorted(files) == sorted(
        [
            base + "Severance - S01E01 - Ep 1 [WEBDL-1080p].mkv",
            base + "Severance - S01E02 - Ep 2 [WEBDL-1080p].mkv",
            base + "Severance - S01E03 - Ep 3 [WEBDL-1080p].mkv",
            base + "Severance - S01E04 - Ep 4 [WEBDL-1080p].mkv",
            base + "Severance - S01E05-E06 - Ep 5 + Ep 6 [WEBDL-1080p].mkv",
            base + "Severance - S01E07 - Ep 7 [WEBDL-1080p].mkv",
            base + "Severance - S01E08 - Ep 8 [WEBDL-1080p].mkv",
        ]
    )
    multi = files[base + "Severance - S01E05-E06 - Ep 5 + Ep 6 [WEBDL-1080p].mkv"]
    assert sorted(multi["wanted_item_ids"]) == sorted([s1[5]["id"], s1[6]["id"]])
    library = settings.downloads_dir / "TV"
    placed = library / base / "Severance - S01E03 - Ep 3 [WEBDL-1080p].mkv"
    assert digest(placed.read_bytes()) == digest(
        made.payload["Severance.S01E03.1080p.WEB-DL.DDP5.1.H.264-NTb.mkv"]
    )
    # the pack's episodes were filed in place: nothing of them is left in .incomplete,
    # and only the extras (sample, .nfo) wait there, out of sight
    left = [p.name for p in (library / ".incomplete").rglob("*") if p.is_file()]
    assert not [n for n in left if n.endswith(".mkv") and "sample" not in n.lower()], left
    visible = [p for p in library.rglob("*") if p.is_file() and ".incomplete" not in p.parts]
    assert not [p for p in visible if "sample" in p.name.lower() or p.suffix == ".nfo"]
    hist = (await c.get("/history", params={"type": "failed"})).json()["items"]
    assert [h["data"]["reason"] for h in hist] == ["not in the download"]


async def test_replacing_one_episode_of_a_multi_episode_file(
    c: httpx.AsyncClient, fakes: Fakes, server: Running, settings: Any
) -> None:
    """An upgrade for E05 alone must not delete the old E05E06 file: E06 still needs it."""
    from types import SimpleNamespace

    from sqlalchemy import select

    from fetchbox.modules.library.models import MediaFile, MediaFileItem
    from fetchbox.modules.releases.parser import parse_release

    body = {"kind": "series", "tmdb_id": 95396, "monitor_mode": "all",
            "profile_id": await standard_hd(c), "search": False}  # fmt: skip
    t = (await c.post("/titles", json=body)).json()
    seasons = (await c.get(f"/titles/{t['id']}/episodes")).json()
    e5, e6 = (seasons[0]["episodes"][4]["wanted"]["id"], seasons[0]["episodes"][5]["wanted"]["id"])
    lib = server.app.state.library
    dest = await server.app.state.storage.default_destination("series")
    old_rel = "Severance/Season 01/Severance - S01E05-E06.mkv"
    old_path = Path(dest.path) / old_rel
    old_path.parent.mkdir(parents=True, exist_ok=True)
    old_path.write_bytes(b"old")
    async with server.app.state.fb.db.sessionmaker() as s:
        mf = MediaFile(storage_node_id=dest.node_id, destination_id=dest.id,
                       relative_path=old_rel, size=3, quality={"name": "HDTV-720p"})  # fmt: skip
        s.add(mf)
        await s.flush()
        s.add_all([MediaFileItem(media_file_id=mf.id, wanted_item_id=w) for w in (e5, e6)])
        await s.commit()
        old_id = mf.id
    new_rel = "Severance/Season 01/Severance - S01E05.mkv"
    new_path = Path(dest.path) / new_rel
    new_path.write_bytes(b"new")
    item5 = await server.app.state.media.get_wanted(e5)
    parsed = parse_release("Severance.S01E05.1080p.WEB-DL-NTb")
    await lib._record(
        SimpleNamespace(id="d"), SimpleNamespace(release_title="x"), await
        server.app.state.media.get_title(t["id"]), dest, [item5], new_rel, new_path,
        "hardlink", parsed, "WEBDL-1080p", 0, None, "x.mkv",
    )  # fmt: skip
    assert old_path.exists()  # still E06's file
    async with server.app.state.fb.db.sessionmaker() as s:
        q = select(MediaFileItem.wanted_item_id).where(MediaFileItem.media_file_id == old_id)
        serves = set((await s.execute(q)).scalars())
        assert serves == {e6}
        assert await s.get(MediaFile, old_id) is not None
