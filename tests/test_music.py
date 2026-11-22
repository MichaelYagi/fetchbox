"""Music: artists, albums and editions, on MusicBrainz responses recorded from the real
service (Portishead; every album answers with Dummy's editions)."""

from __future__ import annotations

import contextlib
import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from fetchbox.modules.media.music import wanted_flags
from fetchbox.providers.metadata.musicbrainz import AlbumResult
from tests.conftest import Running
from tests.fake_torznab import Item
from tests.pipeline import Fakes, _indexer
from tests.test_music_files import AUDIO
from tests.test_musicbrainz import ARTIST, FIX, fake_mb
from tests.torrents import Made, seeder, until


def _album(
    id_: str, kind_type: str, released: date | None, secondary: list[str] | None = None
) -> AlbumResult:
    return AlbumResult(
        id=id_,
        title=id_,
        primary_type=kind_type,
        secondary_types=secondary or [],
        release_date=released,
    )


def test_monitor_modes() -> None:
    today = date(2026, 11, 22)
    albums = [
        _album("old", "Album", date(1994, 8, 22)),
        _album("newest", "Album", date(2008, 4, 28)),
        _album("coming", "Album", date(2027, 3, 1)),
        _album("single", "Single", date(2008, 3, 1)),
        _album("live", "Album", date(2002, 1, 1), ["Live"]),
    ]

    def on(mode: str, kinds: list[str] | None = None) -> set[str]:
        return {k for k, v in wanted_flags(mode, kinds or ["album"], albums, today).items() if v}

    assert on("all") == {"old", "newest", "coming"}
    assert on("future") == {"coming"}
    assert on("latest") == {"newest", "coming"}
    assert on("none") == set()
    assert on("all", ["album", "single", "live"]) == {"old", "newest", "coming", "single", "live"}


async def test_add_an_artist(c: httpx.AsyncClient, server: Running) -> None:
    music = server.app.state.music
    music.transport, music.interval = fake_mb(), 0
    [hit] = (await c.get("/music/search", params={"q": "portishead"})).json()
    assert hit["mbid"] == ARTIST and hit["in_library"] is None

    video = (await c.get("/quality-profiles", params={"media": "video"})).json()[0]["id"]
    r = await c.post("/music/artists", json={"mbid": ARTIST, "profile_id": video})
    assert r.status_code == 422 and "video profile" in r.text
    profile = next(p["id"] for p in (await c.get("/quality-profiles")).json()
                   if p["name"] == "Lossless")  # fmt: skip
    r = await c.post("/music/artists", json={"mbid": ARTIST, "profile_id": profile})
    assert r.status_code == 201, r.text
    artist = r.json()
    assert artist["kind"] == "music" and artist["name"] == "Portishead" and artist["year"] == 1991
    assert artist["albums"] == {"albums": 91, "monitored": 3, "acquired": 0, "missing": 3}
    again = await c.post("/music/artists", json={"mbid": ARTIST, "profile_id": profile})
    assert again.status_code == 409
    hit = (await c.get("/music/search", params={"q": "portishead"})).json()[0]
    assert hit["in_library"] == artist["id"]

    await music.fill_editions()  # the background job, run now
    albums = (await c.get(f"/titles/{artist['id']}/albums")).json()
    wanted = {a["name"]: a for a in albums if a["wanted"]["monitored"]}
    assert set(wanted) == {"Dummy", "Portishead", "Third"}
    dummy = wanted["Dummy"]
    assert dummy["kind"] == "album" and dummy["editions"] == 19
    assert len(dummy["tracks"]) == 11 and dummy["tracks"][0]["name"] == "Mysterons"
    assert dummy["cover_url"].startswith("https://coverartarchive.org/release-group/")
    assert dummy["wanted"]["state"] == "missing" and dummy["wanted"]["album_id"] == dummy["id"]
    assert not next(a for a in albums if a["name"] == "Glory Box")["tracks"]  # not wanted

    # the 10-track edition instead
    eds = (await c.get(f"/albums/{dummy['id']}/editions")).json()
    ten = next(e for e in eds if e["tracks"] == 10)
    r = await c.put(f"/albums/{dummy['id']}/edition", json={"edition_mbid": ten["mbid"]})
    assert r.status_code == 204, r.text
    dummy = next(
        a for a in (await c.get(f"/titles/{artist['id']}/albums")).json() if a["id"] == dummy["id"]
    )
    assert dummy["edition_mbid"] == ten["mbid"] and len(dummy["tracks"]) == 10

    cal = (await c.get("/calendar", params={"start": "1994-08-01", "end": "1994-09-30"})).json()
    assert [(e["kind"], e["name"], e["album"]) for e in cal] == [("album", "Portishead", "Dummy")]
    assert await music.refresh_artist(artist["id"]) == 0  # nothing new

    # changing the artist's mode re-applies it to the albums of the chosen kinds
    async def monitored() -> set[str]:
        albums = (await c.get(f"/titles/{artist['id']}/albums")).json()
        return {a["name"] for a in albums if a["wanted"]["monitored"]}

    t = (await c.get(f"/titles/{artist['id']}")).json()
    r = await c.patch(f"/titles/{artist['id']}", json={"monitor_mode": "latest"},
                      headers={"If-Match": t["etag"]})  # fmt: skip
    assert r.status_code == 200, r.text
    assert await monitored() == {"Third"}  # the newest studio album (2008)
    r = await c.patch(f"/titles/{artist['id']}", json={"monitor_mode": "season_1"},
                      headers={"If-Match": r.headers["etag"]})  # fmt: skip
    assert r.status_code == 422 and "artist" in r.text
    t = (await c.get(f"/titles/{artist['id']}")).json()
    await c.patch(f"/titles/{artist['id']}", json={"monitor_mode": "all"},
                  headers={"If-Match": t["etag"]})  # fmt: skip
    assert await monitored() == {"Dummy", "Portishead", "Third"}
    listed = (await c.get("/titles", params={"filter[kind]": "music"})).json()["items"]
    assert [t["name"] for t in listed] == ["Portishead"]


