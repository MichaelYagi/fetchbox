"""Release-name parser: scene, P2P and anime names -> structured fields.

Pure and synchronous (no I/O). ``parse_release(name)`` never raises; an unparseable name
gives a result with only ``title`` set.

How it works:

1. Strip things that are never part of the title or quality: file extension, website
   prefixes (``www.site.com - ``), trailing tracker tags (``[rartv]``, ``[eztv.re]``), the
   anime CRC (``[ABCD1234]``), the leading anime group (``[SubsPlease]``) and repost
   suffixes (``-Obfuscated``).
2. Split off the scene group (``-GROUP``), tentatively: it is put back if nothing else in
   the name looks like release information (so ``Spider-Man`` keeps its ``Man``).
3. Find where the title ends: the first season/episode/date marker, the release year, or
   the first token that never appears in titles (``1080p``, ``BluRay``, ``x264``...).
   Uppercase-only scene words (``FRENCH``, ``PROPER``, ``CAM``) also end the title unless
   the whole name is uppercase.
4. Read quality, codecs, HDR, audio, languages, edition, revision and flags from the part
   after the title only, so episode titles and movie titles can't leak into them. Each
   detector blanks what it matched so later ones can't match it again (``WEB-DL`` is not
   also the German ``DL``).

The fixture corpus in ``tests/fixtures/releases`` is the specification; add a line there
for every name that parses wrong before changing anything here.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import Literal

from pydantic import BaseModel, computed_field

Source = Literal[
    "cam",
    "telesync",
    "telecine",
    "workprint",
    "screener",
    "regional",
    "sdtv",
    "hdtv",
    "dvd",
    "webrip",
    "webdl",
    "bluray",
]
Modifier = Literal["remux", "brdisk", "rawhd"]


class Revision(BaseModel):
    version: int = 1  # 2 = PROPER/REPACK/v2, 3 = REPACK2/v3 ...
    real: int = 0  # number of REAL tags


class ParsedRelease(BaseModel):
    raw: str
    title: str
    alt_titles: list[str] = []
    year: int | None = None

    seasons: list[int] = []
    episodes: list[int] = []
    absolute_episodes: list[int] = []
    air_date: date | None = None
    complete_series: bool = False

    resolution: int | None = None
    source: Source | None = None
    modifier: Modifier | None = None
    video_codec: str | None = None
    bit_depth: int | None = None
    hdr: list[str] = []
    audio_codecs: list[str] = []
    audio_channels: str | None = None
    atmos: bool = False
    streaming_service: str | None = None
    edition: str | None = None

    languages: list[str] = []  # ISO 639-1; empty = not stated (usually the original)
    multi_language: bool = False
    subtitle_languages: list[str] = []
    hardcoded_subs: bool = False
    dubbed: bool = False

    group: str | None = None
    revision: Revision = Revision()
    proper: bool = False
    repack: bool = False
    internal: bool = False
    limited: bool = False
    three_d: bool = False
    hybrid: bool = False
    sample: bool = False
    hash: str | None = None
    extension: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def media_type(self) -> Literal["movie", "series"]:
        series = (
            self.seasons
            or self.episodes
            or self.absolute_episodes
            or self.air_date
            or self.complete_series
        )
        return "series" if series else "movie"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def full_season(self) -> bool:
        return bool(self.seasons) and not self.episodes and not self.absolute_episodes

    @computed_field  # type: ignore[prop-decorator]
    @property
    def quality(self) -> str:
        return quality_name(self.source, self.resolution, self.modifier)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def normalized_title(self) -> str:
        return normalize_title(self.title)


def quality_name(source: str | None, resolution: int | None, modifier: str | None) -> str:
    """Ladder name, e.g. ``WEBDL-1080p``, ``Remux-2160p``, ``BR-DISK``, ``DVD``."""
    if modifier == "brdisk":
        return "BR-DISK"
    if modifier == "rawhd":
        return "Raw-HD"
    if modifier == "remux":
        return f"Remux-{resolution or 1080}p" if source != "dvd" else "DVD-Remux"
    match source:
        case "cam":
            return "CAM"
        case "telesync":
            return "TELESYNC"
        case "telecine":
            return "TELECINE"
        case "workprint":
            return "WORKPRINT"
        case "screener":
            return "DVDSCR"
        case "regional":
            return "REGIONAL"
        case "dvd":
            return "DVD"
        case "sdtv":
            return "SDTV"
        case "hdtv":
            return f"HDTV-{resolution}p" if resolution and resolution >= 720 else "SDTV"
        case "webdl":
            return f"WEBDL-{resolution or 480}p"
        case "webrip":
            return f"WEBRip-{resolution or 480}p"
        case "bluray":
            return f"Bluray-{resolution or 480}p"
    return f"Unknown-{resolution}p" if resolution else "Unknown"


# ---- title normalization ------------------------------------------------------------------

_SINGLE_RUN = re.compile(r"\b(?:[a-z0-9] )+[a-z0-9]\b")


def normalize_title(title: str) -> str:
    """Comparison key: lowercase ASCII, ``&`` -> ``and``, no punctuation; runs of single
    characters are joined so ``S.H.I.E.L.D.``, ``S H I E L D`` and ``SHIELD`` agree."""
    t = unicodedata.normalize("NFKD", title)
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    t = t.replace("&", " and ")
    t = re.sub("['`\u2019\u00b4]", "", t)
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    return _SINGLE_RUN.sub(lambda m: m.group().replace(" ", ""), t)


# ---- regex helpers ------------------------------------------------------------------------

_L = r"(?<![A-Za-z0-9])"
_R = r"(?![A-Za-z0-9])"
_SEP = r"[\s._-]"
_MAX_YEAR = date.today().year + 1


def _rx(pattern: str, *, cs: bool = False) -> re.Pattern[str]:
    return re.compile(_L + "(?:" + pattern + ")" + _R, 0 if cs else re.I)


class _Text:
    """The part after the title, with matched spans blanked out as detectors consume them."""

    def __init__(self, text: str) -> None:
        self.s = text

    def take(self, rx: re.Pattern[str]) -> list[re.Match[str]]:
        found = list(rx.finditer(self.s))
        for m in found:
            self.s = self.s[: m.start()] + " " * (m.end() - m.start()) + self.s[m.end() :]
        return found

    def first(self, rx: re.Pattern[str]) -> re.Match[str] | None:
        found = self.take(rx)
        return found[0] if found else None


# ---- step 1: cleanup ----------------------------------------------------------------------

_EXT = re.compile(
    r"\.(mkv|mp4|avi|m4v|ts|m2ts|wmv|mov|mpe?g|webm|flv|iso|img|vob|divx|ogm|rmvb|nzb|torrent)$",
    re.I,
)
_PARTIAL = re.compile(r"\.(?:part|!qB|!ut|crdownload|tmp)$", re.I)
_TLD = r"(?:com|org|net|to|me|mx|nu|cc|ws|info|xyz|io|tv|in|ch|is|se|li|re|am|ag|lol|cz|eu|pw)"
_SITE_PREFIX = re.compile(
    rf"^\s*(?:[\[(【]\s*(?:www\.)?[^\]\s)】]+\.{_TLD}\s*[\])】]"
    rf"|www\.[^\s]+?\.{_TLD}(?=[\s_-]))\s*[-\u2013_:]*\s*",
    re.I,
)
_TRAILING_TAG = re.compile(r"\s*[\[(]([^\[\]()]+)[\])]\s*$")
_KNOWN_TAGS = {
    "rartv",
    "rarbg",
    "eztv",
    "ettv",
    "tgx",
    "publichd",
    "ethd",
    "etrg",
    "vtv",
    "torrentgalaxy",
    "1337x",
    "psa",
    "mega",
    "xvid",
}
_CRC = re.compile(r"^[0-9A-Fa-f]{8}$")
_SITE_LIKE = re.compile(rf"^(?:www\.)?[A-Za-z0-9-]+\.{_TLD}$", re.I)
_JUNK_SUFFIX = re.compile(
    r"(?:[-.\s](?:Obfuscated|postbot|xpost|Scrambled|AsRequested|RP|Rakuten|WhiteRev|BUYMORE"
    r"|Chamele0n|newz\[NZB\]|NZBgeek|DDLValley|RakuvFIN|WRTEAM))+$",
    re.I,
)
_LEAD_GROUP = re.compile(r"^\s*[\[【]([^\]】]+)[\]】]\s*")
_QUALITYISH = re.compile(r"\d{3,4}[pi]|x26[45]|hevc|web|bluray", re.I)


def _strip_trailing_tags(s: str) -> tuple[str, str | None, str | None]:
    """Return (rest, crc_hash, site_group) after removing trailing tracker/site/CRC tags."""
    crc = site_group = None
    while m := _TRAILING_TAG.search(s):
        inner = m.group(1).strip()
        low = inner.lower()
        if _CRC.match(inner):
            crc = inner.upper()
        elif low in _KNOWN_TAGS or _SITE_LIKE.match(inner):
            if low.startswith(("yts", "yify")):
                site_group = "YTS"
        else:
            break
        s = s[: m.start()]
    return s.rstrip(" -_."), crc, site_group


# ---- step 2: group ------------------------------------------------------------------------

_GROUP = re.compile(r"-([A-Za-z0-9_@$!&]+)$")
_NODASH_GROUP = re.compile(r"(?<=[\s._])(YIFY|YTS|RARBG|GalaxyRG|GalaxyTV|EZTVx)$", re.I)
_NOT_GROUPS = {
    "DL", "RIP", "HD", "MA", "X", "HRA", "HR", "ES", "DTS", "AC3", "AAC", "WEB", "SBS", "OU",
    "264", "265", "LC", "AUDIO", "TS", "SUB", "SUBS", "DUB", "CUT", "RAY", "DVD", "AC", "3",
    "BR", "BD", "VC1", "1", "CAM", "HDR", "MUX", "DLMUX", "SCR", "PL", "TV",
}  # fmt: skip


def _valid_group(g: str) -> bool:
    up = g.upper()
    if up in _NOT_GROUPS or g.isdigit():
        return False
    return not re.fullmatch(r"\d{3,4}[PI]|S\d+(E\d+)?|E\d+|\d+X\d+", up)


# ---- step 3: episode markers --------------------------------------------------------------


class _Ep:
    def __init__(self, start: int, end: int) -> None:
        self.start, self.end = start, end
        self.seasons: list[int] = []
        self.episodes: list[int] = []
        self.absolute: list[int] = []
        self.air_date: date | None = None
        self.complete = False
        self.version: int | None = None


_SXE = re.compile(_L + r"S(\d{1,4})[\s._-]?E(\d{1,4})(?![0-9])", re.I)
_SXE_CONT = re.compile(
    r"(?:(?P<sep>[-+&]|[\s._]?)(?:S\d{1,4})?[\s._]?E(?P<e>\d{1,4})|(?P<dash>-)(?P<e2>\d{1,4}))"
    r"(?![0-9pPiI])",
    re.I,
)
_NXN = re.compile(_L + r"(\d{1,2})x(\d{2,3})(?![0-9pi])", re.I)
_NXN_CONT = re.compile(
    r"(?:(?P<sep>[-_]?)(?:\d{1,2})?x(?P<e>\d{2,3})|(?P<dash>-)(?P<e2>\d{2,3}))(?!\d)"
)
_SEASON_EP_WORDS = re.compile(
    _L + r"Season[\s._-]?(\d{1,2})[\s._-]*(?:Episode|Ep)[\s._-]?(\d{1,3})" + _R, re.I
)
_DAILY_YMD = re.compile(r"(?<![0-9A-Za-z])((?:19|20)\d{2})[\s._-](\d{2})[\s._-](\d{2})(?![0-9])")
_DAILY_DMY = re.compile(r"(?<![0-9A-Za-z])(\d{2})[\s._-](\d{2})[\s._-]((?:19|20)\d{2})(?![0-9])")
_SEASON_RANGE = re.compile(_L + r"S(\d{1,2})(?:(?:-|[\s._](?:to|&)[\s._])S?(\d{1,2}))?" + _R, re.I)
_SEASON_WORDS = re.compile(
    _L + r"Seasons?[\s._-]?(\d{1,2})"
    r"(?:[\s._]?(?:-|to|&|and)[\s._]?(?:Seasons?[\s._-]?)?(\d{1,2}))?(?![0-9])",
    re.I,
)
_SEASON_ORDINAL = re.compile(_L + r"(\d{1,2})(?:st|nd|rd|th)[\s._-]Season" + _R, re.I)
_COMPLETE_SERIES = re.compile(
    _L + r"(?:The[\s._-])?Complete[\s._-](?:Series|Box[\s._-]?Set)" + _R, re.I
)
_ABS_DASH = re.compile(
    "\\s[-\u2013]\\s"
    r"(?:E|EP|#)?(\d{1,4})(?:v(\d))?(?:\s?[-~]\s?(\d{1,4})(?:v\d)?)?(?=\s|$|[\[(])",
    re.I,
)
_ABS_BATCH = re.compile(r"[\[(]\s?(\d{1,4})\s?[-~]\s?(\d{1,4})\s?[\])]")
_ABS_BARE = re.compile(r"(?<=\s)(\d{2,4})(?:v(\d))?(?=\s*[\[(]|\s*$)")
_E_ONLY = re.compile(
    _L + r"(?:E|EP|Episode[\s._-]?)(\d{1,4})(?:v(\d))?(?:[-~](?:E|EP)?(\d{1,4}))?" + _R, re.I
)


def _span(a: int, b: int) -> list[int]:
    return list(range(a, b + 1)) if a <= b and b - a <= 200 else [a]


def _continue(body: str, pos: int, first: int, cont: re.Pattern[str]) -> tuple[list[int], int]:
    eps, end = [first], pos
    while c := cont.match(body, end):
        n = int(c.group("e") or c.group("e2"))
        prev = eps[-1]
        if n <= prev:
            break
        is_range = (c.group("sep") or c.group("dash") or "") == "-"
        if is_range and n - prev > 50:
            break
        eps.extend(range(prev + 1, n + 1) if is_range else [n])
        end = c.end()
    return eps, end


def _valid_date(y: int, m: int, d: int) -> date | None:
    try:
        out = date(y, m, d)
    except ValueError:
        return None
    return out if out.year <= _MAX_YEAR else None


def _find_episode(body: str, anime: bool) -> _Ep | None:
    if (m := _SXE.search(body)) and m.start() > 0:
        eps, end = _continue(body, m.end(), int(m.group(2)), _SXE_CONT)
        ep = _Ep(m.start(), end)
        ep.seasons, ep.episodes = [int(m.group(1))], eps
        return ep
    if (m := _NXN.search(body)) and m.start() > 0:
        eps, end = _continue(body, m.end(), int(m.group(2)), _NXN_CONT)
        ep = _Ep(m.start(), end)
        ep.seasons, ep.episodes = [int(m.group(1))], eps
        return ep
    if (m := _SEASON_EP_WORDS.search(body)) and m.start() > 0:
        ep = _Ep(m.start(), m.end())
        ep.seasons, ep.episodes = [int(m.group(1))], [int(m.group(2))]
        return ep
    for rx, order in ((_DAILY_YMD, (1, 2, 3)), (_DAILY_DMY, (3, 2, 1))):
        for m in rx.finditer(body):
            if m.start() == 0:
                continue
            y, mo, d = (int(m.group(i)) for i in order)
            if when := _valid_date(y, mo, d):
                ep = _Ep(m.start(), m.end())
                ep.air_date = when
                return ep

    season: _Ep | None = None
    for rx in (_SEASON_RANGE, _SEASON_WORDS, _SEASON_ORDINAL):
        m = rx.search(body)
        if m and m.start() > 0 and (season is None or m.start() < season.start):
            a = int(m.group(1))
            b = int(m.group(2)) if m.lastindex and m.lastindex >= 2 and m.group(2) else a
            season = _Ep(m.start(), m.end())
            season.seasons = _span(a, b) if b >= a else [a]
    complete = _COMPLETE_SERIES.search(body)
    if complete and complete.start() == 0:
        complete = None

    absolute = _find_absolute(body, anime, after=season.end if season else 0)
    if season and absolute and absolute.start >= season.end:
        # "[Group] Show S2 - 05": an episode within the season, not an absolute number.
        season.episodes, season.end = absolute.absolute, absolute.end
        season.version = absolute.version
        return season
    if season:
        season.complete = complete is not None
        return season
    if absolute:
        return absolute
    if complete:
        ep = _Ep(complete.start(), complete.end())
        ep.complete = True
        return ep
    return None


def _find_absolute(body: str, anime: bool, *, after: int) -> _Ep | None:
    candidates: list[tuple[re.Pattern[str], bool]] = [(_E_ONLY, True)]
    if anime:
        candidates = [(_ABS_DASH, True), (_ABS_BATCH, False), (_ABS_BARE, True), *candidates]
    for rx, versioned in candidates:
        for m in rx.finditer(body, after):
            if m.start() == 0:
                continue
            a = int(m.group(1))
            if rx is _ABS_BARE and 1900 <= a <= _MAX_YEAR:
                continue  # a year, not episode 2023
            b_group = 3 if versioned else 2
            b = m.group(b_group) if rx.groups >= b_group else None
            ep = _Ep(m.start(), m.end())
            ep.absolute = _span(a, int(b)) if b else [a]
            if versioned and m.group(2):
                ep.version = int(m.group(2))
            return ep
    return None


# ---- step 3: title end --------------------------------------------------------------------

_YEAR = re.compile(r"(?<![0-9A-Za-z])((?:19|20)\d{2})(?![0-9A-Za-z])")
_STRONG = re.compile(
    _L + r"(?:\d{3,4}[pi]|\d{3,4}x\d{3,4}|4K|UHD|Blu-?Ray|BD-?Rip|BR-?Rip|BDRemux"
    r"|BD(?:25|50|66|100)|BDMV"
    r"|WEB-?DL|WEB-?Rip|WEBDL|WEB-?Cap|HDTV|HDTVRip|PDTV|SDTV|DSR|TVRip|DVD-?Rip|DVD[59R]?"
    r"|HDRip|HDCAM|CAMRip|TELESYNC|TELECINE|HD-?TS|HD-?TC|DVDSCR|REMUX"
    r"|[xh][\s.]?26[45]|HEVC|XviD|DivX|AV1|10-?bit|TrueHD|DTS(?:-HD)?|AAC(?:\d\.?\d)?"
    r"|DDP?\+?\d\.?\d|HDR10\+?|HDR|Dolby[\s._-]?Vision)" + _R,
    re.I,
)
_WEAK = re.compile(
    _L + r"(?:PROPER|REPACK|RERIP|INTERNAL|iNTERNAL|LIMITED|UNRATED|EXTENDED|REMASTERED|IMAX"
    r"|UNCUT|MULTi|MULTI|FRENCH|TRUEFRENCH|GERMAN|ITALIAN|iTALiAN|SPANISH|VOSTFR|SUBFRENCH"
    r"|DUBBED|SUBBED|CAM|TS|TC|SCR|R5|R6|WORKPRINT|READNFO|NFOFIX|DOCU|WEB|DUAL"
    r"|DIRECTORS[\s._]CUT|Directors[\s._]Cut|Director'?s[\s._]Cut|3D|HSBS|H-SBS|COMPLETE)" + _R
)


def _title_end(body: str, ep: _Ep | None) -> tuple[int, int | None]:
    """Return (title_end, year)."""
    n = len(body)
    blocked = (ep.start, ep.end) if ep else (n, n)
    years = [
        m
        for m in _YEAR.finditer(body)
        if m.start() > 0
        and int(m.group(1)) <= _MAX_YEAR
        and not (blocked[0] <= m.start() < blocked[1])
    ]
    strong = next((m.start() for m in _STRONG.finditer(body) if m.start() > 0), n)
    hard = min(ep.start if ep else n, strong)
    weak = n
    if not body.isupper():
        weak = next((m.start() for m in _WEAK.finditer(body) if m.start() > 0), n)

    before = [y for y in years if y.start() < hard]
    if before:
        y = before[-1]
        return min(y.start(), weak), int(y.group(1))
    end = min(hard, weak)
    after = next((y for y in years if y.start() >= end), None)
    return end, int(after.group(1)) if after else None


_ACRONYM = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]\.){2,}(?:[A-Za-z](?![A-Za-z0-9]))?")
_AKA = re.compile(r"\s+(?:a\.?k\.?a\.?|aka)\s+", re.I)


def _display_title(raw: str) -> tuple[str, list[str]]:
    t = raw.replace("_", " ")
    if " " not in raw.strip():

        def protect(m: re.Match[str]) -> str:
            s = m.group(0)
            follows = m.end() < len(t) and t[m.end()].isalnum()
            return s.replace(".", "\0") + (" " if follows and s.endswith(".") else "")

        t = _ACRONYM.sub(protect, t).replace(".", " ").replace("\0", ".")
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub("[\\s\\-\u2013_\\[({:,]+$", "", t)
    if t.endswith(".") and not re.search(r"(?:[A-Za-z]\.){2,}$", t):
        t = t.rstrip(".").rstrip()
    t = re.sub("^[\\s\\-\u2013_:]+", "", t)
    parts = [p.strip(" -") for p in _AKA.split(t) if p.strip(" -")]
    return (parts[0] if parts else t), parts[1:]


# ---- step 4: quality and the rest ---------------------------------------------------------

_RES_EXPLICIT: list[tuple[re.Pattern[str], int]] = [
    (_rx(r"2160[pi]|3840x2160|4096x2160"), 2160),
    (_rx(r"1080[pi]|1920x10[78]0|FHD"), 1080),
    (_rx(r"720[pi]|1280x720"), 720),
    (_rx(r"576[pi]"), 576),
    (_rx(r"480[pi]|640x480|8[45]\dx480"), 480),
    (_rx(r"360p"), 360),
]
_RES_HINT = _rx(r"4K|UHD")

_SOURCES: list[tuple[re.Pattern[str], str | None, str | None]] = [
    # (pattern, source, modifier) in masking order: specific before generic.
    (_rx(r"(?:(?:UHD|BD|Blu-?Ray)[\s._-]?)?Remux"), None, "remux"),
    (
        _rx(
            r"BDMV|BD(?:25|50|66|100)|COMPLETE[\s._-](?:UHD[\s._-])?BLU-?RAY"
            r"|(?:UHD[\s._-])?BLU-?RAY[\s._-]COMPLETE"
        ),
        "bluray",
        "brdisk",
    ),
    (_rx(r"Raw[\s._-]?HD"), "hdtv", "rawhd"),
    (_rx(r"(?:DVD|BD|WEB)[\s._-]?SCR(?:EENER)?|SCREENER"), "screener", None),
    (_rx(r"SCR", cs=True), "screener", None),
    (_rx(r"WEB-?DL-?Rip|WEB-?Rip|WEB-?Cap|HDRip|WEBMux"), "webrip", None),
    (_rx(r"WEB[\s._-]?DL|WEBDL|AmazonHD|iTunesHD|NetflixHD"), "webdl", None),
    (_rx(r"WEB"), "webdl", None),
    (
        _rx(r"Blu-?Ray(?:[\s._-]?Rip)?|BD-?Rip|BR-?Rip|UHD[\s._-]?BD|BD(?=[\s._-]\d{3,4}p)"),
        "bluray",
        None,
    ),
    (_rx(r"HDTV(?:Rip)?|UHDTV"), "hdtv", None),
    (_rx(r"PDTV|SDTV|DSR(?:ip)?|DTHRip|DVBRip|TVRip|SATRip"), "sdtv", None),
    (_rx(r"DVD(?:[\s._-]?(?:Rip|R|5|9|Mux|Full))?|NTSC|PAL|VIDEO_TS"), "dvd", None),
    (_rx(r"R5|R6"), "regional", None),
    (_rx(r"TELECINE|HD[\s._-]?TC"), "telecine", None),
    (_rx(r"TC", cs=True), "telecine", None),
    (_rx(r"TELESYNC|HD[\s._-]?TS|TS[\s._-]?Rip|PDVD|PreDVDRip"), "telesync", None),
    (_rx(r"TS", cs=True), "telesync", None),
    (_rx(r"CAM-?Rip|HD[\s._-]?CAM|HQ[\s._-]?CAM|NEWCAM"), "cam", None),
    (_rx(r"CAM", cs=True), "cam", None),
    (_rx(r"WORKPRINT"), "workprint", None),
    (_rx(r"WP", cs=True), "workprint", None),
]  # fmt: skip

_CODECS: list[tuple[re.Pattern[str], str]] = [
    (_rx(r"[xh][\s.]?265|HEVC"), "h265"),
    (_rx(r"[xh][\s.]?264|AVC|Hi10P?"), "h264"),
    (_rx(r"AV1"), "av1"),
    (_rx(r"VC-?1"), "vc1"),
    (_rx(r"MPEG-?2"), "mpeg2"),
    (_rx(r"XviD"), "xvid"),
    (_rx(r"DivX"), "divx"),
    (_rx(r"VP9"), "vp9"),
]
_UNTOUCHED = _rx(r"AVC|VC-?1|MPEG-?2")
_ENCODER = _rx(r"[xh][\s.]?26[45]|XviD|DivX|Rip")
_BIT_DEPTH = [
    (_rx(r"10[\s.-]?bits?|Hi10P?"), 10),
    (_rx(r"12[\s.-]?bits?"), 12),
    (_rx(r"8[\s.-]?bits?"), 8),
]

_HDR: list[tuple[re.Pattern[str], str]] = [
    (_rx(r"DV|DoVi|Dolby[\s._-]?Vision"), "DV"),
    (_rx(r"HDR10(?:\+|Plus|P)(?![A-Za-z0-9])|HDR10\+"), "HDR10+"),
    (_rx(r"HDR10|HDR"), "HDR10"),
    (_rx(r"HLG"), "HLG"),
]
_HDR10P = re.compile(r"(?<![A-Za-z0-9])HDR10\+", re.I)  # "+" defeats the word boundary

_CH = r"(?:[\s._-]?(?P<ch>[1-9][\s._]?[0-2]))?"
_AUDIO: list[tuple[re.Pattern[str], str]] = [
    (_rx(r"TrueHD(?:[\s._-]?Atmos)?" + _CH), "truehd"),
    (_rx(r"DTS[\s._:-]?X" + _CH), "dts-x"),
    (_rx(r"(?:DTS[\s._-]?HD[\s._-]?MA|DTS[\s._-]?MA|DTSHD[\s._-]?MA)" + _CH), "dts-hd ma"),
    (_rx(r"DTS[\s._-]?HD[\s._-]?(?:HRA|HR)" + _CH), "dts-hd hra"),
    (_rx(r"DTS[\s._-]?HD" + _CH), "dts-hd ma"),
    (_rx(r"DTS[\s._-]?ES" + _CH), "dts-es"),
    (_rx(r"DTS" + _CH), "dts"),
    (_rx(r"(?:DDPA|DDP|DD\+|E-?AC-?3)" + _CH), "eac3"),
    (_rx(r"(?:DD|AC-?3|Dolby[\s._-]?Digital)" + _CH), "ac3"),
    (_rx(r"(?:HE-?)?AAC(?:-?LC)?" + _CH), "aac"),
    (_rx(r"FLAC" + _CH), "flac"),
    (_rx(r"OPUS" + _CH), "opus"),
    (_rx(r"MP3"), "mp3"),
    (_rx(r"L?PCM" + _CH), "pcm"),
]
_DDP_PLUS = re.compile(r"(?<![A-Za-z0-9])DD\+(?:[\s._-]?(?P<ch>[1-9][\s._]?[0-2]))?(?![0-9])", re.I)
_ATMOS = re.compile(r"(?<![A-Za-z0-9])(?:Atmos(?![A-Za-z0-9])|DDPA)", re.I)
_CHANNELS = re.compile(r"(?<![0-9A-Za-z])([1-9])[.]([0-2])(?![0-9])")

_SERVICES = {
    "AMZN": "AMZN", "NF": "NF", "DSNP": "DSNP", "DSPN": "DSNP", "HMAX": "HMAX", "MAX": "MAX",
    "ATVP": "ATVP", "ATV": "ATVP", "HULU": "HULU", "PCOK": "PCOK", "PMTP": "PMTP",
    "iT": "iT", "STAN": "STAN", "CRAV": "CRAV", "CR": "CR", "FUNI": "FUNI", "HIDI": "HIDIVE",
    "HIDIVE": "HIDIVE", "ROKU": "ROKU", "BCORE": "BCORE", "MUBI": "MUBI", "DSCP": "DSCP",
    "AMCP": "AMCP", "SHO": "SHO", "STZ": "STZ", "iP": "iP", "ALL4": "ALL4", "ITVX": "ITVX",
    "NOW": "NOW", "SKST": "SKST", "CRKL": "CRKL", "TVNZ": "TVNZ", "CBC": "CBC", "RED": "RED",
    "VMEO": "VMEO", "PLAY": "PLAY", "APTN": "APTN", "BNGE": "BNGE", "FOXP": "FOXP",
    "MA": "MA", "VUDU": "VUDU", "TUBI": "TUBI", "PLUZ": "PLUZ", "NICK": "NICK", "CC": "CC",
    "AUBC": "AUBC", "SBS": "SBS", "TEN": "TEN", "9NOW": "9NOW", "7PLUS": "7PLUS",
    "ZEE5": "ZEE5", "HS": "HS", "JC": "JC", "VIKI": "VIKI", "KCW": "KCW", "WAVVE": "WAVVE",
}  # fmt: skip
_SERVICE = re.compile(
    _L
    + r"("
    + "|".join(sorted(map(re.escape, _SERVICES), key=len, reverse=True))
    + r")[\s._-]+(?=WEB|Web|web)"
)
_ITUNES = _rx(r"iTunes")

_EDITIONS: list[tuple[re.Pattern[str], str]] = [
    (_rx(r"Director'?s?[\s._-]?Cut|DC(?=[\s._-]\d{3,4}p)"), "Director's Cut"),
    (_rx(r"Extended[\s._-]?(?:Cut|Edition|Version)?"), "Extended"),
    (_rx(r"Theatrical(?:[\s._-]?(?:Cut|Edition|Version))?"), "Theatrical"),
    (_rx(r"Unrated"), "Unrated"),
    (_rx(r"Uncut"), "Uncut"),
    (_rx(r"Uncensored"), "Uncensored"),
    (_rx(r"IMAX(?:[\s._-]?Enhanced)?"), "IMAX"),
    (_rx(r"(?:4K[\s._-]?)?Remaster(?:ed)?"), "Remastered"),
    (_rx(r"Criterion(?:[\s._-]?Collection)?|CC(?=[\s._-]\d{3,4}p)"), "Criterion"),
    (_rx(r"Final[\s._-]?Cut"), "Final Cut"),
    (_rx(r"Ultimate[\s._-]?(?:Cut|Edition)"), "Ultimate"),
    (_rx(r"Special[\s._-]?Edition"), "Special Edition"),
    (_rx(r"Collector'?s?[\s._-]?Edition"), "Collector's Edition"),
    (_rx(r"(\d{1,3}(?:th|st|nd|rd))[\s._-]?Anniversary(?:[\s._-]?Edition)?"), "{0} Anniversary"),
    (_rx(r"Anniversary[\s._-]?Edition"), "Anniversary Edition"),
    (_rx(r"Open[\s._-]?Matte"), "Open Matte"),
    (_rx(r"Redux"), "Redux"),
    (_rx(r"Despecialized"), "Despecialized"),
    (_rx(r"Rogue[\s._-]?Cut"), "Rogue Cut"),
    (_rx(r"Black[\s._-]?and[\s._-]?Chrome"), "Black and Chrome"),
]

_REVISION = _rx(r"(PROPER|REPACK|RERIP)(\d)?")
_REAL = _rx(r"REAL", cs=True)
_VERSION = re.compile(r"(?<![A-Za-z0-9])v([2-9])(?![0-9A-Za-z])")

_HARDSUB = _rx(r"HC|HC[\s._-]?SUBS?|KORSUBS?|HARD[\s._-]?SUB(?:S|BED)?|HARDCODED(?:[\s._-]SUBS?)?")
_KORSUB = _rx(r"KORSUBS?")
_SUB_FR = _rx(r"VOSTFR|VOST|SUBFRENCH|STFR")

_LANGS: list[tuple[str, str]] = [
    (r"ENGLISH|ENG", "en"),
    (r"TRUEFRENCH|FRENCH|VFF|VFQ|VFI|VF2|VF|FRE", "fr"),
    (r"GERMAN|GER|DEU", "de"),
    (r"ITALIAN|ITA", "it"),
    (r"SPANISH|ESP|CASTELLANO|LATINO|LATAM", "es"),
    (r"PORTUGUESE|POR|PT-BR|PTBR|DUBLADO|NACIONAL|BRAZILIAN", "pt"),
    (r"RUSSIAN|RUS", "ru"),
    (r"UKRAINIAN|UKR", "uk"),
    (r"JAPANESE|JAP|JPN", "ja"),
    (r"KOREAN|KOR", "ko"),
    (r"CHINESE|MANDARIN|CANTONESE|CHI|CHS|CHT", "zh"),
    (r"HINDI|HIN", "hi"),
    (r"TAMIL|TAM", "ta"),
    (r"TELUGU|TEL", "te"),
    (r"MALAYALAM|MAL", "ml"),
    (r"KANNADA|KAN", "kn"),
    (r"BENGALI", "bn"),
    (r"POLISH|PL|PLDUB", "pl"),
    (r"DUTCH|FLEMISH|NL", "nl"),
    (r"SWEDISH|SWE", "sv"),
    (r"NORWEGIAN|NOR", "no"),
    (r"DANISH|DAN", "da"),
    (r"FINNISH|FIN", "fi"),
    (r"TURKISH|TUR", "tr"),
    (r"ARABIC|ARA", "ar"),
    (r"HEBREW|HEB", "he"),
    (r"THAI", "th"),
    (r"VIETNAMESE|VIE", "vi"),
    (r"CZECH|CZ", "cs"),
    (r"HUNGARIAN|HUN", "hu"),
    (r"GREEK|GRE", "el"),
    (r"ROMANIAN|ROM", "ro"),
    (r"INDONESIAN|IND", "id"),
    (r"PERSIAN|FARSI", "fa"),
]
_LANG_ALT = "|".join(p for p, _ in _LANGS)
_LANG_RX = [(_rx(p), code) for p, code in _LANGS]
_LANG_SUBS = _rx(rf"(?:{_LANG_ALT}|NORDiC|NORDIC)[\s._-]?(?:SUBS?|SUBBED|SUBTITLES?)")
_SUBS_LANG = _rx(rf"SUBS?[\s._-]?(?:{_LANG_ALT})")
_MULTI_SUBS = _rx(r"MULTi[\s._-]?SUBS?")
_MULTI = _rx(r"MULTi(?:[\s._-]?(?:LANG|AUDIO))?|DUAL[\s._-]?AUDIO|DUAL")
_NORDIC = _rx(r"NORDiC|NORDIC")
_DL = _rx(r"DL", cs=True)
_DUBBED = _rx(r"DUBBED|DUB")
_SUBBED = _rx(r"SUBBED")

_THREE_D = _rx(r"3D|H-?SBS|Half-?SBS|H-?OU|Half-?OU|SBS|HSBS|HOU")
_HYBRID = _rx(r"Hybrid")
_INTERNAL = _rx(r"INTERNAL|iNTERNAL|iNT", cs=True)
_LIMITED = _rx(r"LIMITED", cs=True)
_SAMPLE = _rx(r"sample")


def _lang_codes(text: str) -> list[str]:
    return [code for rx, code in _LANG_RX if rx.search(text)]


def _uniq(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _quality(rest: str, extension: str | None, out: dict[str, object]) -> None:
    t = _Text(rest)

    hdr: list[str] = []
    if t.take(_HDR10P):
        hdr.append("HDR10+")
    for rx, name in _HDR:
        if t.take(rx):
            hdr.append(name)
    out["hdr"] = _uniq(sorted(hdr, key=["DV", "HDR10+", "HDR10", "HLG"].index))

    audio: list[tuple[int, str]] = []
    channels: str | None = None
    atmos = bool(_ATMOS.search(t.s))
    for m in t.take(_DDP_PLUS):
        audio.append((m.start(), "eac3"))
        channels = channels or m.group("ch")
    for rx, name in _AUDIO:
        for m in t.take(rx):
            audio.append((m.start(), name))
            channels = channels or m.groupdict().get("ch")
    t.take(_ATMOS)
    if channels is None and (cm := _CHANNELS.search(t.s)):
        channels = f"{cm.group(1)}.{cm.group(2)}"
        t.take(_CHANNELS)
    if channels and "." not in channels:
        digits = re.sub(r"\D", "", channels)
        channels = f"{digits[0]}.{digits[1]}"
    elif channels:
        channels = re.sub(r"[\s_]", ".", channels)
    out["audio_codecs"] = _uniq([name for _, name in sorted(audio)])
    out["audio_channels"] = channels
    out["atmos"] = atmos

    untouched = bool(_UNTOUCHED.search(t.s))
    encoded = bool(_ENCODER.search(t.s))

    if sm := t.first(_SERVICE):
        out["streaming_service"] = _SERVICES[sm.group(1)]
    elif _ITUNES.search(t.s):
        out["streaming_service"] = "iT"

    sources: list[tuple[int, str]] = []
    modifier: str | None = None
    for rx, source, mod in _SOURCES:
        for m in t.take(rx):
            if mod and modifier is None:
                modifier = mod
            if source:
                sources.append((m.start(), source))
    source = max(sources)[1] if sources else None
    if modifier == "remux" and source not in ("dvd",):
        source = "bluray"
    if modifier == "brdisk":
        source = "bluray"
    if source == "bluray" and modifier is None and untouched and not encoded:
        modifier = "brdisk"
    if extension == "iso" and source in ("bluray", None):
        source, modifier = "bluray", "brdisk"

    resolution: int | None = None
    for rx, res in _RES_EXPLICIT:
        if t.take(rx):
            resolution = resolution or res
    if resolution is None and _RES_HINT.search(t.s):
        resolution = 2160
    if source == "sdtv" and resolution and resolution >= 720:
        source = "hdtv"

    out["source"], out["modifier"], out["resolution"] = source, modifier, resolution

    for rx, depth in _BIT_DEPTH:
        if rx.search(t.s):
            out["bit_depth"] = depth
            break
    codec: tuple[int, str] | None = None
    for rx, name in _CODECS:
        for m in t.take(rx):
            if codec is None or m.start() < codec[0]:
                codec = (m.start(), name)
    out["video_codec"] = codec[1] if codec else None

    editions: list[str] = []
    for rx, name in _EDITIONS:
        for m in t.take(rx):
            editions.append(name.format(m.group(1)) if "{0}" in name else name)
    out["edition"] = " ".join(_uniq(editions)) or None

    version, proper, repack = 1, False, False
    for m in t.take(_REVISION):
        kind = m.group(1).upper()
        proper |= kind == "PROPER"
        repack |= kind in ("REPACK", "RERIP")
        version += int(m.group(2)) if m.group(2) else 1
    if vm := _VERSION.search(t.s):
        version = max(version, int(vm.group(1)))
    out["revision"] = Revision(version=version, real=len(t.take(_REAL)))
    out["proper"], out["repack"] = proper, repack

    korsub = bool(_KORSUB.search(t.s))
    out["hardcoded_subs"] = bool(t.take(_HARDSUB))
    subs: list[str] = ["ko"] if korsub else []
    if t.take(_SUB_FR):
        subs.append("fr")
    for m in t.take(_LANG_SUBS) + t.take(_SUBS_LANG):
        if _NORDIC.search(m.group(0)):
            subs += ["da", "fi", "no", "sv"]
        else:
            subs += _lang_codes(re.sub(r"(?i)subs?|subbed|subtitles?", " ", m.group(0)))
    t.take(_MULTI_SUBS)
    out["subtitle_languages"] = _uniq(subs)

    multi = bool(t.take(_MULTI))
    langs: list[tuple[int, str]] = []
    for rx, code in _LANG_RX:
        for m in t.take(rx):
            langs.append((m.start(), code))
    if t.take(_NORDIC):
        multi = True
    codes = _uniq([c for _, c in sorted(langs)])
    if "de" in codes and t.take(_DL):
        multi = True
    out["languages"] = codes
    out["multi_language"] = multi or len(codes) > 1
    out["dubbed"] = bool(t.take(_DUBBED))
    t.take(_SUBBED)

    out["three_d"] = bool(t.take(_THREE_D))
    out["hybrid"] = bool(t.take(_HYBRID))
    out["internal"] = bool(t.take(_INTERNAL))
    out["limited"] = bool(t.take(_LIMITED))
    out["sample"] = bool(t.take(_SAMPLE))


# ---- entry point --------------------------------------------------------------------------


def parse_release(name: str) -> ParsedRelease:
    try:
        return _parse(name)
    except Exception:  # pragma: no cover - the parser must never break a search
        return ParsedRelease(raw=name, title=name.strip())


def _parse(name: str) -> ParsedRelease:
    s = unicodedata.normalize("NFC", name).strip().replace("\u2013", "-").replace("\u2014", "-")
    extension = None
    s = _PARTIAL.sub("", s)
    if m := _EXT.search(s):
        extension = m.group(1).lower()
        s = s[: m.start()]
    s = _SITE_PREFIX.sub("", s, count=1)
    s, crc, site_group = _strip_trailing_tags(s)

    lead_group = None
    if (m := _LEAD_GROUP.match(s)) and not _QUALITYISH.search(m.group(1)):
        lead_group = m.group(1).strip()
        s = s[m.end() :]
    s = _JUNK_SUFFIX.sub("", s).rstrip(" -_")
    anime = lead_group is not None or crc is not None

    body, group = s, None
    gm = _GROUP.search(s)
    if not (gm and _valid_group(gm.group(1))):
        gm = _NODASH_GROUP.search(s)
    if gm:
        body, group = s[: gm.start()], gm.group(1)

    ep = _find_episode(body, anime)
    end, year = _title_end(body, ep)
    if group and end >= len(body.rstrip(" ._-")) and not ep and year is None:
        # Nothing but a title: "Spider-Man" has no group.
        body, group = s, None
        ep = _find_episode(body, anime)
        end, year = _title_end(body, ep)

    title, alt = _display_title(body[:end])
    if not title:
        title, alt = _display_title(body)
    # Nothing after the title (e.g. the whole name is "1080p"): read quality from all of it.
    rest = body[end:] if end < len(body.rstrip(" ._-")) else body

    out: dict[str, object] = {}
    _quality(rest, extension, out)
    revision = out.pop("revision")
    assert isinstance(revision, Revision)
    if ep and ep.version:
        revision = Revision(version=max(revision.version, ep.version), real=revision.real)

    return ParsedRelease.model_validate(
        {
            **out,
            "raw": name,
            "title": title,
            "alt_titles": alt,
            "year": year,
            "seasons": ep.seasons if ep else [],
            "episodes": ep.episodes if ep else [],
            "absolute_episodes": ep.absolute if ep else [],
            "air_date": ep.air_date if ep else None,
            "complete_series": ep.complete if ep else False,
            "group": lead_group or group or site_group,
            "revision": revision,
            "hash": crc,
            "extension": extension,
        }
    )
