"""Built-in profiles and custom formats, created on first start.

They are ordinary rows afterwards: users can edit or delete them. Seeding happens only
while the respective table is empty, so edits survive restarts.
"""

from __future__ import annotations

from typing import Any

WEB_720 = ["WEBRip-720p", "WEBDL-720p"]
WEB_1080 = ["WEBRip-1080p", "WEBDL-1080p"]
WEB_2160 = ["WEBRip-2160p", "WEBDL-2160p"]

# Custom formats, keyed by a stable slug so profiles can refer to them before ids exist.
FORMATS: dict[str, dict[str, Any]] = {
    "hardcoded_subs": {
        "name": "Hardcoded subtitles",
        "default_score": -10000,
        "conditions": [{"type": "flag", "values": ["hardcoded_subs"]}],
    },
    "three_d": {
        "name": "3D",
        "default_score": -10000,
        "conditions": [{"type": "flag", "values": ["three_d"]}],
    },
    "dolby_vision": {
        "name": "Dolby Vision",
        "default_score": 0,
        "conditions": [{"type": "hdr", "values": ["DV"]}],
    },
    "hdr10_plus": {
        "name": "HDR10+",
        "default_score": 0,
        "conditions": [{"type": "hdr", "values": ["HDR10+"]}],
    },
    "atmos": {
        "name": "Atmos",
        "default_score": 5,
        "conditions": [{"type": "flag", "values": ["atmos"]}],
    },
    "lossless_audio": {
        "name": "Lossless audio",
        "default_score": 5,
        "conditions": [
            {"type": "audio_codec", "values": ["truehd", "dts-hd ma", "dts-x", "flac", "pcm"]}
        ],
    },
}

# format_scores here use FORMATS slugs; seeding maps them to ids.
PROFILES: list[dict[str, Any]] = [
    {
        "name": "Standard HD",
        "ladder": [
            {"name": "HDTV-720p", "qualities": ["HDTV-720p"]},
            {"name": "WEB 720p", "qualities": WEB_720},
            {"name": "Bluray-720p", "qualities": ["Bluray-720p"]},
            {"name": "HDTV-1080p", "qualities": ["HDTV-1080p"]},
            {"name": "WEB 1080p", "qualities": WEB_1080},
            {"name": "Bluray-1080p", "qualities": ["Bluray-1080p"]},
        ],
        "cutoff": "WEB 1080p",
        "format_scores": {},
    },
    {
        "name": "4K",
        "ladder": [
            {"name": "WEB 1080p", "qualities": WEB_1080},
            {"name": "Bluray-1080p", "qualities": ["Bluray-1080p"]},
            {"name": "WEB 2160p", "qualities": WEB_2160},
            {"name": "Bluray-2160p", "qualities": ["Bluray-2160p"]},
        ],
        "cutoff": "WEB 2160p",
        "format_scores": {"dolby_vision": 20, "hdr10_plus": 10},
    },
]
