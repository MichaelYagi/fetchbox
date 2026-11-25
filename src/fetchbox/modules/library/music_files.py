"""Album downloads, file by file: read each audio file, match it to a track of the wanted
edition, name it, and write its tags.

Matching, per file, in order of trust: the disc and track numbers in its tags; the number
its file name starts with ("01 - Angel.flac", "1-03 Title.mp3") with the disc from a "CD2"
/ "Disc 2" folder; then its title; then its length (within a few seconds). An album only
imports when every track of the wanted edition has a file; otherwise the import says which
tracks are missing (wrong edition, incomplete download).

The quality is what the files are (FLAC bit depth, MP3 bitrate), not what the name said.

Tags (mutagen, GPL-2.0-or-later): artist, album artist, album, year, track, disc, title,
and the MusicBrainz ids players and media servers use; the cover goes in too when given.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from fetchbox.modules.library.naming import safe_component
from fetchbox.modules.releases.music import norm

AUDIO_EXTS = {
    ".flac",
    ".mp3",
    ".m4a",
    ".mp4",
    ".aac",
    ".ogg",
    ".opus",
    ".wav",
    ".aiff",
    ".aif",
    ".wma",
    ".ape",
    ".wv",
}
LENGTH_SLACK_S = 6.0  # a file's length may differ this much from MusicBrainz's


@dataclass
class AudioFile:
    rel: str  # path in the download
    path: Path  # on disk
    size: int
    length_s: float | None = None
    title: str | None = None
    track: int | None = None
    disc: int | None = None
    quality: str = "Unknown-Audio"
    codec: str = ""


@dataclass
class WantedTrack:
    id: str
    disc: int
    number: int
    name: str
    length_ms: int | None


@dataclass
class TrackMatch:
    pairs: dict[str, AudioFile] = field(default_factory=dict)  # track id -> file
    missing: list[WantedTrack] = field(default_factory=list)
    extra: list[AudioFile] = field(default_factory=list)  # files no track wanted


def is_audio(path: str) -> bool:
    p = PurePosixPath(path)
    return p.suffix.lower() in AUDIO_EXTS and not p.name.startswith("._")  # not a macOS ._ file


def _num(v: Any) -> int | None:
    """ "3", "3/12", ["03"] -> 3."""
    if isinstance(v, list | tuple):
        v = v[0] if v else None
    if v is None:
        return None
    if isinstance(v, tuple):
        v = v[0]
    m = re.match(r"\s*(\d+)", str(v))
    return int(m.group(1)) if m else None


def _first(tags: Any, *keys: str) -> Any:
    for k in keys:
        try:
            v = tags.get(k)
        except (KeyError, ValueError):
            v = None
        if v:
            return v[0] if isinstance(v, list) else v
    return None


def read(path: Path, rel: str) -> AudioFile:
    """Tags, length and quality of one audio file (whatever mutagen can read)."""
    import mutagen

    af = AudioFile(rel=rel, path=path, size=path.stat().st_size)
    try:
        m = mutagen.File(path)
    except Exception:
        m = None
    if m is None:
        return af
    info = getattr(m, "info", None)
    af.length_s = getattr(info, "length", None)
    tags = m.tags
    name = type(m).__name__  # FLAC, MP3, MP4, OggVorbis, ...
    af.codec = name
    if tags is not None:
        if name == "MP3":  # ID3 frames
            af.title = str(_first(tags, "TIT2") or "") or None
            af.track = _num(str(_first(tags, "TRCK") or ""))
            af.disc = _num(str(_first(tags, "TPOS") or ""))
        elif name == "MP4":
            af.title = _first(tags, "\xa9nam")
            trkn, disk = _first(tags, "trkn"), _first(tags, "disk")
            af.track, af.disc = _num(trkn), _num(disk)
        else:  # Vorbis comments (FLAC, Ogg, Opus)
            af.title = _first(tags, "title")
            af.track = _num(_first(tags, "tracknumber"))
            af.disc = _num(_first(tags, "discnumber"))
    af.quality = _quality(name, info)
    return af


def _quality(codec: str, info: Any) -> str:
    if codec == "FLAC":
        return "FLAC-24bit" if (getattr(info, "bits_per_sample", 16) or 16) >= 24 else "FLAC"
    if codec == "MP4":
        return "ALAC" if getattr(info, "codec", "").startswith("alac") else "AAC"
    if codec == "MP3":
        rate = (getattr(info, "bitrate", 0) or 0) // 1000
        vbr = str(getattr(info, "bitrate_mode", "")).endswith("VBR")
        if vbr:
            return "MP3-VBR-V0" if rate >= 220 else "MP3-VBR-V2"
        return (
            "MP3-320"
            if rate >= 310
            else "MP3-256"
            if rate >= 250
            else "MP3-192"
            if rate >= 185
            else "MP3-128"
        )
    if codec in ("OggVorbis", "OggOpus"):
        return "AAC"  # comparable lossy quality; there's no separate tier
    return "Unknown-Audio"


def album_quality(files: list[AudioFile]) -> str:
    """The album's quality: its worst file's (one MP3 among FLACs makes it lossy)."""
    from fetchbox.modules.profiles.qualities import RANK

    known = [f.quality for f in files if f.quality != "Unknown-Audio"]
    return min(known, key=lambda q: RANK[q]) if known else "Unknown-Audio"


_LEAD_NUM = re.compile(r"^\s*(?:(\d)[\s._-]+)?(\d{1,3})(?=[\s._)\]-])")
_DISC_DIR = re.compile(r"\b(?:cd|disc|disk|vol(?:ume)?)\s*\.?\s*(\d{1,2})\b", re.I)


def _from_name(f: AudioFile) -> tuple[int | None, int | None]:
    """(disc, track) from the file name and its folder: "2-03 Title", "CD2/03 Title"."""
    p = PurePosixPath(f.rel)
    m = _LEAD_NUM.match(p.stem)
    disc = int(m.group(1)) if m and m.group(1) else None
    track = int(m.group(2)) if m else None
    if track is not None and track > 100 and disc is None:  # "103 Title": disc 1, track 3
        disc, track = track // 100, track % 100
    for part in reversed(p.parts[:-1]):
        if dm := _DISC_DIR.search(part):
            disc = disc or int(dm.group(1))
            break
    return disc, track


def match(files: list[AudioFile], tracks: list[WantedTrack]) -> TrackMatch:
    """Pair every wanted track with one file."""
    out = TrackMatch()
    left = list(files)
    multi_disc = len({t.disc for t in tracks}) > 1

    def take(t: WantedTrack, f: AudioFile) -> None:
        out.pairs[t.id] = f
        left.remove(f)

    def by_number(t: WantedTrack, get: Any) -> AudioFile | None:
        hits = []
        for f in left:
            disc, track = get(f)
            same_disc = (disc or 1) == t.disc or (disc is None and not multi_disc)
            if track == t.number and same_disc:
                hits.append(f)
        return hits[0] if len(hits) == 1 else None

    passes = (
        lambda f: (f.disc, f.track),  # tags
        _from_name,  # file name and folder
    )
    for get in passes:
        for t in tracks:
            if t.id not in out.pairs and (f := by_number(t, get)) is not None:
                take(t, f)
    for t in tracks:  # titles
        if t.id in out.pairs:
            continue
        want = norm(t.name)
        hits = [
            f
            for f in left
            if want and (norm(f.title or "") == want or want in norm(PurePosixPath(f.rel).stem))
        ]
        if len(hits) == 1:
            take(t, hits[0])
    for t in tracks:  # lengths
        if t.id in out.pairs or not t.length_ms:
            continue
        hits = [
            f
            for f in left
            if f.length_s is not None and abs(f.length_s - t.length_ms / 1000) <= LENGTH_SLACK_S
        ]
        if len(hits) == 1:
            take(t, hits[0])
    out.missing = [t for t in tracks if t.id not in out.pairs]
    out.extra = left
    return out


@dataclass
class Naming:
    """Settings > Library folders > Music (MusicConfig)."""

    folder: str = "artist/album (year)"
    track: str = "nn - title"
    disc_folders: bool = True


def track_path(
    artist: str,
    album: str,
    year: int | None,
    t: WantedTrack,
    discs: int,
    ext: str,
    naming: Naming | None = None,
    *,
    artist_folder: str | None = None,
) -> str:
    """Artist/Album (Year)/[CD2/]03 - Title.flac, or as ``naming`` says.
    ``artist_folder``: the artist's own folder (remembered), instead of the name."""
    n = naming or Naming()
    dated = f"{album} ({year})" if year else album
    if n.folder == "artist - album (year)":
        parts = [safe_component(f"{artist} - {dated}")]
    else:
        parts = [
            artist_folder or safe_component(artist),
            safe_component(album if n.folder == "artist/album" else dated),
        ]
    num = f"{t.number:02d}"
    if discs > 1:
        if n.disc_folders:
            parts.append(f"CD{t.disc}")
        else:
            num = f"{t.disc}-{num}"
    name = {
        "nn. title": f"{num}. {t.name}",
        "artist - nn - title": f"{artist} - {num} - {t.name}",
    }.get(n.track, f"{num} - {t.name}")
    parts.append(safe_component(name) + ext.lower())
    return "/".join(parts)


