"""Comics against a fake ComicVine (the real API's requests and answers) and a real Fetchbox
from the test kit."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
from fetchbox_comics import Comics, ComicsPlugin

from fetchbox_plugin import Item, Query, TitleDetails
from fetchbox_plugin.testing import check_parse, parse_cases

SETTINGS = {"comics": {"comicvine_key": "cv-key"}}
SAGA = {"id": 49901, "name": "Saga", "start_year": "2012", "deck": "Space opera.",
        "image": {"medium_url": "https://img.example/saga.jpg"}, "publisher": {"name": "Image"},
        "count_of_issues": 66}  # fmt: skip


def comicvine(req: httpx.Request) -> httpx.Response:
    q = req.url.params
    if q.get("api_key") != "cv-key":
        return httpx.Response(200, json={"status_code": 100, "error": "Invalid API Key"})
    ok = {"status_code": 1, "error": "OK"}
    path = req.url.path.removeprefix("/api")
    if path == "/types/":
        return httpx.Response(200, json={**ok, "results": []})
    if path == "/search/":
        hits = [SAGA] if "saga" in q["query"].lower() else []
        return httpx.Response(200, json={**ok, "results": hits})
    if path == "/volume/4050-49901/":
        return httpx.Response(200, json={**ok, "results": SAGA})
    if path == "/issues/":
        assert q["filter"] == "volume:49901"
        every = [
            {"id": 1000 + n, "issue_number": str(n), "name": f"Chapter {n}",
             "store_date": f"{2011 + (n + 11) // 12}-01-1{n % 10}"}
            for n in range(1, 67)
        ]  # fmt: skip
        every.append({"id": 9, "issue_number": "½", "name": "Special", "cover_date": "2013-01-01"})
        offset, limit = int(q["offset"]), int(q["limit"])
        return httpx.Response(200, json={**ok, "results": every[offset : offset + limit],
                                          "number_of_total_results": len(every)})  # fmt: skip
    return httpx.Response(404)


async def _ctx(fetchbox_app: Any) -> tuple[Any, list[httpx.Request]]:
    app = await fetchbox_app(plugins=[ComicsPlugin], settings=SETTINGS)
    return app, app.fake_http("comics", comicvine)


async def test_comics_join_the_media_kinds(fetchbox_app: Any) -> None:
    app, _ = await _ctx(fetchbox_app)
    kind = app.host.loaded["comics"].kinds["comics"]
    assert kind.settings.comicvine_key.value == "cv-key"


async def test_search_and_a_series_issues(fetchbox_app: Any) -> None:
    app, seen = await _ctx(fetchbox_app)
    kind = app.host.loaded["comics"].kinds["comics"]
    ctx = app.ctx("comics")
    [hit] = await kind.search(ctx, "saga")
    assert (hit.ref, hit.name, hit.year, hit.poster_url) == (
        "4050-49901",
        "Saga",
        2012,
        "https://img.example/saga.jpg",
    )
    assert await kind.search(ctx, "nothing like it") == []
    t = await kind.title(ctx, hit.ref)
    assert (t.name, t.year, len(t.items)) == ("Saga", 2012, 67)  # two pages of issues
    assert [i.number for i in t.items[:3]] == [1, 2, 3] and t.items[-1].number is None  # "½" last
    assert t.items[60] == Item("1061", 61.0, "Chapter 61", date(2017, 1, 11))
    assert "fetchbox" in seen[0].headers["user-agent"].lower()


async def test_a_wrong_key_is_said_plainly(fetchbox_app: Any) -> None:
    app = await fetchbox_app(plugins=[ComicsPlugin], settings={"comics": {"comicvine_key": "nope"}})
    app.fake_http("comics", comicvine)
    kind = app.host.loaded["comics"].kinds["comics"]
    with pytest.raises(Exception, match="refused the API key"):
        await kind.search(app.ctx("comics"), "saga")
    with pytest.raises(Exception, match="refused the API key"):
        await kind.settings.test(app.ctx("comics"))


def test_naming_and_queries() -> None:
    kind = Comics()
    t = TitleDetails("4050-49901", "Saga", 2012, extra={"publisher": "Image"})
    item = Item("1061", 61.0, "Chapter 61", date(2023, 1, 11))
    assert kind.naming.pattern.format(**kind.name_tokens(t, item)) == "Saga (2012)/Saga #061 (2023)"
    assert kind.queries(t, [item, Item("x", 50.5)]) == [
        Query("Saga 061", (7030,)),
        Query("Saga 50.5", (7030,)),
    ]


@pytest.mark.parametrize(("name", "want"), parse_cases(Comics, Path(__file__).parent / "cases.txt"))
def test_parse(name: str, want: list[str]) -> None:
    check_parse(Comics, name, want)
