"""Titles of a plugin media kind through the API: the kind listed, searched, a title added
with its items wanted, its library folder made, refreshed; duplicates and failures said."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest

from fetchbox_plugin import (
    Hit,
    Item,
    MediaKind,
    Naming,
    Parsed,
    Plugin,
    Qualities,
    TitleDetails,
)
from tests.conftest import Running
from tests.torrents import until

SOON = date.today() + timedelta(days=30)
ISSUES = [
    Item("i1", 1, "Chapter One", date(2012, 3, 14)),
    Item("i2", 2, "Chapter Two", date(2012, 4, 11)),
]
FAIL: dict[str, Any] = {}


class Zines(MediaKind):
    id = "zines"
    name = "Zines"
    levels = ("series", "issue")
    file_types = frozenset({".cbz"})
    qualities = Qualities(["CBZ"])
    naming = Naming("{series} #{issue}", tokens={"series", "issue"})

    async def search(self, ctx: Any, text: str) -> list[Hit]:
        if FAIL.get("search"):
            raise RuntimeError("the zine library is closed")
        return (
            [Hit("saga", "Saga", 2012, "Space opera.", "https://img.example/saga.jpg")]
            if "saga" in text.lower()
            else []
        )

    async def title(self, ctx: Any, ref: str) -> TitleDetails:
        return TitleDetails(
            ref, "Saga", 2012, "Space opera.", items=list(ISSUES), extra={"publisher": "Image"}
        )

    def parse(self, release_name: str) -> Parsed | None:
        return None

    def name_tokens(self, title: TitleDetails, item: Item) -> dict[str, object]:
        return {"series": title.name, "issue": item.number}


class ZinesPlugin(Plugin):
    id = "zines"
    name = "Zines"
    contributes: ClassVar[list[type[Any]]] = [Zines]


@pytest.fixture
async def zines(server: Running) -> Any:
    host = server.app.state.plugins.host
    FAIL.clear()
    await host.load(ZinesPlugin)
    yield
    await host.unload("zines")


async def test_a_plugin_kind_title(
    zines: Any, c: httpx.AsyncClient, server: Running, settings: Any
) -> None:
    assert (await c.get("/plugins/kinds")).json() == [
        {
            "id": "zines",
            "name": "Zines",
            "plural": "Zines",
            "levels": ["series", "issue"],
            "plugin": "zines",
        }
    ]
    [hit] = (await c.get("/plugins/kinds/zines/search", params={"q": "saga"})).json()
    assert (hit["ref"], hit["name"], hit["year"], hit["title_id"]) == ("saga", "Saga", 2012, None)

    r = await c.post("/plugins/kinds/zines/titles", json={"ref": "saga"})
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    t = (await c.get(f"/titles/{tid}")).json()
    assert (t["kind"], t["name"], t["year"]) == ("zines", "Saga", 2012)
    items = (await c.get(f"/plugins/titles/{tid}/items")).json()
    assert [(i["number"], i["name"], i["state"], i["monitored"]) for i in items] == [
        (1.0, "Chapter One", "missing", True), (2.0, "Chapter Two", "missing", True)]  # fmt: skip
    # its library folder: made, beside Movies, named after the kind
    [dest] = [d for d in (await c.get("/destinations")).json() if d["media_kind"] == "zines"]
    assert dest["name"] == "Zines" and Path(dest["path"]).is_dir()
    assert Path(dest["path"]).parent == settings.downloads_dir
    # in the library like any title; already there: said, not added twice
    assert tid in {x["id"] for x in (await c.get("/titles")).json()["items"]}
    [hit] = (await c.get("/plugins/kinds/zines/search", params={"q": "saga"})).json()
    assert hit["title_id"] == tid
    assert (await c.post("/plugins/kinds/zines/titles", json={"ref": "saga"})).status_code == 409

    # refreshed: a new issue appears, wanted
    ISSUES.append(Item("i3", 3, "Chapter Three", SOON))
    try:
        assert (await c.post(f"/plugins/titles/{tid}/refresh")).status_code == 200
        items = (await c.get(f"/plugins/titles/{tid}/items")).json()
        assert [i["number"] for i in items] == [1.0, 2.0, 3.0]
    finally:
        ISSUES.pop()


async def test_only_future_issues(zines: Any, c: httpx.AsyncClient) -> None:
    ISSUES.append(Item("i9", 9, "Next", SOON))
    try:
        tid = (
            await c.post("/plugins/kinds/zines/titles", json={"ref": "saga", "monitor": "future"})
        ).json()["id"]
        items = (await c.get(f"/plugins/titles/{tid}/items")).json()
        assert [(i["number"], i["monitored"]) for i in items] == [
            (1.0, False),
            (2.0, False),
            (9.0, True),
        ]
    finally:
        ISSUES.pop()


async def test_failures_are_said(zines: Any, c: httpx.AsyncClient) -> None:
    FAIL["search"] = True
    r = await c.get("/plugins/kinds/zines/search", params={"q": "saga"})
    assert r.status_code == 502 and "the zine library is closed" in r.json()["detail"]
    assert (await c.get("/plugins/kinds/nope/search", params={"q": "x"})).status_code == 404


async def test_no_kinds_without_plugins(c: httpx.AsyncClient) -> None:
    assert (await c.get("/plugins/kinds")).json() == []


async def test_issues_on_the_calendar(zines: Any, c: httpx.AsyncClient) -> None:
    tid = (await c.post("/plugins/kinds/zines/titles", json={"ref": "saga"})).json()["id"]
    cal = (await c.get("/calendar", params={"start": "2012-03-01", "end": "2012-04-30"})).json()
    got = [(e["date"], e["kind"], e["media_kind"], e["name"], e["episode"], e["episode_name"])
           for e in cal if e["title_id"] == tid]  # fmt: skip
    assert got == [
        ("2012-03-14", "item", "zines", "Saga", 1, "Chapter One"),
        ("2012-04-11", "item", "zines", "Saga", 2, "Chapter Two"),
    ]


async def test_counts_in_the_titles_list(zines: Any, c: httpx.AsyncClient) -> None:
    tid = (await c.post("/plugins/kinds/zines/titles", json={"ref": "saga"})).json()["id"]
    [t] = [x for x in (await c.get("/titles")).json()["items"] if x["id"] == tid]
    assert t["items"] == {"items": 2, "monitored": 2, "acquired": 0, "missing": 2}
    assert t["episodes"] is None and t["albums"] is None


async def test_titles_are_read_again_daily(
    zines: Any, c: httpx.AsyncClient, server: Running
) -> None:
    """A newly announced issue appears without anyone pressing Refresh."""
    from fetchbox.plugin.titles import REFRESH_ALL

    tid = (await c.post("/plugins/kinds/zines/titles", json={"ref": "saga"})).json()["id"]
    scheds = {s["name"]: s for s in (await c.get("/automation/schedules")).json()}
    assert scheds[REFRESH_ALL]["interval_seconds"] == 24 * 3600
    ISSUES.append(Item("i3", 3, "Chapter Three", SOON))
    try:
        assert (await c.post(f"/automation/schedules/{REFRESH_ALL}/run")).status_code in (200, 202)

        async def three() -> bool:
            items = (await c.get(f"/plugins/titles/{tid}/items")).json()
            return [i["number"] for i in items] == [1.0, 2.0, 3.0]

        await until(three, within=10)
        items = (await c.get(f"/plugins/titles/{tid}/items")).json()
        assert items[-1]["monitored"] is True  # wanted, as the title's monitor setting says
    finally:
        ISSUES.pop()


async def test_searching_one_item(zines: Any, c: httpx.AsyncClient) -> None:
    """The item's own Search: only that one is searched for (released or not)."""
    tid = (await c.post("/plugins/kinds/zines/titles", json={"ref": "saga"})).json()["id"]
    items = (await c.get(f"/plugins/titles/{tid}/items")).json()
    r = await c.post(f"/plugins/titles/{tid}/search", json={"item_ids": [items[0]["id"]]})
    assert r.status_code == 200 and r.json()["searched"] == 1
    every = (await c.post(f"/plugins/titles/{tid}/search")).json()  # no body: all missing
    assert every["searched"] == 2


async def test_all_items_wanted_or_none(zines: Any, c: httpx.AsyncClient) -> None:
    """The "All issues" checkbox: every item at once, by the same rules as one."""
    tid = (await c.post("/plugins/kinds/zines/titles", json={"ref": "saga"})).json()["id"]
    r = await c.put(f"/plugins/titles/{tid}/monitored", json={"monitored": False})
    assert r.status_code == 200 and r.json() == {"changed": 2}
    items = (await c.get(f"/plugins/titles/{tid}/items")).json()
    assert [i["monitored"] for i in items] == [False, False]
    assert (await c.put(f"/plugins/titles/{tid}/monitored", json={"monitored": False})).json() == {
        "changed": 0}  # fmt: skip
    assert (await c.put(f"/plugins/titles/{tid}/monitored", json={"monitored": True})).json() == {
        "changed": 2}  # fmt: skip
    missing = await c.put("/plugins/titles/nope/monitored", json={"monitored": True})
    assert missing.status_code == 404
