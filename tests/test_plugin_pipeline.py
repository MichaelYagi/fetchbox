"""A plugin media kind through the whole pipeline: a real torrent of an issue is searched
for in the kind's categories, chosen by the kind's parser, grabbed, downloaded by the
built-in engine from a local seeder, picked by the kind, named by its naming and filed."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest

from fetchbox.providers.download_clients.builtin import BuiltinClient
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
from tests.fake_torznab import Item as Release
from tests.pipeline import Fakes, _indexer
from tests.test_library_api import _engine
from tests.torrents import Made, make_torrent, seeder, until

NAME = "Saga 001 (2012) (Digital)"
ISSUE = re.compile(r"^(?P<t>.+?) (?P<n>\d{3}) \((?P<y>\d{4})\)")


class Zines(MediaKind):
    id = "zines"
    name = "Zines"
    levels = ("series", "issue")
    file_types = frozenset({".cbz"})
    qualities = Qualities(["CBZ", "CBR"])
    naming = Naming("{series} #{issue}", tokens={"series", "issue"})
    categories = (7030,)

    async def search(self, ctx: Any, text: str) -> list[Hit]:
        return [Hit("saga", "Saga", 2012)]

    async def title(self, ctx: Any, ref: str) -> TitleDetails:
        return TitleDetails(ref, "Saga", 2012, items=[
            Item("i1", 1, "Chapter One", date(2012, 3, 14)),
            Item("i2", 2, "Chapter Two", date(2012, 4, 11)),
        ])  # fmt: skip

    def parse(self, release_name: str) -> Parsed | None:
        m = ISSUE.match(release_name)
        if not m:
            return None
        return Parsed(m["t"], (float(m["n"]),), year=int(m["y"]),
                      quality="CBZ" if "Digital" in release_name else "CBR")  # fmt: skip

    def name_tokens(self, title: TitleDetails, item: Item) -> dict[str, object]:
        return {"series": title.name, "issue": int(item.number or 0)}


class ZinesPlugin(Plugin):
    id = "zines"
    name = "Zines"
    contributes: ClassVar[list[type[Any]]] = [Zines]


@pytest.fixture
def issue(tmp_path: Path) -> Made:
    return make_torrent(tmp_path / "seedroot", NAME, {f"{NAME}.cbz": 300_000, "cover.jpg": 5_000})


@pytest.fixture
async def issue_seed(issue: Made) -> Any:
    s = await seeder(issue)
    yield s
    await s.stop()


async def test_an_issue_from_search_to_the_library(
    c: httpx.AsyncClient,
    fakes: Fakes,
    server: Running,
    issue: Made,
    issue_seed: BuiltinClient,
    settings: Any,
) -> None:
    await server.app.state.plugins.host.load(ZinesPlugin)
    try:
        ixr = await _indexer(c, fakes, "comics", [
            Release(NAME, magnet=issue.magnet, infohash=issue.info_hash),
            Release("Saga 001 (2012) (Scan)", infohash="b" * 40),  # worse quality: not chosen
            Release("Paper Girls 001 (2015) (Digital)", infohash="c" * 40),  # another series
        ])  # fmt: skip
        assert ixr["id"]
        tid = (await c.post("/plugins/kinds/zines/titles", json={"ref": "saga"})).json()["id"]
        r = await c.post(f"/plugins/titles/{tid}/search")
        assert r.status_code == 200, r.text
        assert r.json()["grabbed"] == 1 and r.json()["results"] == 3
        asked = [q for q in fakes.torznab["comics"].requests if q.url.params.get("t") == "search"]
        assert {q.url.params["q"] for q in asked} == {"Saga 1", "Saga 2"}  # the default queries
        assert all(q.url.params.get("cat") == "7030" for q in asked)  # in the kind's category

        first = (await c.get(f"/plugins/titles/{tid}/items")).json()[0]["id"]
        [g] = (await c.get("/grabs", params={"wanted_item_id": first})).json()
        assert g["release_title"] == NAME

        async def downloading() -> bool:
            return any(d["status"] == "downloading" for d in (await c.get("/downloads")).json())

        await until(downloading, within=20)

        async def acquired() -> bool:
            _engine(server).connect_peer(issue.info_hash, "127.0.0.1", issue_seed.listen_port or 0)
            items = (await c.get(f"/plugins/titles/{tid}/items")).json()
            return items[0]["state"] == "acquired"

        await until(acquired, within=60)
        items = (await c.get(f"/plugins/titles/{tid}/items")).json()
        assert [(i["number"], i["state"]) for i in items] == [(1.0, "acquired"), (2.0, "missing")]
        [f] = (await c.get(f"/titles/{tid}/files")).json()
        assert f["relative_path"] == "Saga #1.cbz" and f["quality"]["name"] == "CBZ"
        dest = next(d for d in (await c.get("/destinations")).json() if d["media_kind"] == "zines")
        assert (Path(dest["path"]) / "Saga #1.cbz").stat().st_size == 300_000
        assert not (Path(dest["path"]) / "cover.cbz").exists()  # only the issue was picked
    finally:
        await server.app.state.plugins.host.unload("zines")
