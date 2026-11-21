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
# Named by resolution: each takes only that resolution (sources ranked within it), so
# what you pick is what you get. "Any" also takes releases that don't state a quality.
WEB_480 = ["WEBRip-480p", "WEBDL-480p"]
PROFILES: list[dict[str, Any]] = [
    {
        "name": "1080p",
        "ladder": [
            {"name": "HDTV-1080p", "qualities": ["HDTV-1080p"]},
            {"name": "WEB 1080p", "qualities": WEB_1080},
            {"name": "Bluray-1080p", "qualities": ["Bluray-1080p"]},
        ],
        "cutoff": "WEB 1080p",
        "format_scores": {},
    },
    {
        "name": "2160p",
        "ladder": [
            {"name": "WEB 2160p", "qualities": WEB_2160},
            {"name": "Bluray-2160p", "qualities": ["Bluray-2160p"]},
        ],
        "cutoff": "WEB 2160p",
        "format_scores": {"dolby_vision": 20, "hdr10_plus": 10},
    },
    {
        "name": "720p",
        "ladder": [
            {"name": "HDTV-720p", "qualities": ["HDTV-720p"]},
            {"name": "WEB 720p", "qualities": WEB_720},
            {"name": "Bluray-720p", "qualities": ["Bluray-720p"]},
        ],
        "cutoff": "WEB 720p",
        "format_scores": {},
    },
    {
        "name": "480p",
        "ladder": [
            {"name": "SDTV", "qualities": ["SDTV", "Unknown-480p"]},
            {"name": "WEB 480p", "qualities": WEB_480},
            {"name": "DVD", "qualities": ["DVD", "Bluray-480p", "Bluray-576p"]},
        ],
        "cutoff": "WEB 480p",
        "format_scores": {},
    },
    {
        # Releases that don't state a quality land here, e.g. Internet Archive films
        # (the built-in indexer): the file's real resolution is read on import.
        "name": "Any",
        "ladder": [
            {"name": "Unknown", "qualities": ["Unknown"]},
            {
                "name": "SD",
                "qualities": [
                    "SDTV",
                    "DVD",
                    "Unknown-480p",
                    "WEBRip-480p",
                    "WEBDL-480p",
                    "Bluray-480p",
                    "Bluray-576p",
                ],
            },
            {"name": "720p", "qualities": ["Unknown-720p", "HDTV-720p", *WEB_720, "Bluray-720p"]},
            {
                "name": "1080p",
                "qualities": ["Unknown-1080p", "HDTV-1080p", *WEB_1080, "Bluray-1080p"],
            },
        ],
        "cutoff": "1080p",
        "format_scores": {},
    },
]
