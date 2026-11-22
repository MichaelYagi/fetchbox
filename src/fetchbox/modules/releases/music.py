"""Music release names: what an album download is, and whether it's the album wanted.

Music names follow no convention ("Nirvana [1991] Nevermind - FLAC 8", "Fleetwood Mac -
Rumours [DVD-A] FLAC 24-96", "Daft Punk - Random Access Memories [m4a AAC-256kbps]"), so
nothing here splits a name into artist and album. Fetchbox always searches for an album
it knows, so it only asks: does the name contain that artist and that album (and not a
different album, a discography or a single), and what format is it?

The format comes from the name; when the name doesn't say, the album's length and the
download's size usually do (``quality_from_size``): FLAC is several times bigger than MP3.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field

_SEP = re.compile("[\\s._\\-\u2013\u2014:;,/\\\\|()\\[\\]{}+&'\"!?*~#@]+")


def norm(text: str) -> str:
    """Lowercase, accents removed, punctuation as spaces ("Björk: Homogenic" ->
    "bjork homogenic")."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace("&", " and ").replace("$", "s")
    return " ".join(_SEP.split(text)).strip()


def _words(text: str) -> list[str]:
    return [w for w in norm(text).split() if w]


# ---- quality ----------------------------------------------------------------------------------

_24BIT = re.compile(
    r"\b(24\s?bits?|24\s?[-/ ]\s?(44(\.1)?|48|88(\.2)?|96|176(\.4)?|192)(\s?k(hz)?)?|"
    r"(88(\.2)?|96|176|192)\s?khz|hi[\s-]?res|32\s?[-/ ]\s?192)\b",
    re.I,
)
_16BIT = re.compile(r"\b16\s?bits?|16\s?[-/ ]\s?44(\.1)?\b", re.I)
_FLAC = re.compile(r"\b(flac|lossless|cue\s?/?\s?log|log\s?/?\s?cue|wav)\b", re.I)
_ALAC = re.compile(r"\balac\b", re.I)
_AAC = re.compile(r"\b(aac|m4a|itunes(\s+plus)?)\b", re.I)
_MP3 = re.compile(r"\bmp3\b", re.I)
_V0 = re.compile(r"\b(v0|vbr\s?v?0)\b", re.I)
_V2 = re.compile(r"\b(v2|vbr\s?v?2)\b", re.I)
_KBPS = re.compile(r"\b(128|160|192|224|256|320)\s?(k|kbps|kbit|kb/s)?\b", re.I)
_DISC_IMAGE = re.compile(r"\b(sacd|dvd[\s-]?a|dsd(64|128|256)?|iso)\b", re.I)
_VINYL = re.compile(r"\b(vinyl|lp\s?rip|24[\s-]?bit\s+vinyl)\b", re.I)
_WEB = re.compile(r"\b(web(\s?-?dl)?|hdtracks|qobuz|tidal|bandcamp|deezer|itunes)\b", re.I)
_CD = re.compile(r"\b(\d?cd\d?|cdda|cd[\s-]?rip)\b", re.I)
_YEAR = re.compile(r"(?<!\d)(19[5-9]\d|20[0-4]\d)(?!\d)")
_EDITION = re.compile(
    r"\b(deluxe|super\s+deluxe|expanded|anniversary|remaster(ed)?|reissue|box\s?set|"
    r"collector'?s|special\s+edition|bonus|demos|outtakes|instrumentals?|acapella|"
    r"karaoke|live|unplugged|mfsl|remix(es)?|multitrack|drumless|edition)\b",
    re.I,
)
_MANY = re.compile(
    r"\b(discography|complete\s+(albums|studio|collection|works)|collection|anthology|"
    r"albums?\s*\(?\d{4}\s*-\s*\d{4}|\d{4}\s*-\s*\d{4}\s*(albums|discography))\b",
    re.I,
)
# Not the album itself, whatever else the name says
_VARIANT = re.compile(
    r"\b(karaoke|instrumentals?|acapellas?|a\s?cappella|drumless|tribute|covers?|lullab(y|ies)|"
    r"radio\s+edit|8[\s-]?bit|bootleg|live\s+(at|in|from)|tour\s+\d{4}|multitracks?|"
    r"interviews?|dolby\s+headphone|piano\s+(version|tribute)|reimagined|orchestral\s+version)\b",
    re.I,
)
_MULTI = re.compile(r"\b(\d+|two|three|four)\s+albums\b", re.I)
_SINGLE = re.compile(r"\b(single|singles|ep|cdm|cds|maxi)\b", re.I)


@dataclass
class MusicParse:
    quality: str  # a catalog name: FLAC, FLAC-24bit, MP3-320, ..., Unknown-Audio
    source: str | None  # CD, WEB, Vinyl, SACD / DVD-A (disc images)
    year: int | None
    years: list[int] = field(default_factory=list)
    edition: list[str] = field(default_factory=list)  # deluxe, remaster, live, ...
    many_albums: bool = False  # discography / collection
    disc_image: bool = False  # SACD / DVD-A / DSD: not files players play


