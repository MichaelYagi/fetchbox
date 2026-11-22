"""Music: artists, albums and editions, on MusicBrainz responses recorded from the real
service (Portishead; every album answers with Dummy's editions)."""

from __future__ import annotations

from datetime import date

import httpx

from fetchbox.modules.media.music import wanted_flags
from fetchbox.providers.metadata.musicbrainz import AlbumResult
from tests.conftest import Running
from tests.test_musicbrainz import ARTIST, fake_mb


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

    profile = (await c.get("/quality-profiles")).json()[0]["id"]
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

    assert await music.refresh_artist(artist["id"]) == 0  # nothing new
    listed = (await c.get("/titles", params={"filter[kind]": "music"})).json()["items"]
    assert [t["name"] for t in listed] == ["Portishead"]
