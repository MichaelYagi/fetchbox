"""MusicBrainz client, against responses recorded from the real service (Portishead,
their albums, and the editions of Dummy)."""

from __future__ import annotations

import itertools
import json
import time
from datetime import date
from pathlib import Path

import httpx
import pytest

from fetchbox.providers.metadata.musicbrainz import (
    MusicBrainzClient,
    MusicBrainzError,
    cover_url,
    pick_edition,
    user_agent,
)

FIX = Path(__file__).parent / "fixtures" / "musicbrainz"
ARTIST = "8f6bd1e4-fbe1-4f50-aa9b-94c450ec0f11"
DUMMY = "48140466-cff6-3222-bd55-63c27e43190d"


def _load(name: str) -> object:
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


def fake_mb(seen: list[httpx.Request] | None = None) -> httpx.MockTransport:
    def handle(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(req)
        p = req.url.path.removeprefix("/ws/2")
        if p == "/artist":
            return httpx.Response(200, json=_load("search"))
        if p == f"/artist/{ARTIST}":
            return httpx.Response(200, json=_load("artist"))
        if p == "/release-group":
            return httpx.Response(200, json=_load("rgs"))
        if p == "/release":
            return httpx.Response(200, json=_load("rels"))
        return httpx.Response(404)

    return httpx.MockTransport(handle)


async def test_artist_albums_and_editions() -> None:
    seen: list[httpx.Request] = []
    mb = MusicBrainzClient(transport=fake_mb(seen), interval=0)
    [a] = await mb.search_artists("portishead")
    assert (a.id, a.name, a.type, a.country, a.begin_year) == (
        ARTIST,
        "Portishead",
        "Group",
        "GB",
        1991,
    )
    assert seen[0].headers["user-agent"] == user_agent() and "github.com" in user_agent()
    assert seen[0].url.params["fmt"] == "json"

    albums = await mb.albums(ARTIST)
    by_title = {x.title: x for x in albums}
    assert by_title["Dummy"].kind == "album" and by_title["Dummy"].release_date == date(1994, 8, 22)
    assert by_title["Mysteries"].kind == "compilation"
    assert by_title["Live Weisen 1998"].kind == "live"
    assert by_title["Glory Box"].kind == "single"
    assert by_title["Remixes"].kind == "other"  # an EP, but a remix one
    studio = sorted(x.title for x in albums if x.kind == "album")
    assert studio == ["Dummy", "Portishead", "Third"]

    eds = await mb.editions(DUMMY)
    assert len(eds) == 19 and {e.track_count for e in eds} >= {10, 11, 12}
    std = pick_edition(eds)
    assert std is not None and std.track_count == 11 and std.tracks[0].title == "Mysterons"
    assert std.tracks[0].length_ms and std.length_ms > 2_900_000
    await mb.aclose()


async def test_one_request_a_second_and_busy_retries() -> None:
    calls: list[float] = []

    def handle(req: httpx.Request) -> httpx.Response:
        calls.append(time.monotonic())
        if len(calls) == 2:
            return httpx.Response(503)  # "slow down"
        return httpx.Response(200, json=_load("artist"))

    mb = MusicBrainzClient(transport=httpx.MockTransport(handle), interval=0.2)
    await mb.artist(ARTIST)
    await mb.artist(ARTIST)  # 503 once, then answered
    gaps = [b - a for a, b in itertools.pairwise(calls)]
    # spaced by the interval (Windows' timer is coarse: a little slack)
    assert len(calls) == 3 and all(g >= 0.15 for g in gaps), gaps

    gone = MusicBrainzClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404)), interval=0
    )
    with pytest.raises(MusicBrainzError, match="doesn't know"):
        await gone.artist("nope")


def test_cover_url() -> None:
    assert cover_url(DUMMY, 500) == f"https://coverartarchive.org/release-group/{DUMMY}/front-500"