@dataclass
class TagValues:
    artist: str
    album: str
    year: int | None
    title: str
    track: int
    tracks: int
    disc: int
    discs: int
    artist_mbid: str | None = None
    album_mbid: str | None = None  # the release group
    release_mbid: str | None = None  # the edition
    recording_mbid: str | None = None


def write_tags(path: Path, v: TagValues, cover: bytes | None = None) -> None:
    """Write the tags (and the front cover) the file format supports."""
    import mutagen

    m = mutagen.File(path)
    if m is None:
        return
    name = type(m).__name__
    year = str(v.year) if v.year else None
    if name == "MP3":
        from mutagen.id3 import APIC, ID3, TALB, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK, TXXX, UFID

        tags = m.tags if m.tags is not None else ID3()
        tags.delall("APIC") if cover else None
        tags.add(TPE1(encoding=3, text=v.artist))
        tags.add(TPE2(encoding=3, text=v.artist))
        tags.add(TALB(encoding=3, text=v.album))
        tags.add(TIT2(encoding=3, text=v.title))
        tags.add(TRCK(encoding=3, text=f"{v.track}/{v.tracks}"))
        tags.add(TPOS(encoding=3, text=f"{v.disc}/{v.discs}"))
        if year:
            tags.add(TDRC(encoding=3, text=year))
        for desc, val in (
            ("MusicBrainz Artist Id", v.artist_mbid),
            ("MusicBrainz Release Group Id", v.album_mbid),
            ("MusicBrainz Album Id", v.release_mbid),
        ):
            if val:
                tags.add(TXXX(encoding=3, desc=desc, text=val))
        if v.recording_mbid:
            tags.add(UFID(owner="http://musicbrainz.org", data=v.recording_mbid.encode()))
        if cover:
            tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
        tags.save(path, v2_version=4)
        return
    if name == "MP4":
        from mutagen.mp4 import MP4Cover

        if m.tags is None:
            m.add_tags()
        t: Any = m.tags
        t["\xa9ART"], t["aART"], t["\xa9alb"], t["\xa9nam"] = v.artist, v.artist, v.album, v.title
        t["trkn"], t["disk"] = [(v.track, v.tracks)], [(v.disc, v.discs)]
        if year:
            t["\xa9day"] = year
        if cover:
            t["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG)]
        m.save()
        return
    # Vorbis comments: FLAC, Ogg Vorbis, Opus
    if m.tags is None:
        m.add_tags()
    vc: Any = m.tags
    vc["artist"], vc["albumartist"], vc["album"], vc["title"] = v.artist, v.artist, v.album, v.title
    vc["tracknumber"], vc["tracktotal"] = str(v.track), str(v.tracks)
    vc["discnumber"], vc["disctotal"] = str(v.disc), str(v.discs)
    if year:
        vc["date"] = year
    for key, val in (
        ("musicbrainz_artistid", v.artist_mbid),
        ("musicbrainz_releasegroupid", v.album_mbid),
        ("musicbrainz_albumid", v.release_mbid),
        ("musicbrainz_trackid", v.recording_mbid),
    ):
        if val:
            vc[key] = val
    if cover and name == "FLAC":
        from mutagen.flac import Picture

        pic = Picture()
        pic.type, pic.mime, pic.desc, pic.data = 3, "image/jpeg", "Cover", cover
        m.clear_pictures()
        m.add_picture(pic)
    m.save()