async def test_search_for_an_album(c: httpx.AsyncClient, server: Running, fakes: Fakes) -> None:
    """Real-world names for Dummy: the FLAC wins; each other one is turned down for what
    it is. Dummy's standard edition is 11 tracks, 49 minutes."""
    music = server.app.state.music
    music.transport, music.interval = fake_mb(), 0
    lossless = next(p["id"] for p in (await c.get("/quality-profiles")).json()
                    if p["name"] == "Lossless")  # fmt: skip
    artist = (await c.post("/music/artists", json={"mbid": ARTIST, "profile_id": lossless})).json()
    await music.fill_editions()
    dummy = next(a for a in (await c.get(f"/titles/{artist['id']}/albums")).json()
                 if a["name"] == "Dummy")  # fmt: skip
    mb = 1_000_000
    names = [
        ("Portishead - Dummy (1994) [FLAC]", 300 * mb),
        ("Portishead - Dummy (1994) [MP3 320]", 115 * mb),  # lossy: not in Lossless
        ("Portishead  Dummy (1994)", 290 * mb),  # says nothing; the size says FLAC
        ("Portishead - Dummy - Glory Box (Single) [FLAC]", 40 * mb),
        ("Portishead - Dummy (Karaoke) [FLAC]", 300 * mb),
        ("Portishead Discography 1994-2008 [FLAC]", 1500 * mb),
        ("Massive Attack - Mezzanine (1998) [FLAC]", 350 * mb),
        ("Portishead - Dummy [FLAC]", 6 * mb),  # far too small for 49 minutes
    ]
    items = [
        Item(n, size=s, category=3010, infohash=f"{i:040x}") for i, (n, s) in enumerate(names, 1)
    ]
    await _indexer(c, fakes, "musicidx", items)
    r = await c.get("/releases", params={"wanted_item_id": dummy["wanted"]["id"]})
    assert r.status_code == 200, r.text
    got = {x["candidate"]["title"]: x for x in r.json()["releases"]}
    best = r.json()["releases"][0]
    assert best["candidate"]["title"] == "Portishead - Dummy (1994) [FLAC]" and best["accepted"]
    assert best["quality"] == "FLAC"
    assert got["Portishead  Dummy (1994)"]["accepted"]  # FLAC by size, ranked after
    assert got["Portishead  Dummy (1994)"]["quality"] == "FLAC"

    def why(name: str) -> str:
        x = got[name]
        assert not x["accepted"], name
        return " | ".join(f"{j['code']}: {j['message']}" for j in x["rejections"])

    assert "quality_not_allowed" in why("Portishead - Dummy (1994) [MP3 320]")
    assert "single" in why("Portishead - Dummy - Glory Box (Single) [FLAC]")
    assert "karaoke" in why("Portishead - Dummy (Karaoke) [FLAC]")
    assert "wrong_album" in why("Portishead Discography 1994-2008 [FLAC]")
    assert "not by Portishead" in why("Massive Attack - Mezzanine (1998) [FLAC]")
    assert "size_too_small" in why("Portishead - Dummy [FLAC]")
    # the indexer was asked for music, by artist and album
    req = fakes.torznab["musicidx"].requests[-1]
    assert req.url.params["t"] == "search" and req.url.params["cat"] == "3000"
    assert req.url.params["q"] == "Portishead Dummy"


