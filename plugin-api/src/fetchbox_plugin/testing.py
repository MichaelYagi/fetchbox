"""The test kit: a real Fetchbox in each test, with your plugin loaded, offline.

In a plugin's tests (pytest with pytest-asyncio, ``asyncio_mode = "auto"``):

    # conftest.py: makes the fetchbox_app fixture available to every test
    pytest_plugins = ["fetchbox_plugin.testing"]

    # test_hello.py
    async def test_hello_is_written(fetchbox_app):
        app = await fetchbox_app(plugins=[HelloPlugin], settings={"hello": {"word": "Hi"}})
        f = await app.add_file("Dune", 2021)          # a movie in the library, then file.imported
        assert f.path.with_suffix(".hello.txt").read_text() == "Hi"

``app.emit("title.added", title_id=..., kind="movie")`` fires any public event at your
handlers. ``parse_cases`` reads a table of release names for a media kind's ``parse``.

The kit runs Fetchbox itself, so a plugin's tests need it installed (a dev dependency):
``pip install fetchbox``. The rest of fetchbox_plugin never needs Fetchbox.
"""

from __future__ import annotations

import itertools
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from fetchbox_plugin.kind import MediaKind, Parsed
from fetchbox_plugin.manifest import Plugin

if TYPE_CHECKING:
    from fetchbox.testing import App

_n = itertools.count()


def _engine() -> Any:
    try:
        from fetchbox import testing
    except ImportError as exc:
        raise RuntimeError(
            "the test kit runs a real Fetchbox: add fetchbox to your dev dependencies "
            f"(pip install fetchbox) ({exc})"
        ) from None
    return testing


@pytest.fixture
async def fetchbox_app(
    tmp_path: Path,
) -> AsyncIterator[Callable[..., Awaitable[App]]]:
    """``app = await fetchbox_app(plugins=[...], settings={plugin id: {...}})``: Fetchbox
    started in a temporary folder with the plugins loaded; stopped after the test."""
    engine = _engine()
    async with AsyncExitStack() as stack:

        async def make(
            plugins: Sequence[type[Plugin]], settings: dict[str, dict[str, Any]] | None = None
        ) -> App:
            root = tmp_path / f"fetchbox-{next(_n)}"
            app: App = await stack.enter_async_context(
                engine.start(plugins, root=root, settings=settings)
            )
            return app

        yield make


NONE = "-"  # in a parse_cases table: the kind doesn't take this release (parse gives None)


def parse_cases(kind: type[MediaKind], table: str | Path) -> list[Any]:
    """Table-driven tests for ``parse``: one release name per line, then what it should
    give, separated by ``|``: title | numbers | year | quality (empty: not checked; ``-``
    on its own: parse gives None). A line starting with ``#`` is a comment.

        Saga 061 (2023) (Digital).cbz | Saga | 61 | 2023 |
        Saga 001-006 (2012) TPB.cbr   | Saga | 1,2,3,4,5,6 | 2012
        Not a comic.mkv               | -

    Use with ``@pytest.mark.parametrize("name, want", parse_cases(MyKind, "cases.txt"))``
    and ``check_parse(MyKind, name, want)``."""
    is_file = isinstance(table, Path) or "\n" not in table  # else the table itself
    text = Path(table).read_text("utf-8") if is_file else str(table)
    out = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):  # a comment line ("#" elsewhere is the name's)
            continue
        name, *fields = [f.strip() for f in line.split("|")]
        out.append(pytest.param(name, fields, id=f"{kind.id}:{n}:{name[:40]}"))
    return out


def check_parse(kind: type[MediaKind], name: str, want: list[str]) -> None:
    """Assert ``kind().parse(name)`` matches a parse_cases row."""
    got: Parsed | None = kind().parse(name)
    if want[:1] == [NONE]:
        assert got is None, f"{name!r}: parse gave {got}, the table says it isn't this kind's"
        return
    assert got is not None, f"{name!r}: parse gave None"
    title, numbers, year, quality = [*want, "", "", "", ""][:4]
    if title:
        assert got.title == title, f"{name!r}: title {got.title!r}, want {title!r}"
    if numbers:
        nums = tuple(float(x) for x in numbers.split(","))
        assert got.numbers == nums, f"{name!r}: numbers {got.numbers}, want {nums}"
    if year:
        assert got.year == int(year), f"{name!r}: year {got.year}, want {year}"
    if quality:
        assert got.quality == quality, f"{name!r}: quality {got.quality!r}, want {quality!r}"
