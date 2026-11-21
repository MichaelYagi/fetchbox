"""Read what a video file really is: resolution, codecs, HDR, duration, languages.

Pure Python, no ffprobe or MediaInfo to install: parses the container headers of
Matroska/WebM (EBML) and MP4/MOV (ISO BMFF), the formats releases come in. Only
headers are read (a few MB at most), never the media data. Anything else, or a damaged
file, gives ``None``; the import then relies on the release name alone.

Values use the release parser's vocabulary (``h265``, ``eac3``, ``HDR10``...), so the
two can be compared and either can fill a naming token.
"""

from __future__ import annotations

import struct
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

MAX_HEADER = 16 * 1024 * 1024  # Matroska: Tracks come before the first Cluster

_MKV_VIDEO = {
    "V_MPEG4/ISO/AVC": "h264", "V_MPEGH/ISO/HEVC": "h265", "V_AV1": "av1", "V_VP9": "vp9",
    "V_MPEG2": "mpeg2", "V_MS/VFW/FOURCC": "xvid", "V_MPEG4/ISO/ASP": "xvid", "V_VP8": "vp8",
}  # fmt: skip
_MKV_AUDIO = {
    "A_EAC3": "eac3", "A_AC3": "ac3", "A_DTS": "dts", "A_TRUEHD": "truehd", "A_AAC": "aac",
    "A_OPUS": "opus", "A_FLAC": "flac", "A_MPEG/L3": "mp3", "A_PCM/INT/LIT": "pcm",
    "A_VORBIS": "vorbis",
}  # fmt: skip
_MP4_VIDEO = {
    b"avc1": "h264", b"avc3": "h264", b"hvc1": "h265", b"hev1": "h265", b"dvh1": "h265",
    b"dvhe": "h265", b"av01": "av1", b"vp09": "vp9", b"mp4v": "xvid",
}  # fmt: skip
_MP4_AUDIO = {
    b"mp4a": "aac", b"ac-3": "ac3", b"ec-3": "eac3", b"Opus": "opus", b"fLaC": "flac",
    b"dtsc": "dts", b"dtsh": "dts", b"dtsl": "dts", b"mlpa": "truehd",
}  # fmt: skip
_DV_BOXES = (b"dvcC", b"dvvC", b"dvwC")
_TRANSFER = {16: "HDR10", 18: "HLG"}  # ITU-T H.273 transfer characteristics
_CHANNELS = {1: "1.0", 2: "2.0", 3: "2.1", 6: "5.1", 7: "6.1", 8: "7.1"}


@dataclass
class MediaInfo:
    container: str
    width: int | None = None
    height: int | None = None
    video_codec: str | None = None
    hdr: list[str] = field(default_factory=list)
    duration_seconds: float | None = None
    audio_codecs: list[str] = field(default_factory=list)
    audio_channels: str | None = None  # of the first audio track
    audio_languages: list[str] = field(default_factory=list)
    subtitle_languages: list[str] = field(default_factory=list)

    @property
    def resolution(self) -> int | None:
        """The release-name resolution class: 2160, 1080, 720, 576 or 480."""
        if not self.width and not self.height:
            return None
        w, h = self.width or 0, self.height or 0
        # Widescreen crops shrink the height, so judge by width too.
        for res, min_w, min_h in ((2160, 3200, 1800), (1080, 1800, 900), (720, 1200, 600),
                                  (576, 1000, 540)):  # fmt: skip
            if w >= min_w or h >= min_h:
                return res
        return 480

    def to_json(self) -> dict[str, Any]:
        return {**asdict(self), "resolution": self.resolution}


def probe(path: str | Path) -> MediaInfo | None:
    try:
        with open(path, "rb") as f:
            head = f.read(12)
            f.seek(0)
            if head[:4] == b"\x1a\x45\xdf\xa3":
                return _mkv(f)
            if head[4:8] in (b"ftyp", b"moov", b"mdat", b"free", b"wide", b"skip"):
                return _mp4(f)
    except (OSError, struct.error, ValueError, IndexError):
        return None
    return None


# ---- Matroska ------------------------------------------------------------------------------------

_SEGMENT, _INFO, _TRACKS, _CLUSTER = 0x18538067, 0x1549A966, 0x1654AE6B, 0x1F43B675
_TRACK_ENTRY, _TRACK_TYPE, _CODEC_ID, _LANG, _LANG_BCP47 = 0xAE, 0x83, 0x86, 0x22B59C, 0x22B59D
_VIDEO, _AUDIO, _WIDTH, _HEIGHT, _CHANNELS_ID = 0xE0, 0xE1, 0xB0, 0xBA, 0x9F
_COLOUR, _TRANSFER_ID, _BLOCK_ADD_MAPPING, _BLOCK_ADD_TYPE = 0x55B0, 0x55BA, 0x41E4, 0x41E7
_TIMESCALE, _DURATION, _FLAG_DEFAULT = 0x2AD7B1, 0x4489, 0x88
_DV_FOURCCS = {int.from_bytes(b, "big") for b in _DV_BOXES}