def _album_torrent(root: Path, name: str, files: dict[str, bytes]) -> Made:
    """A torrent of real audio files (make_torrent writes random bytes)."""
    import libtorrent as lt

    top = root / name
    for rel, data in files.items():
        (top / rel).parent.mkdir(parents=True, exist_ok=True)
        (top / rel).write_bytes(data)
    fs = lt.file_storage()
    for rel, data in sorted(files.items()):
        fs.add_file(f"{name}/{rel}", len(data))
    ct = lt.create_torrent(fs, 16 * 1024)
    lt.set_piece_hashes(ct, str(root))
    torrent = lt.bencode(ct.generate())
    ti = lt.torrent_info(lt.bdecode(torrent))
    return Made(torrent, str(ti.info_hashes().v1), root, name, files)


@pytest.mark.parametrize("link_mode", ["copy", "hardlink_or_copy"])
async def test_download_and_import_an_album(
    c: httpx.AsyncClient, server: Running, fakes: Fakes, tmp_path: Path, link_mode: str
) -> None:
    """Gate: an album downloads as FLAC, and every track is matched, named and tagged.
    A hardlinked file is the torrent's own while it seeds: its tags aren't touched."""
    import mutagen

    music = server.app.state.music
    music.transport, music.interval = fake_mb(), 0
    server.app.state.downloads.poll_interval = 0.1
    server.app.state.library.sweep_interval = 0.2
    profiles = (await c.get("/quality-profiles")).json()
    lossless = next(p for p in profiles if p["name"] == "Lossless")
    # the test's tracks are seconds long: no minimum size per minute for FLAC here
    r = await c.patch(f"/quality-profiles/{lossless['id']}", json={"sizes": {"FLAC": {"min": 0}}},
                      headers={"If-Match": lossless["etag"]})  # fmt: skip
    assert r.status_code == 200, r.text
    music_dest = next(
        d for d in (await c.get("/destinations")).json() if d["media_kind"] == "music"
    )
    r = await c.patch(f"/destinations/{music_dest['id']}", json={"link_mode": link_mode},
                      headers={"If-Match": music_dest["etag"]})  # fmt: skip
    assert r.status_code == 200, r.text

    artist = (
        await c.post("/music/artists", json={"mbid": ARTIST, "profile_id": lossless["id"]})
    ).json()
    await music.fill_editions()
    dummy = next(a for a in (await c.get(f"/titles/{artist['id']}/albums")).json()
                 if a["name"] == "Dummy")  # fmt: skip
    assert len(dummy["tracks"]) == 11
    sources = ["flac16_1s.flac", "flac16_2s.flac", "flac16_3s.flac"]
    files = {
        f"{t['number']:02d} - {t['name']}.flac": (AUDIO / sources[i % 3]).read_bytes()
        for i, t in enumerate(dummy["tracks"])
    }
    files["folder.jpg"] = b"\xff\xd8\xff not really"
    name = "Portishead - Dummy (1994) [FLAC]"
    made = _album_torrent(tmp_path / "seedroot", name, files)
    seed = await seeder(made)
    try:
        await _indexer(c, fakes, "musicidx",
                       [Item(name, size=len(b"".join(files.values())), category=3010,
                             magnet=made.magnet, infohash=made.info_hash)])  # fmt: skip
        rel = (await c.get("/releases", params={"wanted_item_id": dummy["wanted"]["id"]})).json()
        best = rel["releases"][0]
        assert best["accepted"], best["rejections"]
        assert (await c.post("/grabs", json={"release_id": best["release_id"]})).status_code == 201

        async def imported() -> bool:
            with contextlib.suppress(Exception):
                engine = next(x for x in server.app.state.downloads.clients.values()
                              if x.kind == "builtin")  # fmt: skip
                engine.connect_peer(made.info_hash, "127.0.0.1", seed.listen_port or 0)
            albums = (await c.get(f"/titles/{artist['id']}/albums")).json()
            return (
                next(a for a in albums if a["id"] == dummy["id"])["wanted"]["state"] == "acquired"
            )

        await until(imported, within=60)
    finally:
        await seed.stop()

    folder = Path(music_dest["path"]) / "Portishead" / "Dummy (1994)"
    names = sorted(p.name for p in folder.iterdir())
    assert len(names) == 11 and names[0] == "01 - Mysterons.flac", names
    first = mutagen.File(folder / "01 - Mysterons.flac")
    hist = (await c.get(f"/titles/{artist['id']}/history")).json()["items"]
    imported_ev = next(h for h in hist if h["type"] == "imported")
    assert imported_ev["data"]["tracks"] == 11
    if link_mode == "copy":
        assert first["title"] == ["Mysterons"] and first["artist"] == ["Portishead"]
        assert first["album"] == ["Dummy"] and first["tracknumber"] == ["1"]
        assert first["date"] == ["1994"] and first["musicbrainz_artistid"] == [ARTIST]
        assert imported_ev["data"]["tagged"] == 11
    else:  # hardlinked to the seeding download: left exactly as downloaded
        assert imported_ev["data"]["link_mode"] == "hardlink"
        assert imported_ev["data"]["tagged"] == 0 and not (first.tags or {}).get("title")
        # once the torrent is removed, the held-back tags are written
        [dl] = (await c.get("/downloads")).json()
        assert (await c.delete(f"/downloads/{dl['id']}")).status_code == 204

        async def tagged() -> bool:  # every track, whatever order the job takes them in
            return all(
                (f := mutagen.File(p)).tags and f.tags.get("title") for p in folder.iterdir()
            )

        await until(tagged, within=20)
        last = mutagen.File(folder / "11 - Glory Box.flac")
        assert last["tracknumber"] == ["11"] and last["album"] == ["Dummy"]
        assert mutagen.File(folder / "01 - Mysterons.flac")["title"] == ["Mysterons"]
    assert imported_ev["data"]["quality"] == "FLAC"
    queue = (await c.get("/downloads")).json()
    assert queue[0]["imported_to"].endswith("Portishead/Dummy (1994)")


