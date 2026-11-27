"""Subtitles against a fake OpenSubtitles (the real API's requests and answers) and a real
Fetchbox from the test kit."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fetchbox_subtitles import SubtitlesPlugin, pick
from fetchbox_subtitles.moviehash import moviehash
from fetchbox_subtitles.opensubtitles import Found

SRT = b"1\n00:00:01,000 --> 00:00:03,000\nThe spice must flow.\n"
SETTINGS = {"subtitles": {"api_key": "k3y", "languages": ["en", "fr"]}}


class FakeOpenSubtitles:
    def __init__(self) -> None:
        self.subtitles: dict[str, list[dict[str, Any]]] = {"en": [self.hit(101, "en")]}
        self.remaining = 20

    @staticmethod
    def hit(file_id: int, lang: str, **attrs: Any) -> dict[str, Any]:
        return {
            "attributes": {
                "language": lang,
                "release": f"Release {file_id}",
                "download_count": 10,
                "files": [{"file_id": file_id}],
                **attrs,
            }
        }

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.url.host == "dl.opensubtitles.example":
            return httpx.Response(200, content=SRT)
        if "fetch" in req.headers["user-agent"].lower():  # as the real gateway does
            return httpx.Response(403, json={"error": "Blocked (kong-user-agent-block)"})
        assert req.headers["api-key"] == "k3y"
        path = req.url.path.removeprefix("/api/v1")
        if path == "/login":
            body = json.loads(req.content)
            if body["password"] != "right":
                return httpx.Response(401, json={"message": "Error, invalid username/password"})
            return httpx.Response(200, json={"token": "t0ken"})
        if path == "/infos/languages":
            return httpx.Response(200, json={"data": []})
        if path == "/subtitles":
            return httpx.Response(
                200, json={"data": self.subtitles.get(req.url.params["languages"], [])}
            )
        if path == "/download":
            if self.remaining <= 0:
                return httpx.Response(406, json={"reset_time": "2099-01-01T00:00:00Z"})
            self.remaining -= 1
            fid = json.loads(req.content)["file_id"]
            return httpx.Response(
                200,
                json={
                    "link": f"https://dl.opensubtitles.example/{fid}.srt",
                    "remaining": self.remaining,
                },
            )
        return httpx.Response(404)


async def _app(
    fetchbox_app: Any, fake: FakeOpenSubtitles, settings: Any = SETTINGS
) -> tuple[Any, list[httpx.Request]]:
    app = await fetchbox_app(plugins=[SubtitlesPlugin], settings=settings)
    return app, app.fake_http("subtitles", fake)


async def test_a_movie_gets_its_subtitles(fetchbox_app: Any) -> None:
    fake = FakeOpenSubtitles()
    app, seen = await _app(fetchbox_app, fake)
    f = await app.add_file("Dune", 2021, tmdb_id=438631)
    assert f.path.with_suffix(".en.srt").read_bytes() == SRT
    [sc] = [s for s in await app.ctx("subtitles").files.sidecars(f.id)]
    assert sc.meta["source"] == "OpenSubtitles" and sc.meta["file_id"] == 101
    searches = [r for r in seen if r.url.path.endswith("/subtitles")]
    assert {r.url.params["languages"] for r in searches} == {"en", "fr"}
    assert all(r.url.params["tmdb_id"] == "438631" for r in searches)
    assert searches[0].headers["user-agent"].startswith("FBox Subtitles v")
    # French wasn't found: remembered, and found by the daily retry once it's there
    assert await app.ctx("subtitles").store.get("missing") == {f.id: ["fr"]}
    fake.subtitles["fr"] = [fake.hit(202, "fr")]
    ran = await app.run("subtitles", "retry_missing")
    assert ran.result == {"subtitles": 1, "still_missing": 0}
    assert f.path.with_suffix(".fr.srt").read_bytes() == SRT
    # and "Search subtitles" on the movie: nothing left to get
    item = f.item_ids[0]
    assert (await app.run("subtitles", "search_now", item)).result == {"subtitles": 0}


async def test_an_episode_is_searched_by_the_show_and_its_number(fetchbox_app: Any) -> None:
    app, seen = await _app(fetchbox_app, FakeOpenSubtitles())
    f = await app.add_file("Severance", 2022, kind="series", season=1, episode=3, tmdb_id=95396)
    assert f.path.with_suffix(".en.srt").is_file()
    params = next(r.url.params for r in seen if r.url.path.endswith("/subtitles"))
    assert (params["parent_tmdb_id"], params["season_number"], params["episode_number"]) == (
        "95396",
        "1",
        "3",
    )
    assert "tmdb_id" not in params


async def test_music_and_titles_without_ids_are_left_alone(fetchbox_app: Any) -> None:
    app, seen = await _app(fetchbox_app, FakeOpenSubtitles())
    assert await app.emit("file.imported", kind="music", file_ids=["x"]) == []
    f = await app.add_file("Home video", 2020)  # no TMDB or IMDb id: nothing to search by
    assert not f.path.with_suffix(".en.srt").exists() and seen == []


async def test_out_of_downloads_it_waits(fetchbox_app: Any) -> None:
    fake = FakeOpenSubtitles()
    fake.remaining = 0
    app, seen = await _app(fetchbox_app, fake)
    f = await app.add_file("Dune", 2021, imdb_id="tt1160419")
    assert not f.path.with_suffix(".en.srt").exists()
    assert await app.ctx("subtitles").store.get("paused_until") == "2099-01-01T00:00:00Z"
    asked = len(seen)
    await app.add_file("Arrival", 2016, imdb_id="tt2543164")
    assert len(seen) == asked  # paused: OpenSubtitles isn't asked again until the reset


async def test_the_settings_test_button(fetchbox_app: Any) -> None:
    app, _ = await _app(fetchbox_app, FakeOpenSubtitles())
    from fetchbox_subtitles import SubtitleSettings

    s = SubtitleSettings.model_validate({"api_key": "k3y"})
    assert await s.test(app.ctx("subtitles")) == "The API key works"


def test_the_best_subtitle() -> None:
    base = dict(language="en", release="r", hearing_impaired=False)
    popular = Found(1, downloads=900, hash_match=False, machine_made=False, **base)
    timed = Found(2, downloads=5, hash_match=True, machine_made=False, **base)
    machine = Found(3, downloads=9999, hash_match=False, machine_made=True, **base)
    assert pick([popular, timed, machine], hearing_impaired=False) == timed
    assert pick([popular, machine], hearing_impaired=False) == popular
    assert pick([], hearing_impaired=False) is None


def test_moviehash(tmp_path: Any) -> None:
    small = tmp_path / "small.mkv"
    small.write_bytes(b"x" * 1000)
    assert moviehash(small) is None
    big = tmp_path / "big.mkv"
    big.write_bytes(bytes(range(256)) * 1024)  # 256 KB
    h = moviehash(big)
    assert h is not None and len(h) == 16 and h == moviehash(big)


async def test_signing_in(fetchbox_app: Any) -> None:
    """The Test button with an account: OpenSubtitles' own words when it says no."""
    from fetchbox_subtitles import SubtitleSettings
    from fetchbox_subtitles.opensubtitles import OpenSubtitlesError

    fake = FakeOpenSubtitles()
    app, seen = await _app(fetchbox_app, fake)
    ctx = app.ctx("subtitles")
    good = SubtitleSettings(api_key="k3y", username="me", password="right")
    assert await good.test(ctx) == "The API key and the account work"
    bad = SubtitleSettings(api_key="k3y", username="you", password="wrong")
    with pytest.raises(OpenSubtitlesError, match="invalid username/password; check the"):
        await bad.test(ctx)
    assert all("fetch" not in r.headers["user-agent"].lower() for r in seen)


