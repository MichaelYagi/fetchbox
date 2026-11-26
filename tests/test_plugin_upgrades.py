"""A plugin kind's title: which qualities it accepts and when it's good enough, chosen per
title; an item whose file is below that is upgraded only by a better release."""

from __future__ import annotations

from datetime import date
from typing import Any, ClassVar

import httpx
import pytest

from fetchbox.providers.indexers import ReleaseCandidate
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


class Strips(MediaKind):
    id = "strips"
    name = "Strips"
    levels = ("series", "issue")
    file_types = frozenset({".cbz", ".cbr"})
    qualities = Qualities(["CBZ", "CBR"])
    naming = Naming("{series} #{issue}", tokens={"series", "issue"})

    async def search(self, ctx: Any, text: str) -> list[Hit]:
        return [Hit("saga", "Saga", 2012)]

    async def title(self, ctx: Any, ref: str) -> TitleDetails:
        items = [Item("i1", 1, "One", date(2012, 3, 14)), Item("i2", 2, "Two", date(2012, 4, 11))]
        return TitleDetails(ref, "Saga", 2012, items=items)

    def parse(self, release_name: str) -> Parsed | None:
        stem, _, ext = release_name.rpartition(".")
        n, q = stem.split()[1], ext.upper()
        return Parsed("Saga", (float(n),), quality=q if q in ("CBZ", "CBR") else None)

    def name_tokens(self, title: TitleDetails, item: Item) -> dict[str, object]:
        return {"series": title.name, "issue": int(item.number or 0)}


class StripsPlugin(Plugin):
    id = "strips"
    name = "Strips"
    contributes: ClassVar[list[type[Any]]] = [Strips]


@pytest.fixture
async def saga(server: Running, c: httpx.AsyncClient) -> Any:
    await server.app.state.plugins.host.load(StripsPlugin)
    tid = (await c.post("/plugins/kinds/strips/titles", json={"ref": "saga"})).json()["id"]
    yield tid
    await server.app.state.plugins.host.unload("strips")


async def test_qualities_per_title(saga: str, c: httpx.AsyncClient) -> None:
    q = (await c.get(f"/plugins/titles/{saga}/qualities")).json()
    assert q == {"available": ["CBZ", "CBR"], "qualities": ["CBZ", "CBR"], "cutoff": "CBZ"}
    r = await c.put(
        f"/plugins/titles/{saga}/qualities", json={"qualities": ["CBR", "CBZ"], "cutoff": "CBR"}
    )
    assert r.json() == {"available": ["CBZ", "CBR"], "qualities": ["CBZ", "CBR"], "cutoff": "CBR"}
    bad = await c.put(
        f"/plugins/titles/{saga}/qualities", json={"qualities": ["PDF"], "cutoff": "PDF"}
    )
    assert bad.status_code == 422


async def test_an_upgrade_needs_a_better_release(saga: str, server: Running) -> None:
    pipeline = server.app.state.plugin_pipeline
    title = await server.app.state.media.get_title(saga)
    kind = server.app.state.plugin_titles.kind("strips")[0]
    rows = await pipeline._rows(saga)
    details = pipeline._details(title, rows)
    one, two = rows[0][1].id, rows[1][1].id

    def rel(name: str) -> ReleaseCandidate:
        return ReleaseCandidate(title=name, info_hash=name.encode().hex()[:40].ljust(40, "0"))

    found = [rel("Saga 1.cbr"), rel("Saga 1.cbz"), rel("Saga 2.cbr")]
    # issue 1 has a CBR file (rank 1); issue 2 is missing
    picks = await pipeline.decide(title, kind, details, rows, found,
                                  allowed=["CBZ", "CBR"], current={one: 1})  # fmt: skip
    got = {(p.candidate.title, tuple(p.items)) for p in picks}
    assert got == {("Saga 1.cbz", (one,)), ("Saga 2.cbr", (two,))}  # not another CBR for #1

    # a title that takes only CBR: CBZ releases aren't taken at all
    picks = await pipeline.decide(title, kind, details, rows, found, allowed=["CBR"], current={})
    assert {p.candidate.title for p in picks} == {"Saga 1.cbr", "Saga 2.cbr"}


async def test_good_enough_isnt_upgraded(saga: str, server: Running, c: httpx.AsyncClient) -> None:
    """With CBR as the cutoff, an item with a CBR file isn't searched for again."""
    pipeline = server.app.state.plugin_pipeline
    title = await server.app.state.media.get_title(saga)
    kind = server.app.state.plugin_titles.kind("strips")[0]
    assert pipeline.qualities(title, kind) == (["CBZ", "CBR"], "CBZ")
    await c.put(
        f"/plugins/titles/{saga}/qualities", json={"qualities": ["CBZ", "CBR"], "cutoff": "CBR"}
    )
    title = await server.app.state.media.get_title(saga)
    assert pipeline.qualities(title, kind) == (["CBZ", "CBR"], "CBR")