async def test_rss_grabs_a_wanted_album(
    c: httpx.AsyncClient, server: Running, fakes: Fakes
) -> None:
    """The newest releases of a feed: the wanted album is grabbed; the rest is not."""
    from fetchbox.modules.automation.rss import rss_sync

    music = server.app.state.music
    music.transport, music.interval = fake_mb(), 0
    lossless = next(p["id"] for p in (await c.get("/quality-profiles")).json()
                    if p["name"] == "Lossless")  # fmt: skip
    body = {"mbid": ARTIST, "profile_id": lossless, "monitor_mode": "latest"}
    artist = (await c.post("/music/artists", json=body)).json()
    await music.fill_editions()
    mb = 1_000_000
    feed = [
        Item("Portishead - Third (2008) [FLAC]", size=320 * mb, category=3040, infohash="a" * 40),
        Item("Portishead - Dummy (1994) [FLAC]", size=300 * mb, category=3040, infohash="b" * 40),
        Item("Massive Attack - Mezzanine (1998) [FLAC]", size=350 * mb, category=3040,
             infohash="c" * 40),
        Item("Some.Show.S01E01.1080p.WEB-DL", size=2000 * mb, category=5040, infohash="d" * 40),
    ]  # fmt: skip
    await _indexer(c, fakes, "musicfeed", feed)
    out = await rss_sync(server.app.state.autosearch)
    # Third is the only wanted album ("latest"); Dummy isn't wanted, Mezzanine isn't theirs
    assert out["grabbed"] == ["Portishead - Third (2008) [FLAC]"], out
    third = next(a for a in (await c.get(f"/titles/{artist['id']}/albums")).json()
                 if a["name"] == "Third")  # fmt: skip
    assert third["wanted"]["state"] in ("grabbed", "downloading")
    # the feed asks for music too
    req = fakes.torznab["musicfeed"].requests[-1]
    assert "3000" in req.url.params["cat"].split(",")


