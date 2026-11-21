"""Reading resolution, codecs and HDR from Matroska and MP4 headers (files built here)."""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

from fetchbox.modules.library.probe import MediaInfo, probe
from fetchbox.modules.library.service import movie_context
from fetchbox.modules.releases.parser import parse_release

# ---- Matroska writer ----------------------------------------------------------------------------


def _id(eid: int) -> bytes:
    return eid.to_bytes((eid.bit_length() + 7) // 8, "big")


def _size(n: int) -> bytes:
    """EBML data size: L bytes, the length marker at bit 7L."""
    for length in range(1, 9):
        if n < (1 << (7 * length)) - 1:  # all ones means "unknown size"
            return (n | (1 << (7 * length))).to_bytes(length, "big")
    raise ValueError(n)


def el(eid: int, *children: bytes | int | str | float) -> bytes:
    body = b""
    for c in children:
        if isinstance(c, bytes):
            body += c
        elif isinstance(c, bool | int):
            body += c.to_bytes(max(1, (c.bit_length() + 7) // 8), "big")
        elif isinstance(c, float):
            body += struct.pack(">d", c)
        else:
            body += c.encode()
    return _id(eid) + _size(len(body)) + body


def mkv(*, dv: bool = True) -> bytes:
    ebml = el(0x1A45DFA3, el(0x4282, "matroska"))
    info = el(0x1549A966, el(0x2AD7B1, 1_000_000), el(0x4489, 5_400_000.0))
    video = el(
        0xAE,
        el(0x83, 1),
        el(0x86, "V_MPEGH/ISO/HEVC"),
        el(0xE0, el(0xB0, 3840), el(0xBA, 1600), el(0x55B0, el(0x55BA, 16))),
        el(0x41E4, el(0x41E7, int.from_bytes(b"dvcC", "big"))) if dv else b"",
    )
    audio = el(0xAE, el(0x83, 2), el(0x86, "A_EAC3"), el(0x22B59C, "eng"), el(0xE1, el(0x9F, 6)))
    audio2 = el(0xAE, el(0x83, 2), el(0x86, "A_DTS/LOSSLESS"), el(0x22B59C, "fre"))
    subs = el(0xAE, el(0x83, 17), el(0x86, "S_TEXT/UTF8"), el(0x22B59C, "spa"))
    tracks = el(0x1654AE6B, video, audio, audio2, subs)
    cluster = el(0x1F43B675, b"\x00" * 64)
    segment = _id(0x18538067) + b"\x01\xff\xff\xff\xff\xff\xff\xff" + info + tracks + cluster
    return ebml + segment


# ---- MP4 writer ---------------------------------------------------------------------------------


def box(kind: bytes, *payload: bytes) -> bytes:
    body = b"".join(payload)
    return struct.pack(">I4s", 8 + len(body), kind) + body


def _lang(code: str) -> bytes:
    packed = 0
    for ch in code:
        packed = (packed << 5) | (ord(ch) - 0x60)
    return struct.pack(">H", packed)


def _trak(handler: bytes, entry: bytes, lang: str = "und") -> bytes:
    mdhd = box(b"mdhd", b"\x00" * 4 + b"\x00" * 16 + _lang(lang) + b"\x00\x00")
    hdlr = box(b"hdlr", b"\x00" * 8 + handler + b"\x00" * 12)
    stsd = box(b"stsd", b"\x00" * 4 + struct.pack(">I", 1) + entry)
    return box(b"trak", box(b"mdia", mdhd, hdlr, box(b"minf", box(b"stbl", stsd))))


def mp4(*, moov_last: bool = False) -> bytes:
    mvhd = box(b"mvhd", b"\x00" * 12 + struct.pack(">II", 1000, 7_200_000) + b"\x00" * 80)
    colr = box(b"colr", b"nclx" + struct.pack(">HHH", 9, 18, 9) + b"\x00")
    visual = b"\x00" * 24 + struct.pack(">HH", 1920, 1080) + b"\x00" * 50
    video = _trak(b"vide", box(b"avc1", visual, box(b"avcC", b"\x01"), colr))
    sound = b"\x00" * 16 + struct.pack(">HH", 2, 16) + b"\x00" * 8
    audio = _trak(b"soun", box(b"mp4a", sound), "jpn")
    moov = box(b"moov", mvhd, video, audio)
    ftyp, mdat = box(b"ftyp", b"isom\x00\x00\x02\x00"), box(b"mdat", b"\x00" * 4096)
    return ftyp + (mdat + moov if moov_last else moov + mdat)


# ---- tests --------------------------------------------------------------------------------------


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_matroska(tmp_path: Path) -> None:
    info = probe(_write(tmp_path, "a.mkv", mkv()))
    assert info is not None
    assert info.container == "mkv" and (info.width, info.height) == (3840, 1600)
    assert info.resolution == 2160  # a scope crop is still 4K
    assert info.video_codec == "h265" and info.hdr == ["DV", "HDR10"]
    assert info.duration_seconds == 5400.0
    assert info.audio_codecs == ["eac3", "dts"] and info.audio_channels == "5.1"
    assert info.audio_languages == ["eng", "fre"] and info.subtitle_languages == ["spa"]
    plain = probe(_write(tmp_path, "b.mkv", mkv(dv=False)))
    assert plain is not None and plain.hdr == ["HDR10"]


def test_mp4_either_layout(tmp_path: Path) -> None:
    for moov_last in (False, True):
        info = probe(_write(tmp_path, f"a{moov_last}.mp4", mp4(moov_last=moov_last)))
        assert info is not None, moov_last
        assert info.container == "mp4" and (info.width, info.height) == (1920, 1080)
        assert info.resolution == 1080 and info.video_codec == "h264" and info.hdr == ["HLG"]
        assert info.duration_seconds == 7200.0
        assert info.audio_codecs == ["aac"] and info.audio_channels == "2.0"
        assert info.audio_languages == ["jpn"]


def test_unreadable_files(tmp_path: Path) -> None:
    assert probe(_write(tmp_path, "x.avi", b"RIFF....AVI LIST" + b"\x00" * 100)) is None
    assert probe(_write(tmp_path, "z.mp4", b"\x00\x00\x00\x08ftyp\xff\xff")) is None
    assert probe(tmp_path / "missing.mkv") is None
    # truncated or garbage headers: no crash, at most an empty result
    for name, data in (
        ("t.mkv", mkv()[:30]),
        ("g.mkv", b"\x1a\x45\xdf\xa3" + bytes(range(256)) * 8),
    ):
        info = probe(_write(tmp_path, name, data))
        assert info is None or info.video_codec is None


def test_resolution_classes() -> None:
    def res(w: int, h: int) -> int | None:
        return MediaInfo("mkv", width=w, height=h).resolution

    assert res(1920, 800) == 1080 and res(1280, 534) == 720 and res(720, 576) == 576
    assert res(640, 480) == 480 and res(3840, 2160) == 2160
    assert MediaInfo("mkv").resolution is None


def test_file_fills_what_the_name_leaves_out() -> None:
    class T:
        name, year, tmdb_id, imdb_id = "Night of the Living Dead", 1968, 10331, None

    t: Any = T()
    info = MediaInfo("mkv", width=1920, height=1040, video_codec="h265", hdr=["HDR10"],
                     audio_codecs=["ac3"], audio_channels="5.1")  # fmt: skip
    bare = parse_release("Night of the Living Dead 1968")
    ctx = movie_context(t, bare, "Unknown", info)
    assert ctx["Resolution"] == "1080p" and ctx["Codec"] == "x265" and ctx["HDR"] == ["HDR10"]
    assert ctx["Audio"] == "AC3 5.1"
    named = parse_release("Night.of.the.Living.Dead.1968.720p.BluRay.x264.DTS-GRP")
    ctx = movie_context(t, named, "Bluray-720p", info)
    assert ctx["Resolution"] == "720p" and ctx["Codec"] == "x264"  # the name wins
    assert ctx["Audio"].startswith("DTS")
