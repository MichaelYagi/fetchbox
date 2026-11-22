"""Music release names, collected from real indexers (Prowlarr, category 3000) for 12
album searches: tests/fixtures/music_release_names.json."""

from __future__ import annotations

import collections
import json
from pathlib import Path

import pytest

from fetchbox.modules.releases.music import match_album, norm, parse_music, quality_from_size

NAMES = json.loads(
    (Path(__file__).parent / "fixtures" / "music_release_names.json").read_text(encoding="utf-8")
)
WANT = {
    "Massive Attack Mezzanine": ("Massive Attack", "Mezzanine"),
    "Portishead Dummy": ("Portishead", "Dummy"),
    "Radiohead OK Computer": ("Radiohead", "OK Computer"),
    "Daft Punk Random Access Memories": ("Daft Punk", "Random Access Memories"),
    "Pink Floyd Dark Side of the Moon": ("Pink Floyd", "The Dark Side of the Moon"),
    "Taylor Swift 1989": ("Taylor Swift", "1989"),
    "Kendrick Lamar DAMN": ("Kendrick Lamar", "DAMN."),
    "Fleetwood Mac Rumours": ("Fleetwood Mac", "Rumours"),
    "Bjork Homogenic": ("Björk", "Homogenic"),
    "Beatles Abbey Road": ("The Beatles", "Abbey Road"),
    "Nirvana Nevermind": ("Nirvana", "Nevermind"),
}


@pytest.mark.parametrize(("name", "quality"), [
    ("Taylor Swift - 1989 (2014) [FLAC] {HDtracks 24-44.1}", "FLAC-24bit"),
    ("Fleetwood Mac - Rumours [DVD-A] FLAC 24-96", "FLAC-24bit"),
    ("Radiohead - OK Computer: OKNOTOK 1997-2017 (2017) [24-96]", "FLAC-24bit"),
    ("Daft Punk   Random Access Memories [24Bit 88.2kHz] (2021) FLAC [PMEDIA]", "FLAC-24bit"),
    ("Nirvana - Nevermind (1991) [FLAC]", "FLAC"),
    ("Massive Attack - Mezzanine (FLAC/CUE/LOG)", "FLAC"),
    ("Taylor Swift  1989 (Deluxe Edition) (2014) [FLAC 16 44]", "FLAC"),
    ("Fleetwood Mac - Rumours (35th Anniversary Expanded Remaster) (MP3 320kbps)", "MP3-320"),
    ("Taylor Swift - 1989 (Deluxe Edition) (2014) [Real 320 KBPS]", "MP3-320"),
    ("Fleetwood Mac - Rumours [Mp3 - 192 Kbps] TNT Village", "MP3-192"),
    ("Daft Punk - Random Access Memories [m4a AAC-256kbps] Proper", "AAC"),
    ("Daft Punk - Random Access Memories [iTunes Plus]", "AAC"),
    ("Portishead  Dummy (1994)", "Unknown-Audio"),  # says nothing: judged by size
    ("Taylor Swift   1989 (Big Machine Radio Release Special)", "Unknown-Audio"),
])  # fmt: skip
def test_quality(name: str, quality: str) -> None:
    assert parse_music(name).quality == quality


def test_source_year_and_edition() -> None:
    p = parse_music("Nirvana-Nevermind (1991) 24/192 Vinyl FLAC [Geffen, 424 425-1 20")
    assert (p.quality, p.source, p.year) == ("FLAC-24bit", "Vinyl", 1991)
    p = parse_music("Fleetwood Mac - Rumours (Super Deluxe Edition) (2013) [FLAC 24-96]")
    assert "super deluxe" in p.edition or "deluxe" in p.edition
    assert parse_music("Pink Floyd - The Dark Side Of The Moon (1973/2023) [SACD]").disc_image
    assert parse_music("Massive Attack Discography (Blueshiff)").many_albums
    assert norm("Björk \u2013 Homogenic (Deluxe)") == "bjork homogenic deluxe"  # en dash