def _vint(buf: bytes, pos: int, *, keep_marker: bool) -> tuple[int, int, bool]:
    """(value, new position, all-ones = unknown size)."""
    first = buf[pos]
    length = 1
    while length <= 8 and not first & (0x80 >> (length - 1)):
        length += 1
    if length > 8:
        raise ValueError("bad EBML vint")
    raw = buf[pos : pos + length]
    if len(raw) < length:
        raise ValueError("truncated")
    value = int.from_bytes(raw, "big")
    if not keep_marker:
        value &= (1 << (7 * length)) - 1
    unknown = not keep_marker and value == (1 << (7 * length)) - 1
    return value, pos + length, unknown


def _elements(buf: bytes, start: int, end: int) -> list[tuple[int, int, int]]:
    """(id, data start, data end) of the children in buf[start:end]."""
    out = []
    pos = start
    while pos < end - 1:
        try:
            eid, pos, _ = _vint(buf, pos, keep_marker=True)
            size, pos, unknown = _vint(buf, pos, keep_marker=False)
        except (ValueError, IndexError):
            break
        stop = end if unknown else min(pos + size, end)
        out.append((eid, pos, stop))
        if eid == _CLUSTER:
            break  # media data from here on
        pos = stop
    return out


def _uint(buf: bytes, s: int, e: int) -> int:
    return int.from_bytes(buf[s:e], "big")


def _str(buf: bytes, s: int, e: int) -> str:
    return buf[s:e].rstrip(b"\x00").decode("utf-8", "replace")


def _float(buf: bytes, s: int, e: int) -> float | None:
    if e - s == 4:
        return float(struct.unpack(">f", buf[s:e])[0])
    if e - s == 8:
        return float(struct.unpack(">d", buf[s:e])[0])
    return None


def _mkv(f: BinaryIO) -> MediaInfo | None:
    buf = f.read(MAX_HEADER)
    top = _elements(buf, 0, len(buf))
    seg = next(((s, e) for i, s, e in top if i == _SEGMENT), None)
    if seg is None:
        return None
    doctype = b"webm" in buf[:64]
    info = MediaInfo(container="webm" if doctype else "mkv")
    scale = 1_000_000
    duration = None
    for eid, s, e in _elements(buf, *seg):
        if eid == _INFO:
            for cid, cs, ce in _elements(buf, s, e):
                if cid == _TIMESCALE:
                    scale = _uint(buf, cs, ce)
                elif cid == _DURATION:
                    duration = _float(buf, cs, ce)
        elif eid == _TRACKS:
            for tid, ts, te in _elements(buf, s, e):
                if tid == _TRACK_ENTRY:
                    _mkv_track(buf, ts, te, info)
    if duration is not None:
        info.duration_seconds = round(duration * scale / 1e9, 3)
    return info


def _mkv_track(buf: bytes, s: int, e: int, info: MediaInfo) -> None:
    kind = codec = lang = None
    width = height = channels = transfer = None
    dv = False
    for eid, cs, ce in _elements(buf, s, e):
        if eid == _TRACK_TYPE:
            kind = _uint(buf, cs, ce)
        elif eid == _CODEC_ID:
            codec = _str(buf, cs, ce)
        elif eid == _LANG_BCP47 or (eid == _LANG and lang is None):
            lang = _str(buf, cs, ce)
        elif eid == _VIDEO:
            for vid, vs, ve in _elements(buf, cs, ce):
                if vid == _WIDTH:
                    width = _uint(buf, vs, ve)
                elif vid == _HEIGHT:
                    height = _uint(buf, vs, ve)
                elif vid == _COLOUR:
                    for kid, ks, ke in _elements(buf, vs, ve):
                        if kid == _TRANSFER_ID:
                            transfer = _uint(buf, ks, ke)
        elif eid == _AUDIO:
            for aid, as_, ae in _elements(buf, cs, ce):
                if aid == _CHANNELS_ID:
                    channels = _uint(buf, as_, ae)
        elif eid == _BLOCK_ADD_MAPPING:
            for bid, bs, be in _elements(buf, cs, ce):
                if bid == _BLOCK_ADD_TYPE and _uint(buf, bs, be) in _DV_FOURCCS:
                    dv = True
    lang = None if lang in (None, "", "und") else lang
    if kind == 1 and info.video_codec is None:
        info.video_codec = _MKV_VIDEO.get(codec or "", (codec or "").lower() or None)
        info.width, info.height = width, height
        info.hdr = _hdr(dv, transfer)
    elif kind == 2:
        name = _MKV_AUDIO.get(codec or "")
        if name is None and codec and codec.startswith("A_DTS"):
            name = "dts"
        if name and name not in info.audio_codecs:
            info.audio_codecs.append(name)
        if info.audio_channels is None and channels:
            info.audio_channels = _CHANNELS.get(channels, f"{channels}ch")
        if lang and lang not in info.audio_languages:
            info.audio_languages.append(lang)
    elif kind == 17 and lang and lang not in info.subtitle_languages:
        info.subtitle_languages.append(lang)


