"""The media kind list: the core kinds keep exactly the values the code had written in
before it read them from the list, and a plugin's kind can be added but never replace one."""

from __future__ import annotations

from typing import get_args

import pytest

from fetchbox.core import kinds
from fetchbox.core.kinds import KindError, MediaKind
from fetchbox.modules.library.music_files import AUDIO_EXTS, is_audio
from fetchbox.modules.library.service import VIDEO_EXTS, is_video


@pytest.fixture
def plugin_kind():
    yield MediaKind(
        key="comic",
        label="Comic",
        plural="Comics",
        levels=("series", "issue"),
        file_exts=frozenset({"cbz", "cbr"}),
        category="fetchbox-comics",
        profile_media="comic",
        plugin="comics",
    )
    kinds.unregister("comics")


def test_the_core_kinds_and_their_order():
    assert kinds.CORE_KEYS == ("movie", "series", "music")
    assert kinds.keys() == kinds.CORE_KEYS
    assert all(k.core for k in kinds.all_kinds())


def test_core_values_are_the_ones_the_code_had():
    # Download client categories: changing one would strand downloads already in a client
    assert {k: kinds.get(k).category for k in kinds.CORE_KEYS} == {
        "movie": "fetchbox-movies",
        "series": "fetchbox-tv",
        "music": "fetchbox-music",
    }
    # Default library folder names
    assert [kinds.get(k).plural for k in kinds.CORE_KEYS] == ["Movies", "TV", "Music"]
    assert {k: kinds.get(k).profile_media for k in kinds.CORE_KEYS} == {
        "movie": "video",
        "series": "video",
        "music": "music",
    }
    old_video = {"mkv", "mp4", "m4v", "avi", "mov", "wmv", "ts", "m2ts", "webm", "mpg", "mpeg"}
    old_audio = {".flac", ".mp3", ".m4a", ".mp4", ".aac", ".ogg", ".opus", ".wav", ".aiff", ".aif"}
    old_audio |= {".wma", ".ape", ".wv"}
    assert old_video == VIDEO_EXTS
    assert old_audio == AUDIO_EXTS


def test_the_api_still_lists_the_same_kinds():
    """The API's kind fields are unchanged; plugin kinds reach the API only later."""
    from fetchbox.modules.media import models
    from fetchbox.modules.storage.router import Kind

    assert models.KINDS == kinds.CORE_KEYS
    assert get_args(Kind) == kinds.CORE_KEYS


@pytest.mark.parametrize(
    "name",
    [
        "Movie.2020.1080p.mkv",
        "movie.MKV",
        "._Movie.mkv",
        "folder/Show.S01E01.mp4",
        "folder\\Show.S01E01.avi",
        "notes.txt",
        "mkv",
        ".mkv",
        "01 Song.flac",
        "01 Song.FLAC",
        "._01 Song.flac",
        "album/02 Song.m4a",
        "cover.jpg",
        "Song.mp4",
    ],
)
def test_is_media_file_agrees_with_the_old_checks(name):
    assert kinds.get("movie").is_media_file(name) == is_video(name)
    assert kinds.get("series").is_media_file(name) == is_video(name)
    assert kinds.get("music").is_media_file(name) == is_audio(name.replace("\\", "/"))


def test_a_plugin_adds_a_kind_after_the_core_ones(plugin_kind):
    kinds.register(plugin_kind)
    assert kinds.keys() == (*kinds.CORE_KEYS, "comic")
    assert kinds.get("comic") is plugin_kind
    assert not plugin_kind.core
    assert plugin_kind.is_media_file("Saga 001.cbz")
    assert not plugin_kind.is_media_file("Saga 001.mkv")
    kinds.unregister("comics")
    assert kinds.find("comic") is None
    assert kinds.keys() == kinds.CORE_KEYS


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"key": "movie"}, "already a kind"),
        ({"key": "music"}, "already a kind"),
        ({"key": "Comic"}, "lowercase"),
        ({"key": "c"}, "lowercase"),
        ({"key": "graphicnovel"}, "lowercase"),  # longer than the 10-character columns
        ({"plugin": None}, "names its plugin"),
        ({"levels": ()}, "levels"),
        ({"file_exts": frozenset()}, "file types"),
        ({"category": "fetchbox-tv"}, "category"),
    ],
)
def test_a_plugin_cant_replace_or_break_a_kind(plugin_kind, change, why):
    from dataclasses import replace

    with pytest.raises(KindError, match=why):
        kinds.register(replace(plugin_kind, **change))
    assert kinds.keys() == kinds.CORE_KEYS
    assert kinds.get("movie").category == "fetchbox-movies"


def test_a_second_plugin_cant_take_a_kind(plugin_kind):
    from dataclasses import replace

    kinds.register(plugin_kind)
    with pytest.raises(KindError, match="already a kind"):
        kinds.register(replace(plugin_kind, plugin="other", category="fetchbox-other"))
    kinds.unregister("other")  # removes nothing of the first plugin's
    assert kinds.get("comic").plugin == "comics"


def test_an_unknown_kind_is_an_error():
    with pytest.raises(KindError, match="not a media kind"):
        kinds.get("ebook")