@pytest.mark.parametrize("name", [
    "Pink Floyd - Dark Side Of The Moon [1st UK Harvest issue] VINYL",  # no leading "The"
    "Bjork  Homogenic (1997 Alternativa e indie) [Flac 16 44]",  # no accent
    "Kendrick Lamar  DAMN (2017) [M4A iTunes]",  # no full stop
    "Nirvana [1991] Nevermind - FLAC 8",
    "[Rock City]Pink Floyd The Dark Side Of The Moon(1973)[Eac Flac Cue][LIFE](UF SPG)",
    "Fleetwood Mac - Rumours (35th Anniversary Expanded Remaster) (MP3 320kbps)",
])  # fmt: skip
def test_matches(name: str) -> None:
    search = next(q for q in WANT if any(n == name for n, _ in NAMES[q]))
    assert match_album(name, *WANT[search]).ok, match_album(name, *WANT[search])


@pytest.mark.parametrize(("name", "artist", "album", "why"), [
    ("Poor Man's Whiskey   Dark Side of the Moonshine (2013) [FLAC]", "Pink Floyd",
     "The Dark Side of the Moon", "not by"),
    ("Taylor Swift - 1989 - 3 Pre-Order Singles [iTunes Plus AAC M4A]", "Taylor Swift", "1989",
     "single"),
    ("Taylor Swift   Taylor Swift Karaoke 1989 (2014) MP3 @ 320 KBPS [GloDLS]", "Taylor Swift",
     "1989", "karaoke"),
    ("Radiohead  OK Computer (Instrumentals) (1997) [FLAC 16 44]", "Radiohead", "OK Computer",
     "instrumentals"),
    ("Daft Punk   Random Access Memories (Drumless Edition) (2023) Mp3 320kbps [PMEDIA]",
     "Daft Punk", "Random Access Memories", "drumless"),
    ("Massive Attack Discography (Blueshiff)", "Massive Attack", "Mezzanine", "not Mezzanine"),
    ("Portishead*Dummy+Self-Titled(2 albums)", "Portishead", "Dummy", "several"),
    ("Fleetwood Mac  Rumours Tour 1978  Tiger Stadium  Baton Rouge  2025  FLAC", "Fleetwood Mac",
     "Rumours", "tour"),
])  # fmt: skip
def test_rejects(name: str, artist: str, album: str, why: str) -> None:
    m = match_album(name, artist, album)
    assert not m.ok and why in m.reason, m


def test_another_album_named_too() -> None:
    others = ["Mysteries", "Third", "Portishead", "Roseland NYC Live"]
    m = match_album("Portishead -Dummy,Mysteries,Portishead-", "Portishead", "Dummy",
                    other_albums=others)  # fmt: skip
    assert not m.ok and "Mysteries" in m.reason
    # their self-titled album is the artist's name: not a second album
    assert match_album("Portishead - Dummy (1994) [FLAC]", "Portishead", "Dummy",
                       other_albums=others).ok  # fmt: skip
    m = match_album("Fleetwood Mac  Rumours Live (2023) Mp3 320kbps", "Fleetwood Mac", "Rumours",
                    other_albums=["Rumours Live", "Tusk"])  # fmt: skip
    assert not m.ok and "Rumours Live" in m.reason


def test_quality_from_size() -> None:
    mezzanine = 63 * 60_000  # ms
    assert quality_from_size(450_000_000, mezzanine) == "FLAC"
    assert quality_from_size(150_000_000, mezzanine) == "MP3-320"
    assert quality_from_size(1_500_000_000, mezzanine) == "FLAC-24bit"
    assert quality_from_size(8_000_000, mezzanine) is None  # a single track, not the album
    assert quality_from_size(None, mezzanine) is None


def test_corpus_does_not_regress() -> None:
    """Over every real name: how many say their format, and how many match."""
    quality: collections.Counter[str] = collections.Counter()
    matched = 0
    total = 0
    for search, rows in NAMES.items():
        for name, _size in rows:
            quality[parse_music(name).quality] += 1
            if search in WANT:
                total += 1
                matched += match_album(name, *WANT[search]).ok
    stated = sum(n for q, n in quality.items() if q != "Unknown-Audio")
    assert stated >= 580, quality  # 589 of 944 when written
    assert 820 <= matched <= 900, matched  # most search results are the album
