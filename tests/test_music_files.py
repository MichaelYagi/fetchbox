"""Album files: reading, matching files to tracks, naming and tagging, on real (tiny)
audio files: tests/fixtures/audio (1-3 s tones made with ffmpeg)."""

from __future__ import annotations

import shutil
from pathlib import Path

import mutagen
import pytest

from fetchbox.modules.library.music_files import (
    AudioFile,
    TagValues,
    WantedTrack,
    album_quality,
    match,
    read,
    track_path,
    write_tags,
)

AUDIO = Path(__file__).parent / "fixtures" / "audio"
COVER = b"\xff\xd8\xff\xe0" + b"\x00" * 200  # a JPEG header is enough for the tag


def _copy(tmp: Path, name: str, as_: str) -> Path:
    dst = tmp / as_
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(AUDIO / name, dst)
    return dst


@pytest.mark.parametrize(("name", "quality"), [
    ("flac16_1s.flac", "FLAC"), ("flac24_1s.flac", "FLAC-24bit"), ("mp3_320_1s.mp3", "MP3-320"),
    ("mp3_vbr_2s.mp3", "MP3-VBR-V2"), ("aac_1s.m4a", "AAC"),
])  # fmt: skip
def test_quality_is_what_the_file_is(name: str, quality: str) -> None:
    assert read(AUDIO / name, name).quality == quality


def test_album_quality_is_its_worst_file(tmp_path: Path) -> None:
    files = [read(AUDIO / n, n) for n in ("flac16_1s.flac", "flac24_1s.flac")]
    assert album_quality(files) == "FLAC"
    files.append(read(AUDIO / "mp3_320_1s.mp3", "x.mp3"))
    assert album_quality(files) == "MP3-320"


@pytest.mark.parametrize("name", ["flac16_1s.flac", "mp3_320_1s.mp3", "aac_1s.m4a"])
def test_write_tags_round_trip(tmp_path: Path, name: str) -> None:
    p = _copy(tmp_path, name, "file" + Path(name).suffix)
    v = TagValues(artist="Portishead", album="Dummy", year=1994, title="Sour Times", track=3,
                  tracks=11, disc=1, discs=1, artist_mbid="8f6bd1e4-fbe1-4f50-aa9b-94c450ec0f11",
                  album_mbid="48140466-cff6-3222-bd55-63c27e43190d")  # fmt: skip
    write_tags(p, v, cover=COVER)
    back = read(p, p.name)
    assert (back.title, back.track, back.disc) == ("Sour Times", 3, 1)
    m = mutagen.File(p)
    if name.endswith(".flac"):
        assert m["album"] == ["Dummy"] and m["date"] == ["1994"] and m.pictures
        assert m["musicbrainz_releasegroupid"] == ["48140466-cff6-3222-bd55-63c27e43190d"]
    elif name.endswith(".mp3"):
        assert str(m.tags["TALB"]) == "Dummy" and m.tags.getall("APIC")
    else:
        assert m.tags["\xa9alb"] == ["Dummy"] and m.tags["covr"]
    assert back.length_s and back.length_s > 0.9  # the audio is untouched


def _tracks(*rows: tuple[int, int, str, int]) -> list[WantedTrack]:
    return [WantedTrack(id=f"t{d}-{n}", disc=d, number=n, name=name, length_ms=ms)
            for d, n, name, ms in rows]  # fmt: skip


def _af(rel: str, **kw: object) -> AudioFile:
    return AudioFile(rel=rel, path=Path(rel), size=1, **kw)  # type: ignore[arg-type]


def test_match_by_tags_then_names_then_titles_then_lengths() -> None:
    tracks = _tracks(
        (1, 1, "Mysterons", 306_000),
        (1, 2, "Sour Times", 254_000),
        (1, 3, "Strangers", 238_000),
        (1, 4, "It Could Be Sweet", 260_000),
    )
    files = [
        _af("Dummy/a.flac", track=1, disc=1, length_s=306),  # tags
        _af("Dummy/02 - whatever.flac", length_s=254),  # file name number
        _af("Dummy/strangers.flac", length_s=10),  # title in the file name
        _af("Dummy/zzz.flac", length_s=261.5),  # only its length fits
        _af("Dummy/hidden track.flac", length_s=30),  # nobody's
    ]
    m = match(files, tracks)
    assert {t: f.rel for t, f in m.pairs.items()} == {
        "t1-1": "Dummy/a.flac", "t1-2": "Dummy/02 - whatever.flac",
        "t1-3": "Dummy/strangers.flac", "t1-4": "Dummy/zzz.flac",
    }  # fmt: skip
    assert not m.missing and [f.rel for f in m.extra] == ["Dummy/hidden track.flac"]


def test_match_two_discs_from_folders() -> None:
    tracks = _tracks((1, 1, "A", 1000), (1, 2, "B", 1000), (2, 1, "C", 1000), (2, 2, "D", 1000))
    files = [_af(f"Album/CD{d}/0{n} x.flac") for d in (1, 2) for n in (1, 2)]
    m = match(files, tracks)
    assert m.pairs["t2-1"].rel == "Album/CD2/01 x.flac" and not m.missing
    # "2-01" style names, no folders
    files = [_af(f"{d}-0{n} x.mp3") for d in (1, 2) for n in (1, 2)]
    assert match(files, tracks).pairs["t2-2"].rel == "2-02 x.mp3"


def test_missing_tracks_are_named() -> None:
    tracks = _tracks((1, 1, "A", 1000), (1, 2, "B", 1000), (1, 3, "C", 1000))
    m = match([_af("01 a.flac"), _af("02 b.flac")], tracks)
    assert [t.name for t in m.missing] == ["C"]


def test_track_path() -> None:
    [t] = _tracks((2, 3, "Wandering Star / Live?", 0))
    assert track_path("Portishead", "Dummy", 1994, t, 2, ".FLAC") == (
        "Portishead/Dummy (1994)/CD2/03 - Wandering Star - Live.flac"
    )
    assert track_path("AC/DC", "Back in Black", None, t, 1, ".mp3").startswith(
        "AC-DC/Back in Black/03 - "
    )


def test_naming_presets() -> None:
    from fetchbox.modules.library.music_files import Naming

    [t] = _tracks((2, 3, "Roads", 0))
    path = lambda **kw: track_path("Portishead", "Dummy", 1994, t, 2, ".flac", Naming(**kw))  # noqa: E731
    assert path() == "Portishead/Dummy (1994)/CD2/03 - Roads.flac"
    assert path(folder="artist/album") == "Portishead/Dummy/CD2/03 - Roads.flac"
    assert path(folder="artist - album (year)", disc_folders=False) == (
        "Portishead - Dummy (1994)/2-03 - Roads.flac"
    )
    assert path(track="nn. title", disc_folders=False) == "Portishead/Dummy (1994)/2-03. Roads.flac"
    assert path(track="artist - nn - title") == (
        "Portishead/Dummy (1994)/CD2/Portishead - 03 - Roads.flac"
    )
