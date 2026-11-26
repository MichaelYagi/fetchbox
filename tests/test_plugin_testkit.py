"""The test kit, used the way a plugin's own tests use it."""

from __future__ import annotations

import os
from typing import Any, ClassVar

import pytest

from fetchbox.testing import HandlerFailed
from fetchbox_plugin import (
    Addon,
    Field,
    Hit,
    Item,
    MediaKind,
    Naming,
    Parsed,
    Permission,
    Plugin,
    PublicEvent,
    Qualities,
    Settings,
    TitleDetails,
    on,
)
from fetchbox_plugin.testing import check_parse, fetchbox_app, parse_cases  # noqa: F401


class Words(Settings):
    word: str = Field("Hello", title="Word")


class Hello(Addon):
    id = "hello"
    name = "Hello"
    applies_to: ClassVar[set[str]] = {"movie"}
    settings = Words

    @on("file.imported")
    async def wave(self, ctx: Any, event: PublicEvent) -> None:
        assert event.file is not None
        await ctx.files.add_sidecar(event.file, "hello.txt", self.settings.word.encode())

    @on("title.added")
    async def remember(self, ctx: Any, event: PublicEvent) -> dict[str, Any]:
        await ctx.store.set("last_title", event.data.get("name"))
        return {"remembered": event.data.get("name")}

    @on("title.removed")
    async def fail(self, ctx: Any, event: PublicEvent) -> None:
        raise ValueError("can't say goodbye")


class HelloPlugin(Plugin):
    id = "hello"
    name = "Hello"
    permissions: ClassVar[set[Permission]] = {Permission.LIBRARY_FILES}
    contributes: ClassVar[list[type[Any]]] = [Hello]


async def test_a_file_imported_runs_the_addon(fetchbox_app: Any) -> None:
    app = await fetchbox_app(plugins=[HelloPlugin], settings={"hello": {"word": "Hi"}})
    f = await app.add_file("Dune", 2021)
    assert f.path == app.library_file("Dune (2021)/Dune (2021).mkv") and f.path.is_file()
    assert f.path.with_suffix(".hello.txt").read_text() == "Hi"
    # the API sees the same library
    [api] = (await app.client.get(f"/titles/{f.title_id}/files")).json()
    assert api["id"] == f.id
    assert os.environ.get("FETCHBOX_TORRENT_ISOLATED") == "1"  # offline while it runs


async def test_emit_any_event(fetchbox_app: Any) -> None:
    app = await fetchbox_app(plugins=[HelloPlugin])
    [ran] = await app.emit("title.added", title_id="t1", kind="movie", name="Arrival")
    assert ran.status == "succeeded" and ran.result == {"remembered": "Arrival"}
    assert await app.ctx("hello").store.get("last_title") == "Arrival"
    assert await app.emit("title.added", kind="series", name="Severance") == []  # movies only
    with pytest.raises(HandlerFailed, match="can't say goodbye"):
        await app.emit("title.removed", kind="movie")
    [ran] = await app.emit("title.removed", kind="movie", check=False)
    assert ran.status == "failed" and "can't say goodbye" in (ran.error or "")


async def test_two_apps_in_one_test(fetchbox_app: Any) -> None:
    a = await fetchbox_app(plugins=[HelloPlugin], settings={"hello": {"word": "A"}})
    b = await fetchbox_app(plugins=[], settings={})
    assert a.root != b.root
    assert "hello" in a.host.loaded and not b.host.loaded


class Comics(MediaKind):
    id = "comics"
    name = "Comics"
    levels = ("series", "issue")
    file_types = frozenset({".cbz"})
    qualities = Qualities(["CBZ"])
    naming = Naming("{series} #{issue}", tokens={"series", "issue"})

    async def search(self, ctx: Any, text: str) -> list[Hit]:
        return []

    async def title(self, ctx: Any, ref: str) -> TitleDetails:
        return TitleDetails(ref, ref)

    def parse(self, release_name: str) -> Parsed | None:
        import re

        m = re.match(r"(?P<t>.+?) (?P<a>\d{3})(?:-(?P<b>\d{3}))? \((?P<y>\d{4})\)", release_name)
        if not m:
            return None
        a, b = int(m["a"]), int(m["b"] or m["a"])
        return Parsed(m["t"], tuple(float(n) for n in range(a, b + 1)), year=int(m["y"]))

    def name_tokens(self, title: TitleDetails, item: Item) -> dict[str, object]:
        return {}


CASES = """
# release name                        | title | numbers     | year
Saga 061 (2023) (Digital).cbz         | Saga  | 61          | 2023
Saga 001-006 (2012) TPB.cbr           | Saga  | 1,2,3,4,5,6 | 2012
Paper Girls 030 (2019).cbz            | Paper Girls |       |
Not a comic.mkv                       | -
"""


@pytest.mark.parametrize(("name", "want"), parse_cases(Comics, CASES))
def test_parse_cases(name: str, want: list[str]) -> None:
    check_parse(Comics, name, want)


def test_a_wrong_case_says_what_differs(tmp_path: Any) -> None:
    table = tmp_path / "cases.txt"
    table.write_text("Saga 061 (2023).cbz | Saga | 62\nNot a comic.mkv | Saga\n")
    (_, want1), (_, want2) = [p.values for p in parse_cases(Comics, table)]
    with pytest.raises(AssertionError, match=r"numbers \(61.0,\), want \(62.0,\)"):
        check_parse(Comics, "Saga 061 (2023).cbz", want1)
    with pytest.raises(AssertionError, match="parse gave None"):
        check_parse(Comics, "Not a comic.mkv", want2)
