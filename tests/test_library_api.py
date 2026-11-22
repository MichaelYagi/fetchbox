"""Import, files, destinations and naming, end to end with a real torrent.

The first test is the Phase 1 gate in miniature: with an indexer set up, adding a movie
is all it takes (search on add, grab, download, import) for a correctly named file to
appear under Downloads/Fetchbox/Movies.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from fetchbox.modules.library.naming import DEFAULT_PATTERNS, movie_path, parse, render
from fetchbox.modules.library.service import pick_video
from fetchbox.providers.download_clients import ClientFile, DownloadClientError
from fetchbox.providers.download_clients.builtin import BuiltinClient
from fetchbox.providers.storage.local import LocalStorage, cleanup_partials
from tests.conftest import Running
from tests.fake_torznab import Item
from tests.pipeline import Fakes, _indexer, _movie
from tests.torrents import Made, digest, make_torrent, seeder, until

NAME = "Dune.2021.1080p.WEB-DL.DDP5.1.Atmos.H.264-FLUX"
MB = 1024 * 1024
EXPECTED = "Dune (2021) {tmdb-438631}/Dune (2021) - WEBDL-1080p x264-FLUX.mkv"


@pytest.fixture
async def made(tmp_path: Path) -> Made:
    return make_torrent(
        tmp_path / "seedroot",
        NAME,
        {
            f"{NAME}.mkv": 3 * MB,
            "Sample/dune.sample.mkv": MB // 2,
            f"{NAME}.nfo": 80,
        },
    )


@pytest.fixture
async def seed(made: Made) -> AsyncIterator[BuiltinClient]:
    s = await seeder(made)
    yield s
    await s.stop()


@pytest.fixture(autouse=True)
def fast(server: Running) -> None:
    server.app.state.downloads.poll_interval = 0.1
    server.app.state.library.sweep_interval = 0.2
    server.app.state.autosearch.sweep_interval = 0.2


def _engine(server: Running) -> BuiltinClient:
    return next(
        c for c in server.app.state.downloads.clients.values() if isinstance(c, BuiltinClient)
    )


async def _acquire(
    c: httpx.AsyncClient,
    fakes: Fakes,
    server: Running,
    made: Made,
    seed: BuiltinClient,
    *,
    automatic: bool = False,
) -> dict[str, Any]:
    """Grab ``made`` and wait until it's imported. ``automatic``: no search or grab by
    hand, adding the movie is enough (search on add)."""
    await _indexer(c, fakes, "one", [Item(NAME, magnet=made.magnet, infohash=made.info_hash)])
    movie = await _movie(c, search=automatic)
    if not automatic:
        wid = movie["wanted"][0]["id"]
        rel = (await c.get("/releases", params={"wanted_item_id": wid})).json()["releases"][0]
        assert rel["accepted"], rel["rejections"]
        assert (await c.post("/grabs", json={"release_id": rel["release_id"]})).status_code == 201

    async def downloading() -> bool:
        return any(d["status"] == "downloading" for d in (await c.get("/downloads")).json())

    await until(downloading, within=20)
    _engine(server).connect_peer(made.info_hash, "127.0.0.1", seed.listen_port or 0)

    async def acquired() -> bool:
        # A single loopback connect sometimes fails on Windows: keep offering the seeder.
        _engine(server).connect_peer(made.info_hash, "127.0.0.1", seed.listen_port or 0)
        t = (await c.get(f"/titles/{movie['id']}")).json()
        return bool(t["wanted"][0]["state"] == "acquired")

    await until(acquired, within=60)
    return movie


async def test_gate_add_movie_get_named_file(
    c: httpx.AsyncClient,
    fakes: Fakes,
    server: Running,
    made: Made,
    seed: BuiltinClient,
    settings: Any,
) -> None:
    movie = await _acquire(c, fakes, server, made, seed, automatic=True)
    library = settings.downloads_dir / "Movies"
    placed = library / EXPECTED
    assert placed.is_file(), sorted(str(p) for p in library.rglob("*"))
    assert digest(placed.read_bytes()) == digest(made.payload[f"{NAME}.mkv"])
    assert not list(library.rglob("*sample*")) and not list(library.rglob("*.nfo"))
    assert not list(library.rglob("*.fetchbox-partial"))

    # hardlinked from the torrent folder (same filesystem): seeding continues, no extra space
    torrent_copy = settings.downloads_dir / "Torrents" / "fetchbox-movies" / NAME / f"{NAME}.mkv"
    assert os.stat(placed).st_ino == os.stat(torrent_copy).st_ino

    wanted = (await c.get(f"/titles/{movie['id']}")).json()["wanted"][0]
    assert wanted["state"] == "acquired" and wanted["lock"] == "acquired"
    assert wanted["cutoff_met"] is True  # WEB 1080p is the Standard HD cutoff

    [f] = (await c.get(f"/titles/{movie['id']}/files")).json()
    assert f["relative_path"] == EXPECTED and f["size"] == 3 * MB and f["link_mode"] == "hardlink"
    assert f["quality"]["name"] == "WEBDL-1080p" and f["release_group"] == "FLUX"
    assert f["custom_format_score"] == 5 and f["wanted_item_ids"] == [wanted["id"]]

    [imp] = (await c.get("/imports")).json()
    assert imp["status"] == "imported" and imp["imported_at"]
    # the queue says where the library copy is, and that it's a hardlink
    [dl] = (await c.get("/downloads")).json()
    assert dl["imported_to"] == str(placed) and dl["imported_as"] == "hardlink"
    hist = [h["type"] for h in (await c.get(f"/titles/{movie['id']}/history")).json()["items"]]
    assert hist == ["imported", "grabbed"]

    # Deleting the file recycles it; the item stays locked (no automatic re-grab).
    assert (await c.delete(f"/files/{f['id']}")).status_code == 204
    assert not placed.exists()
    assert list((library / ".fetchbox-recycle").rglob("*.mkv"))
    wanted = (await c.get(f"/titles/{movie['id']}")).json()["wanted"][0]
    assert wanted["lock"] == "acquired"

    # Download again (the file was moved or deleted since): wanted again and searched,
    # nothing blocklisted
    r = await c.post(f"/downloads/{dl['id']}/download-again")
    assert r.status_code == 200 and r.json() == {"searching": 1, "skipped": []}, r.text
    again = (await c.get(f"/titles/{movie['id']}")).json()["wanted"][0]
    # wanted again: the search it queued may already have grabbed a release
    assert again["lock"] is None
    assert again["state"] in ("missing", "searching", "grabbed", "downloading")
    assert (await c.get("/blocklist")).json() == []


async def test_offline_destination_needs_attention_then_recovers(
    c: httpx.AsyncClient,
    fakes: Fakes,
    server: Running,
    made: Made,
    seed: BuiltinClient,
    tmp_path: Path,
) -> None:
    movies = next(d for d in (await c.get("/destinations")).json() if d["media_kind"] == "movie")
    elsewhere = tmp_path / "Library"
    r = await c.patch(
        f"/destinations/{movies['id']}", json={"path": str(elsewhere), "is_default": False}
    )
    assert r.status_code == 200 and r.json()["status"] == "offline"  # folder doesn't exist
    # (a non-default destination isn't created for you: it may be an unmounted share)
    assert "does not exist" in r.json()["status_message"]

    movie = await _movie(c)
    await _indexer(c, fakes, "one", [Item(NAME, magnet=made.magnet, infohash=made.info_hash)])
    wid = movie["wanted"][0]["id"]
    rel = (await c.get("/releases", params={"wanted_item_id": wid})).json()["releases"][0]
    await c.post("/grabs", json={"release_id": rel["release_id"]})

    async def downloading() -> bool:
        return any(d["status"] == "downloading" for d in (await c.get("/downloads")).json())

    await until(downloading, within=20)
    _engine(server).connect_peer(made.info_hash, "127.0.0.1", seed.listen_port or 0)

    async def attention() -> bool:
        imps = (await c.get("/imports")).json()
        return bool(imps) and imps[0]["status"] == "needs_attention"

    await until(attention, within=40)
    imp = (await c.get("/imports")).json()[0]
    assert "offline" in imp["error"] and imp["retry_at"]
    assert (await c.get(f"/titles/{movie['id']}")).json()["wanted"][0]["state"] == "downloading"

    elsewhere.mkdir()  # the share comes back
    assert (await c.post(f"/destinations/{movies['id']}/check")).json()["status"] == "online"
    assert (await c.post(f"/imports/{imp['download_id']}/retry")).status_code == 202

    async def acquired() -> bool:
        return (await c.get(f"/titles/{movie['id']}")).json()["wanted"][0]["state"] == "acquired"

    await until(acquired, within=20)
    assert (elsewhere / EXPECTED).is_file()


async def test_destinations_and_health(c: httpx.AsyncClient, settings: Any, tmp_path: Path) -> None:
    dests = (await c.get("/destinations")).json()
    assert {(d["name"], d["media_kind"], d["is_default"]) for d in dests} == {
        ("Movies", "movie", True),
        ("TV", "series", True),
    }
    assert {Path(d["path"]) for d in dests} == {
        settings.downloads_dir / "Movies",
        settings.downloads_dir / "TV",
    }
    [node] = (await c.get("/storage/nodes")).json()
    assert node["kind"] == "local"

    health = (await c.get("/system/health")).json()
    storage = next(x for x in health["checks"] if x["name"] == "storage")
    assert storage["status"] in ("ok", "warning"), storage  # warning only if the test disk is full

    extra = tmp_path / "Movies4K"
    extra.mkdir()
    r = await c.post(
        "/destinations",
        json={
            "name": "4K",
            "path": str(extra),
            "media_kind": "movie",
            "is_default": True,
            "link_mode": "copy",
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["status"] in ("online", "low_space")
    movies = [d for d in (await c.get("/destinations", params={"media_kind": "movie"})).json()]
    assert [d["name"] for d in movies if d["is_default"]] == ["4K"]  # only one default
    assert (
        await c.post(
            "/destinations", json={"name": "x", "path": "relative/path", "media_kind": "movie"}
        )
    ).status_code == 422
    assert (
        await c.post(
            "/destinations", json={"name": "4K", "path": str(extra), "media_kind": "movie"}
        )
    ).status_code == 409
    assert (await c.delete(f"/destinations/{r.json()['id']}")).status_code == 204


async def test_naming_templates_api(c: httpx.AsyncClient) -> None:
    [tpl] = (await c.get("/naming-templates")).json()
    assert tpl["patterns"] == DEFAULT_PATTERNS
    r = await c.post("/naming-templates/preview", json={})
    assert r.json()["movie"] == (
        "Dune - Part Two (2024) {tmdb-693134}/"
        "Dune - Part Two (2024) - WEBDL-2160p DV HDR10 x265-FLUX.mkv"
    )
    assert r.json()["episode"] == (
        "Severance (2022) {tvdb-371980}/Season 01/"
        "Severance - S01E07 - Defiant Jazz [WEBDL-1080p].mkv"
    )
    r = await c.post(
        "/naming-templates/preview", json={"patterns": {"movie_file": "{Title}.{Year}.{Quality}"}}
    )
    assert r.json()["movie"].endswith("/Dune - Part Two.2024.WEBDL-2160p.mkv")
    bad = await c.post("/naming-templates/preview", json={"patterns": {"movie_file": "{Nope}"}})
    assert bad.status_code == 422 and "unknown token" in bad.json()["errors"][0]["message"]

    r = await c.post(
        "/naming-templates", json={"name": "Dots", "patterns": {"movie_file": "{Title}.{Year}"}}
    )
    assert (
        r.status_code == 201
        and r.json()["patterns"]["movie_folder"] == DEFAULT_PATTERNS["movie_folder"]
    )
    r = await c.patch(f"/naming-templates/{r.json()['id']}", json={"patterns": {"movie_file": "["}})
    assert r.status_code == 422
    assert (await c.post("/naming-templates", json={"name": "Dots"})).status_code == 409


# ---- units ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("template", "ctx", "out"),
    [
        ("{Title} ({Year})", {"Title": "Alien", "Year": 1979}, "Alien (1979)"),
        ("{Title} ({Year})", {"Title": "Alien"}, "Alien"),  # empty () removed
        ("S{Season:00}E{Episode:00}[-E{EpisodeEnd:00}]", {"Season": 1, "Episode": 2}, "S01E02"),
        (
            "S{Season:00}E{Episode:00}[-E{EpisodeEnd:00}]",
            {"Season": 1, "Episode": 2, "EpisodeEnd": 3},
            "S01E02-E03",
        ),
        ("{Title} - {Group}", {"Title": "X"}, "X"),  # dangling separator trimmed
        ("[[{Quality}]]", {"Quality": "WEBDL-1080p"}, "[WEBDL-1080p]"),
        ("{{tmdb-{TmdbId}}}", {"TmdbId": 1}, "{tmdb-1}"),
        ("{HDR}", {"HDR": ["DV", "HDR10"]}, "DV HDR10"),
    ],
)
def test_render(template: str, ctx: dict[str, Any], out: str) -> None:
    assert render(template, ctx) == out


@pytest.mark.parametrize(
    "bad", ["{Nope}", "{Title", "x}", "[a", "a]", "[[x]", "{Season:x}", "[a[b]]"]
)
def test_parse_errors(bad: str) -> None:
    from fetchbox.modules.library.naming import TemplateError

    with pytest.raises(TemplateError):
        parse(bad)


def test_movie_path_is_windows_safe() -> None:
    ctx = {"Title": 'What? "If"/Then: <Now>*|', "Year": 2020, "TmdbId": 1, "Quality": "CON"}
    path = movie_path({**DEFAULT_PATTERNS, "movie_file": "{Quality}"}, ctx, ".MKV")
    folder, file = path.split("/")
    assert folder == "What If-Then - Now (2020) {tmdb-1}" and file == "CON_.mkv"


def test_pick_video_skips_samples_and_extras() -> None:
    files = [
        ClientFile(path="M/movie.mkv", size=900, progress=1),
        ClientFile(path="M/sample.mkv", size=950, progress=1),
        ClientFile(path="M/Sample/clip.mkv", size=990, progress=1),
        ClientFile(path="M/m.nfo", size=5000, progress=1),
        ClientFile(path="M/extras.mp4", size=100, progress=1),
    ]
    pick = pick_video(files)
    assert pick is not None and pick.path == "M/movie.mkv"
    assert pick_video([ClientFile(path="a.nfo", size=1, progress=1)]) is None
    assert pick_video([ClientFile(path="The.Sampler.2020.mkv", size=1, progress=1)]) is not None


async def test_local_storage_two_phase_ops(tmp_path: Path) -> None:
    s = LocalStorage(LocalStorage.Settings())
    src = tmp_path / "src.bin"
    src.write_bytes(os.urandom(10 * MB))
    await s.copy(str(src), str(tmp_path / "a" / "copy.bin"))
    assert (tmp_path / "a" / "copy.bin").read_bytes() == src.read_bytes()
    await s.hardlink(str(src), str(tmp_path / "b" / "link.bin"))
    assert os.stat(tmp_path / "b" / "link.bin").st_ino == os.stat(src).st_ino
    recycled = await s.delete(str(tmp_path / "a" / "copy.bin"), recycle_dir=str(tmp_path / "bin"))
    assert recycled and Path(recycled).exists() and not (tmp_path / "a" / "copy.bin").exists()
    (tmp_path / "b" / "half.bin.fetchbox-partial").write_bytes(b"x")
    assert cleanup_partials(tmp_path) == [str(tmp_path / "b" / "half.bin.fetchbox-partial")]
    report = await s.check_access(str(tmp_path / "b"), link_source=str(tmp_path))
    assert report.writable and report.deletable and report.hardlink_with and report.same_fs_as
    assert not (await s.check_access(str(tmp_path / "nope"))).exists


async def _job_done(c: httpx.AsyncClient, job_id: str) -> dict[str, Any]:
    async def done() -> bool:
        return (await c.get(f"/jobs/{job_id}")).json()["status"] in ("succeeded", "failed")

    await until(done, within=20)
    out: dict[str, Any] = (await c.get(f"/jobs/{job_id}")).json()
    return out


async def test_unlock_then_redownload(
    c: httpx.AsyncClient,
    fakes: Fakes,
    server: Running,
    made: Made,
    seed: BuiltinClient,
    settings: Any,
) -> None:
    movie = await _acquire(c, fakes, server, made, seed)
    wid = movie["wanted"][0]["id"]
    library = settings.downloads_dir / "Movies"

    # unlock, file kept: the search only takes a real upgrade, and the one release isn't
    r = await c.post(f"/wanted/{wid}/unlock")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["wanted"]["lock"] is None and body["wanted"]["state"] == "acquired"
    assert r.headers["ETag"] == body["wanted"]["etag"]
    job = await _job_done(c, body["search_job_id"])
    assert job["status"] == "succeeded" and job["result"]["mode"] == "upgrade"
    assert job["result"]["accepted"] == 0 and "grabbed" not in job["result"]
    assert (library / EXPECTED).is_file()

    # redownload: blocklist the release, recycle the file, missing again
    r = await c.post(f"/wanted/{wid}/redownload")
    assert r.status_code == 200, r.text
    w = r.json()["wanted"]
    assert w["state"] == "missing" and w["lock"] is None and w["cutoff_met"] is False
    assert not (library / EXPECTED).exists()
    assert list((library / ".fetchbox-recycle").rglob("*.mkv"))
    assert (await c.get(f"/titles/{movie['id']}/files")).json() == []
    [b] = (await c.get("/blocklist")).json()
    assert b["release_title"] == NAME and b["reason"] == "redownload"
    job = await _job_done(c, r.json()["search_job_id"])
    assert job["result"]["accepted"] == 0  # the only release is blocklisted now
    assert len((await c.get("/grabs", params={"wanted_item_id": wid})).json()) == 1

    hist = [h["type"] for h in (await c.get(f"/titles/{movie['id']}/history")).json()["items"]]
    assert hist == ["unlocked", "deleted", "unlocked", "imported", "grabbed"]

    async def logged() -> bool:  # the activity log is written asynchronously
        acts = (await c.get("/activity", params={"limit": 50})).json()["items"]
        msgs = [a["message"] for a in acts]
        return "Redownloading Dune" in msgs and any(m.startswith("Unlocked Dune") for m in msgs)

    await until(logged, within=30)  # slow Windows runners

    assert (await c.post("/wanted/nope/unlock")).status_code == 404
    assert (await c.post("/wanted/nope/redownload")).status_code == 404


async def test_redownload_keeps_the_file_until_the_new_import(
    c: httpx.AsyncClient,
    fakes: Fakes,
    server: Running,
    made: Made,
    seed: BuiltinClient,
    settings: Any,
) -> None:
    movie = await _acquire(c, fakes, server, made, seed)
    wid = movie["wanted"][0]["id"]
    r = await c.post(f"/wanted/{wid}/redownload", json={"blocklist": False, "delete_file": False})
    assert r.status_code == 200, r.text
    assert r.json()["wanted"]["state"] == "missing"
    assert (settings.downloads_dir / "Movies" / EXPECTED).is_file()
    assert len((await c.get(f"/titles/{movie['id']}/files")).json()) == 1
    assert (await c.get("/blocklist")).json() == []
    # not blocklisted: the same release may be grabbed again (say the file was corrupt)
    job = await _job_done(c, r.json()["search_job_id"])
    assert job["result"]["mode"] == "missing" and job["result"]["grabbed"], job
    # The torrent is still seeding from the first download: that download is reused,
    # verified again and imported again (the file is already in place).

    async def reimported() -> bool:
        hist = (await c.get(f"/titles/{movie['id']}/history")).json()["items"]
        return [h["type"] for h in hist][:3] == ["imported", "grabbed", "unlocked"]

    await until(reimported, within=90)  # verified again: slow on Windows runners
    [f] = (await c.get(f"/titles/{movie['id']}/files")).json()
    assert f["relative_path"] == EXPECTED
    w = (await c.get(f"/titles/{movie['id']}")).json()["wanted"][0]
    assert w["state"] == "acquired" and w["lock"] == "acquired"


async def test_unlock_a_busy_or_fileless_item(
    c: httpx.AsyncClient, fakes: Fakes, server: Running
) -> None:
    movie = await _movie(c)
    wid = movie["wanted"][0]["id"]
    await _indexer(c, fakes, "one", [Item(NAME, infohash="f" * 40, link="http://one/dl/0")])
    rel = (await c.get("/releases", params={"wanted_item_id": wid})).json()["releases"][0]
    assert (await c.post("/grabs", json={"release_id": rel["release_id"]})).status_code == 201
    r = await c.post(f"/wanted/{wid}/unlock")
    assert r.status_code == 409, r.text
    assert (await c.post(f"/wanted/{wid}/redownload")).status_code == 409


@pytest.mark.parametrize("action", ["remove", "pause"])
async def test_seeding_goal_stops_the_torrent_after_import(
    c: httpx.AsyncClient,
    fakes: Fakes,
    server: Running,
    made: Made,
    seed: BuiltinClient,
    settings: Any,
    action: str,
) -> None:
    await _acquire(c, fakes, server, made, seed)
    client = (await c.get("/download-clients")).json()[0]
    sub = server.app.state.fb.bus.subscribe("download.seeded")
    r = await c.patch(
        f"/download-clients/{client['id']}",
        json={"policy": {"seed_minutes": 0, "seed_action": action}},
    )
    assert r.status_code == 200, r.text
    ev = await asyncio.wait_for(sub.queue.get(), 20)
    assert ev.data["action"] == action and ev.data["deleted_data"] is (action == "remove")

    [d] = (await c.get("/downloads")).json()
    assert d["status"] == "completed" and d["seeded_at"]
    torrent_copy = settings.downloads_dir / "Torrents" / "fetchbox-movies" / NAME / f"{NAME}.mkv"
    library_file = settings.downloads_dir / "Movies" / EXPECTED
    assert library_file.is_file()  # the library's own link survives
    engine = _engine(server)
    if action == "remove":
        assert not torrent_copy.exists() and await engine.status() == []
    else:
        assert torrent_copy.exists()
        [t] = await engine.status()
        assert t.state == "paused"

    async def logged() -> bool:
        acts = (await c.get("/activity", params={"limit": 50})).json()["items"]
        return any(a["message"].startswith(f"Finished seeding {NAME}") for a in acts)

    await until(logged, within=10)


async def test_stop_seeding_keeps_files_that_were_not_imported(
    c: httpx.AsyncClient, server: Running, settings: Any
) -> None:
    """Removing a finished download with delete_data only deletes once it was imported;
    before that its data is the only copy."""
    from sqlalchemy import update

    from fetchbox.core.events import Actor
    from fetchbox.modules.downloads.models import Download
    from fetchbox.modules.releases.models import Grab, GrabItem

    svc = server.app.state.downloads
    builtin = next(x for x in (await c.get("/download-clients")).json() if x["kind"] == "builtin")
    calls: list[bool] = []

    class Spy:
        async def remove(self, ref: str, delete_data: bool) -> None:
            calls.append(delete_data)

    movie = await _movie(c)
    async with server.app.state.fb.db.sessionmaker() as s:
        g = Grab(release={}, release_title="X", reason="manual", actor="t")
        s.add(g)
        await s.flush()
        s.add(GrabItem(grab_id=g.id, wanted_item_id=movie["wanted"][0]["id"]))
        d = Download(
            grab_id=g.id,
            client_id=builtin["id"],
            client_ref="a" * 40,
            name="X",
            media_kind="movie",
            status="completed",
            import_status="needs_attention",
        )
        s.add(d)
        await s.commit()
        did = d.id
    real = svc.clients[builtin["id"]]
    svc.clients[builtin["id"]] = Spy()
    try:
        await svc.remove(did, delete_data=True, blocklist=False, actor=Actor())
        assert calls == [False]  # not imported: data kept
        async with server.app.state.fb.db.sessionmaker() as s:
            await s.execute(update(Download).values(status="completed", import_status="imported"))
            await s.commit()
        await svc.remove(did, delete_data=True, blocklist=False, actor=Actor())
        assert calls == [False, True]  # imported (hardlink/copy): the torrent's copy may go
    finally:
        svc.clients[builtin["id"]] = real


async def test_removed_and_readded_title_imports_again(
    c: httpx.AsyncClient, fakes: Fakes, server: Running, made: Made, seed: BuiltinClient,
    settings: Any,
) -> None:  # fmt: skip
    """Removing a title keeps its files and used to leave their records behind; the same
    release imported again for the re-added title then crashed on the old record, and
    the item stayed "downloading" for good."""
    movie = await _acquire(c, fakes, server, made, seed)
    [first] = (await c.get(f"/titles/{movie['id']}/files")).json()
    [dl] = (await c.get("/downloads")).json()
    assert (await c.delete(f"/downloads/{dl['id']}")).status_code == 204  # keep its data
    assert (await c.delete(f"/titles/{movie['id']}")).status_code == 204

    again = await _movie(c)
    wid = again["wanted"][0]["id"]
    rel = (await c.get("/releases", params={"wanted_item_id": wid})).json()["releases"][0]
    assert (await c.post("/grabs", json={"release_id": rel["release_id"]})).status_code == 201

    async def acquired() -> bool:
        with contextlib.suppress(DownloadClientError):  # not in the engine yet
            _engine(server).connect_peer(made.info_hash, "127.0.0.1", seed.listen_port or 0)
        t = (await c.get(f"/titles/{again['id']}")).json()
        return bool(t["wanted"][0]["state"] == "acquired")

    await until(acquired, within=60)
    [f] = (await c.get(f"/titles/{again['id']}/files")).json()
    assert f["relative_path"] == first["relative_path"]
    assert f["wanted_item_ids"] == [wid]


async def test_an_unexpected_import_error_is_shown_not_stuck(
    c: httpx.AsyncClient, fakes: Fakes, server: Running, made: Made, seed: BuiltinClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    async def boom(d: Any) -> Any:
        raise RuntimeError("disk exploded")

    monkeypatch.setattr(server.app.state.library, "_import", boom)
    await _indexer(c, fakes, "one", [Item(NAME, magnet=made.magnet, infohash=made.info_hash)])
    movie = await _movie(c)
    wid = movie["wanted"][0]["id"]
    rel = (await c.get("/releases", params={"wanted_item_id": wid})).json()["releases"][0]
    assert (await c.post("/grabs", json={"release_id": rel["release_id"]})).status_code == 201

    async def flagged() -> bool:
        with contextlib.suppress(DownloadClientError):  # not in the engine yet
            _engine(server).connect_peer(made.info_hash, "127.0.0.1", seed.listen_port or 0)
        return any(i["status"] == "needs_attention" for i in (await c.get("/imports")).json())

    await until(flagged, within=60)
    [imp] = (await c.get("/imports")).json()
    assert "unexpectedly" in imp["error"] and "disk exploded" in imp["error"]

    # removing it now gives the movie back to "wanted", not "downloading" forever
    [dl] = (await c.get("/downloads")).json()
    assert (await c.delete(f"/downloads/{dl['id']}")).status_code == 204
    w = (await c.get(f"/titles/{movie['id']}")).json()["wanted"][0]
    assert w["state"] == "missing"


async def test_stuck_items_are_repaired_at_startup(c: httpx.AsyncClient, server: Running) -> None:
    from sqlalchemy import update

    from fetchbox.modules.media.models import WantedItem

    movie = await _movie(c)
    wid = movie["wanted"][0]["id"]
    async with server.app.state.fb.db.sessionmaker() as s:  # what a crash left behind
        await s.execute(update(WantedItem).where(WantedItem.id == wid).values(state="downloading"))
        await s.commit()
    assert await server.app.state.downloads.repair_stuck() == 1
    w = (await c.get(f"/titles/{movie['id']}")).json()["wanted"][0]
    assert w["state"] == "missing"