def parse_music(name: str) -> MusicParse:
    text = name.replace("_", " ")
    hi = bool(_24BIT.search(text))
    lossless = bool(_FLAC.search(text))
    image = bool(_DISC_IMAGE.search(text))
    if _ALAC.search(text):
        quality = "ALAC"
    elif (lossless and hi) or (hi and not _MP3.search(text) and not _AAC.search(text)):
        quality = "FLAC-24bit"  # "[24-96]" alone is a hi-res lossless release
    elif lossless or _16BIT.search(text):
        quality = "FLAC"
    elif _V0.search(text):
        quality = "MP3-VBR-V0"
    elif _V2.search(text):
        quality = "MP3-VBR-V2"
    elif _AAC.search(text):
        quality = "AAC"
    elif (m := _KBPS.search(text)) and (m.group(2) or re.search(r"\b(mp3|cbr)\b", text, re.I)):
        # a number is a bitrate only with "k"/"kbps" or next to MP3: not "Album 192"
        rate = int(m.group(1))
        quality = {320: "MP3-320", 256: "MP3-256"}.get(rate) or (
            "MP3-192" if rate >= 192 else "MP3-128"
        )
    elif _MP3.search(text):
        quality = "MP3-192"  # MP3 of an unstated bitrate: assume a middling one
    else:
        quality = "Unknown-Audio"
    source = (
        "SACD / DVD-A" if image else
        "Vinyl" if _VINYL.search(text) else
        "WEB" if _WEB.search(text) else
        "CD" if _CD.search(text) else None
    )  # fmt: skip
    years = [int(y) for y in _YEAR.findall(text)]
    return MusicParse(
        quality=quality,
        source=source,
        year=years[0] if years else None,
        years=years,
        edition=sorted({m.group(0).lower() for m in _EDITION.finditer(text)}),
        many_albums=bool(_MANY.search(text)),
        disc_image=image,
    )


def quality_from_size(size_bytes: int | None, album_ms: int | None) -> str | None:
    """A format guess from size per minute of music, for names that don't say."""
    if not size_bytes or not album_ms:
        return None
    mb_per_min = size_bytes / 1_000_000 / (album_ms / 60000)
    if mb_per_min >= 12:
        return "FLAC-24bit"
    if mb_per_min >= 4:
        return "FLAC"
    if mb_per_min >= 1.8:
        return "MP3-320"
    if mb_per_min >= 0.6:
        return "MP3-192"
    return None


# ---- matching ---------------------------------------------------------------------------------

_LEADING_THE = re.compile(r"^the\s+")


def _contains(hay: list[str], needle: list[str]) -> bool:
    """``needle``'s words appear in ``hay`` in order (other words may sit between)."""
    if not needle:
        return False
    i = 0
    for w in hay:
        if w == needle[i]:
            i += 1
            if i == len(needle):
                return True
    return False


def _whole(hay: list[str], needle: list[str]) -> bool:
    """``needle`` appears as consecutive words in ``hay``."""
    n = len(needle)
    return any(hay[i : i + n] == needle for i in range(len(hay) - n + 1))


@dataclass
class MusicMatch:
    ok: bool
    reason: str = ""


def match_album(
    name: str,
    artist: str,
    album: str,
    *,
    other_albums: Sequence[str] = (),
    album_kind: str = "album",
) -> MusicMatch:
    """Is ``name`` this artist's ``album`` (not another album, a discography, a single)?"""
    words = _words(name)
    a = _LEADING_THE.sub("", norm(artist)).split()
    if not _contains(words, a):
        return MusicMatch(False, f"not by {artist}")
    al = _LEADING_THE.sub("", norm(album)).split() or _words(album)
    if not _contains(words, al):
        return MusicMatch(False, f"not {album}")
    p = parse_music(name)
    if p.many_albums:
        return MusicMatch(False, "a discography or collection, not one album")
    if (v := _VARIANT.search(name)) and not _VARIANT.search(album):
        return MusicMatch(False, f"a {v.group(0).lower()} version, not the album")
    if _MULTI.search(name):
        return MusicMatch(False, "several albums, not one")
    # Another of the artist's albums named too: a bundle of several albums. (Not their
    # self-titled album: its title is the artist's name, in every name anyway.)
    for other in other_albums:
        ow = _LEADING_THE.sub("", norm(other)).split()
        if not ow or ow == a or _contains(al, ow) or _contains(ow, al):
            continue
        if _whole(words, ow):
            return MusicMatch(False, f"also has {other}: several albums")
    if album_kind == "album" and _SINGLE.search(name) and not _contains(words, _words("ep")):
        return MusicMatch(False, "a single, not the album")
    # A longer title of this artist containing the wanted one ("Rumours" vs "Rumours
    # Live"): the name is the other album when it holds that title's extra words too.
    for other in other_albums:
        ow = _words(other)
        if len(ow) > len(al) and _contains(ow, al) and _contains(words, ow):
            return MusicMatch(False, f"that's {other}")
    return MusicMatch(True)