def _hdr(dv: bool, transfer: int | None) -> list[str]:
    out = ["DV"] if dv else []
    if transfer in _TRANSFER:
        out.append(_TRANSFER[transfer])
    return out


# ---- MP4 / MOV -----------------------------------------------------------------------------------

_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl"}


def _boxes(f: BinaryIO, start: int, end: int) -> list[tuple[bytes, int, int]]:
    """(type, payload start, payload end) at one level, by seeking past payloads."""
    out = []
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        head = f.read(8)
        if len(head) < 8:
            break
        size, kind = struct.unpack(">I4s", head)
        hdr = 8
        if size == 1:
            size = struct.unpack(">Q", f.read(8))[0]
            hdr = 16
        elif size == 0:
            size = end - pos
        if size < hdr:
            break
        out.append((kind, pos + hdr, min(pos + size, end)))
        pos += size
    return out


def _read(f: BinaryIO, s: int, e: int, cap: int = 1 << 20) -> bytes:
    f.seek(s)
    return f.read(min(e - s, cap))


def _mp4(f: BinaryIO) -> MediaInfo | None:
    f.seek(0, 2)
    size = f.tell()
    moov = next(((s, e) for k, s, e in _boxes(f, 0, size) if k == b"moov"), None)
    if moov is None:
        return None
    info = MediaInfo(container="mp4")
    for kind, s, e in _boxes(f, *moov):
        if kind == b"mvhd":
            d = _read(f, s, e, 64)
            if d[0] == 1:
                timescale, duration = struct.unpack(">IQ", d[20:32])
            else:
                timescale, duration = struct.unpack(">II", d[12:20])
            if timescale:
                info.duration_seconds = round(duration / timescale, 3)
        elif kind == b"trak":
            _mp4_track(f, s, e, info)
    return info


def _find(f: BinaryIO, s: int, e: int, path: list[bytes]) -> tuple[int, int] | None:
    for kind, cs, ce in _boxes(f, s, e):
        if kind == path[0]:
            return (cs, ce) if len(path) == 1 else _find(f, cs, ce, path[1:])
    return None


def _mp4_track(f: BinaryIO, s: int, e: int, info: MediaInfo) -> None:
    hdlr = _find(f, s, e, [b"mdia", b"hdlr"])
    handler = _read(f, *hdlr)[8:12] if hdlr else b""
    mdhd = _find(f, s, e, [b"mdia", b"mdhd"])
    lang = None
    if mdhd:
        d = _read(f, *mdhd, 64)
        off = 32 if d[0] == 1 else 20
        packed = struct.unpack(">H", d[off : off + 2])[0]
        code = "".join(chr(((packed >> sh) & 0x1F) + 0x60) for sh in (10, 5, 0))
        lang = None if code in ("und", "```") or not code.isalpha() else code
    stsd = _find(f, s, e, [b"mdia", b"minf", b"stbl", b"stsd"])
    if stsd is None:
        return
    entries = _boxes(f, stsd[0] + 8, stsd[1])  # skip version/flags + entry count
    if not entries:
        return
    fourcc, es, ee = entries[0]
    if handler == b"vide" and info.video_codec is None:
        d = _read(f, es, es + 78)
        info.video_codec = _MP4_VIDEO.get(fourcc, fourcc.decode("latin-1").strip().lower())
        info.width, info.height = struct.unpack(">HH", d[24:28])
        children = _boxes(f, es + 78, ee)
        dv = fourcc in (b"dvh1", b"dvhe") or any(k in _DV_BOXES for k, _, _ in children)
        transfer = None
        for k, cs, ce in children:
            if k == b"colr":
                c = _read(f, cs, ce, 16)
                if c[:4] in (b"nclx", b"nclc"):
                    transfer = struct.unpack(">H", c[6:8])[0]
        info.hdr = _hdr(dv, transfer)
    elif handler == b"soun":
        name = _MP4_AUDIO.get(fourcc)
        if name and name not in info.audio_codecs:
            info.audio_codecs.append(name)
        d = _read(f, es, es + 28)
        channels = struct.unpack(">H", d[16:18])[0]
        if info.audio_channels is None and channels and fourcc not in (b"ac-3", b"ec-3"):
            info.audio_channels = _CHANNELS.get(channels, f"{channels}ch")
        if lang and lang not in info.audio_languages:
            info.audio_languages.append(lang)
    elif handler in (b"sbtl", b"text", b"subt") and lang and lang not in info.subtitle_languages:
        info.subtitle_languages.append(lang)