async def test_import_an_existing_music_folder(c: httpx.AsyncClient, server: Running) -> None:
    """A tagged album (with MusicBrainz ids, as Picard writes) matches exactly; an untagged
    one is recognised from its Artist/Album (Year) folders. Applying records the files in
    place: nothing moves."""
    from fetchbox.modules.library.music_files import TagValues, write_tags
    from tests.test_existing_library import _job

    music = server.app.state.music
    music.transport, music.interval = fake_mb(), 0
    dest = next(d for d in (await c.get("/destinations")).json() if d["media_kind"] == "music")
    root = Path(dest["path"])
    dummy_tracks = [
        t["title"]
        for t in json.loads((FIX / "rels.json").read_text())["releases"][0]["media"][0]["tracks"]
    ]
    for i, name in enumerate(dummy_tracks, 1):
        p = root / "Portishead" / "Dummy (1994)" / f"{i:02d} - {name}.flac"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes((AUDIO / "flac16_1s.flac").read_bytes())
        write_tags(p, TagValues(artist="Portishead", album="Dummy", year=1994, title=name,
                                track=i, tracks=len(dummy_tracks), disc=1, discs=1,
                                artist_mbid=ARTIST,
                                album_mbid="48140466-cff6-3222-bd55-63c27e43190d"))  # fmt: skip
    third = root / "Portishead" / "Third (2008)" / "01 - Silence.mp3"
    third.parent.mkdir(parents=True)
    third.write_bytes((AUDIO / "mp3_320_1s.mp3").read_bytes())

    scan = await _job(
        c, (await c.post("/library/scan", json={"destination_id": dest["id"]})).json()["id"]
    )
    items = {i["match"]["album"]: i for i in scan["result"]["items"] if i["match"]}
    assert set(items) == {"Dummy", "Third"}, scan["result"]["items"]
    assert items["Dummy"]["confidence"] == "exact" and len(items["Dummy"]["files"]) == 11
    assert items["Third"]["confidence"] == "high" and items["Third"]["status"] == "new"

    lossless = next(p["id"] for p in (await c.get("/quality-profiles")).json()
                    if p["name"] == "Lossless")  # fmt: skip
    d = items["Dummy"]
    body = {"destination_id": dest["id"], "items": [{
        "folder": d["folder"], "files": [f["path"] for f in d["files"]],
        "artist_mbid": d["match"]["artist_mbid"], "album_mbid": d["match"]["album_mbid"],
        "profile_id": lossless,
    }]}  # fmt: skip
    res = (await _job(c, (await c.post("/library/import", json=body)).json()["id"]))["result"]
    assert res["files"] == 11 and not res["skipped"], res
    [artist] = (await c.get("/titles", params={"filter[kind]": "music"})).json()["items"]
    albums = {a["name"]: a for a in (await c.get(f"/titles/{artist['id']}/albums")).json()}
    assert (
        albums["Dummy"]["wanted"]["state"] == "acquired" and albums["Dummy"]["wanted"]["monitored"]
    )
    assert not albums["Third"]["wanted"]["monitored"]  # only what was imported is wanted
    files = (await c.get(f"/titles/{artist['id']}/files")).json()
    assert len(files) == 11 and {f["link_mode"] for f in files} == {"existing"}
    assert (root / "Portishead" / "Dummy (1994)" / "01 - Mysterons.flac").is_file()  # in place
    again = await _job(
        c, (await c.post("/library/scan", json={"destination_id": dest["id"]})).json()["id"]
    )
    dummy_again = next(
        i for i in again["result"]["items"] if i["match"] and i["match"]["album"] == "Dummy"
    )
    assert dummy_again["status"] == "imported"