async def test_finding_what_the_library_already_has(fetchbox_app: Any) -> None:
    """Files there before the plugin: Find missing subtitles, for the library or a title."""
    fake = FakeOpenSubtitles()  # English found, French not (yet)
    app, _seen = await _app(fetchbox_app, fake)
    dune = await app.add_file("Dune", 2021, tmdb_id=438631, imported=False)
    ep = await app.add_file("Severance", 2022, kind="series", season=1, episode=1,
                            tmdb_id=95396, imported=False)  # fmt: skip
    assert not dune.path.with_suffix(".en.srt").exists()  # not imported: nothing happened

    ran = await app.run("subtitles", "find_everywhere")
    assert ran.result is not None
    assert {k: ran.result[k] for k in ("checked", "queued", "added", "left")} == {
        "checked": 2, "queued": 2, "added": 2, "left": 2}  # fmt: skip
    assert ran.result["message"].startswith("2 file(s) checked, 2 missing subtitles; 2 added now")
    assert dune.path.with_suffix(".en.srt").is_file() and ep.path.with_suffix(".en.srt").is_file()

    # French turns up; the movie's own button gets it
    fake.subtitles["fr"] = [fake.hit(202, "fr")]
    ran = await app.run("subtitles", "find_for_title", dune.title_id)
    assert ran.result is not None and ran.result["added"] == 1
    assert (
        dune.path.with_suffix(".fr.srt").is_file() and not ep.path.with_suffix(".fr.srt").exists()
    )
    # everything there: said so
    await app.run("subtitles", "find_for_title", ep.title_id)
    ran = await app.run("subtitles", "find_for_title", dune.title_id)
    assert ran.result is not None
    assert ran.result["message"] == "1 file(s) checked: all have subtitles in your languages."


def test_languages_are_picked_by_name() -> None:
    from fetchbox_subtitles import SubtitleSettings

    schema = SubtitleSettings.model_json_schema()["properties"]["languages"]
    choices = schema["x-fetchbox-choices"]
    assert choices["en"] == "English" and choices["pt-br"] == "Portuguese (Brazil)"


async def test_finding_for_one_season(fetchbox_app: Any) -> None:
    app, _seen = await _app(fetchbox_app, FakeOpenSubtitles())
    s1 = await app.add_file("Severance", 2022, kind="series", season=1, episode=1,
                            tmdb_id=95396, imported=False)  # fmt: skip
    s2 = await app.add_file("Severance", 2022, kind="series", season=2, episode=1,
                            tmdb_id=95396, imported=False)  # fmt: skip
    assert s1.title_id == s2.title_id
    ran = await app.run("subtitles", "find_for_season", f"{s1.title_id}:2")
    assert ran.result is not None and (ran.result["checked"], ran.result["added"]) == (1, 1)
    assert s2.path.with_suffix(".en.srt").is_file() and not s1.path.with_suffix(".en.srt").exists()
