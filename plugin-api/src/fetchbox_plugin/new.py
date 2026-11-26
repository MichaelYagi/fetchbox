"""``fetchbox plugin new <name>``: a new plugin project that works, passes its tests and
``plugin check`` from the first minute.

    fetchbox-hello/
      pyproject.toml           the package, its entry point, its dev dependencies
      src/fetchbox_hello/      the plugin (an add-on that writes hello.txt beside imports)
      tests/                   tests using the test kit (a real Fetchbox, offline)
      README.md
      .github/workflows/ci.yml

``--kind media`` makes a media kind (a small comics kind with a parser) instead.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Literal

from fetchbox_plugin.manifest import PLUGIN_ID

Kind = Literal["addon", "media"]
KINDS: tuple[Kind, ...] = ("addon", "media")


class NewPluginError(ValueError):
    pass


def names(name: str) -> dict[str, str]:
    """hello -> package fetchbox-hello, module fetchbox_hello, id hello, class Hello."""
    pid = name.strip().lower()
    if pid.startswith("fetchbox-"):
        pid = pid.removeprefix("fetchbox-")
    if not PLUGIN_ID.match(pid):
        raise NewPluginError(
            f"{name!r}: a plugin name is 2 to 40 lowercase letters, digits, - or _, "
            "starting with a letter (it becomes the plugin's permanent id)"
        )
    words = re.split(r"[-_]", pid)
    return {
        "id": pid,
        "package": f"fetchbox-{pid}",
        "module": "fetchbox_" + pid.replace("-", "_"),
        "cls": "".join(w.capitalize() for w in words),
        "title": " ".join(w.capitalize() for w in words),
        "kind_id": re.sub(r"[^a-z0-9_]", "", pid.replace("-", "_"))[:10] or "kind",
    }


def create(name: str, where: str | Path = ".", *, kind: Kind = "addon") -> Path:
    """Write the project; its folder. Refuses to write into a folder that exists."""
    if kind not in KINDS:
        raise NewPluginError(f"--kind is one of {', '.join(KINDS)}")
    n = names(name)
    root = Path(where) / n["package"]
    if root.exists():
        raise NewPluginError(f"{root} already exists")
    files = _files(n, kind)
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text.lstrip("\n"), encoding="utf-8")
    return root


def _files(n: dict[str, str], kind: Kind) -> dict[str, str]:
    mod = n["module"]
    code, test, what = (
        (_ADDON, _ADDON_TEST, _ADDON_WHAT)
        if kind == "addon"
        else (_MEDIA, _MEDIA_TEST, _MEDIA_WHAT)
    )
    fill = {**n, "what": what.format(**n)}
    return {
        "pyproject.toml": _PYPROJECT.format(**fill),
        f"src/{mod}/__init__.py": code.format(**fill),
        "tests/conftest.py": _CONFTEST,
        f"tests/test_{mod.removeprefix('fetchbox_')}.py": test.format(**fill),
        "README.md": _README.format(**fill),
        ".github/workflows/ci.yml": _CI,
        ".gitignore": "__pycache__/\n*.egg-info/\n.venv/\ndist/\n.pytest_cache/\n",
    }


_ADDON_WHAT = "Writes hello.txt beside every imported movie or episode."
_MEDIA_WHAT = "{title}: a media kind (series and issues, filed as CBZ)."

_PYPROJECT = """
[project]
name = "{package}"
version = "0.1.0"
description = "{what}"
readme = "README.md"
requires-python = ">=3.12"
dependencies = ["fetchbox-plugin-api>=1,<2"]

[project.entry-points."fetchbox.plugins"]
{id} = "{module}:{cls}Plugin"

[dependency-groups]
# the test kit runs a real Fetchbox, offline
dev = ["fetchbox", "pytest>=8", "pytest-asyncio>=0.24"]

[build-system]
requires = ["hatchling>=1.25"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/{module}"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
pythonpath = ["src"]
"""

_ADDON = '''
"""{title}: {what}"""

from fetchbox_plugin import Addon, Field, Permission, Plugin, PublicEvent, Settings, on


class {cls}Settings(Settings):
    text: str = Field("Hello from {title}!", title="What to write", help="Written to hello.txt")


class {cls}(Addon):
    id = "{id}"
    name = "{title}"
    applies_to = {{"movie", "series"}}  # media kinds it works on
    settings = {cls}Settings

    @on("file.imported")
    async def write_hello(self, ctx, event: PublicEvent) -> None:
        """A movie or episode was filed: write hello.txt beside it. Fetchbox renames,
        moves and deletes it together with the file from now on."""
        for file_id in event.file_ids:
            await ctx.files.add_sidecar(file_id, "hello.txt", self.settings.text.encode())
        ctx.log.info("wrote hello.txt", files=len(event.file_ids))


class {cls}Plugin(Plugin):
    id = "{id}"
    name = "{title}"
    description = "{what}"
    fetchbox_api = 1
    permissions = {{Permission.LIBRARY_FILES}}
    contributes = [{cls}]
'''

_ADDON_TEST = """
from {module} import {cls}Plugin


async def test_hello_is_written_beside_an_imported_movie(fetchbox_app):
    app = await fetchbox_app(plugins=[{cls}Plugin])
    f = await app.add_file("Dune", 2021)  # in the library, then file.imported
    assert f.path.with_suffix(".hello.txt").read_text() == "Hello from {title}!"


async def test_the_text_comes_from_the_settings(fetchbox_app):
    app = await fetchbox_app(plugins=[{cls}Plugin], settings={{"{id}": {{"text": "Hi"}}}})
    f = await app.add_file("Arrival", 2016)
    assert f.path.with_suffix(".hello.txt").read_text() == "Hi"


async def test_music_is_left_alone(fetchbox_app):
    app = await fetchbox_app(plugins=[{cls}Plugin])
    assert await app.emit("file.imported", kind="music", file_ids=["x"]) == []
"""

_MEDIA = '''
"""{title}: {what}"""

import re

from fetchbox_plugin import (
    Hit,
    Item,
    MediaKind,
    Naming,
    Parsed,
    Permission,
    Plugin,
    Qualities,
    TitleDetails,
)

ISSUE = re.compile(
    r"^(?P<title>.+?)\\s+#?(?P<first>\\d{{1,4}})(?:-(?P<last>\\d{{1,4}}))?\\s+\\((?P<year>\\d{{4}})\\)"
)


class {cls}(MediaKind):
    id = "{kind_id}"
    name = "{title}"
    levels = ("series", "issue")
    file_types = {{".cbz", ".cbr"}}
    qualities = Qualities(["CBZ", "CBR"])
    naming = Naming(
        "{{series}} ({{start_year}})/{{series}} #{{issue:03}}",
        tokens={{"series", "start_year", "issue"}},
    )

    async def search(self, ctx, text: str) -> list[Hit]:
        # Ask your metadata source here (with ctx.http, and Permission.NETWORK)
        return [Hit(ref="saga", name="Saga", year=2012)] if "saga" in text.lower() else []

    async def title(self, ctx, ref: str) -> TitleDetails:
        return TitleDetails(ref, "Saga", 2012, items=[Item(str(n), n) for n in range(1, 67)])

    def parse(self, release_name: str) -> Parsed | None:
        m = ISSUE.match(release_name)
        if not m:
            return None
        first, last = int(m["first"]), int(m["last"] or m["first"])
        quality = "CBR" if release_name.lower().endswith(".cbr") else "CBZ"
        return Parsed(m["title"], tuple(float(n) for n in range(first, last + 1)),
                      year=int(m["year"]), quality=quality)

    def name_tokens(self, title: TitleDetails, item: Item) -> dict[str, object]:
        return {{"series": title.name, "start_year": title.year, "issue": item.number}}


class {cls}Plugin(Plugin):
    id = "{id}"
    name = "{title}"
    description = "{what}"
    fetchbox_api = 1
    permissions = set()
    contributes = [{cls}]
'''

_MEDIA_TEST = '''
import pytest

from fetchbox_plugin.testing import check_parse, parse_cases
from {module} import {cls}

CASES = """
# release name                     | title | issues | year | quality
Saga 061 (2023) (Digital).cbz      | Saga  | 61     | 2023 | CBZ
Saga #1-6 (2012) TPB.cbr           | Saga  | 1,2,3,4,5,6 | 2012 | CBR
Not a comic at all.mkv             | -
"""


@pytest.mark.parametrize(("name", "want"), parse_cases({cls}, CASES))
def test_parse(name, want):
    check_parse({cls}, name, want)


async def test_search_and_title():
    kind = {cls}()
    [hit] = await kind.search(None, "saga")
    title = await kind.title(None, hit.ref)
    item = title.items[60]
    assert kind.naming.pattern.format(**kind.name_tokens(title, item)) == "Saga (2012)/Saga #061"
'''

_CONFTEST = """
# The test kit: the fetchbox_app fixture (a real Fetchbox, offline) and more
pytest_plugins = ["fetchbox_plugin.testing"]
"""

_README = """
# {package}

{what}

A [Fetchbox](https://github.com/MichaelYagi/fetchbox) plugin.

## Develop

```sh
uv sync                      # or: pip install -e . fetchbox pytest pytest-asyncio
fetchbox plugin test .       # its tests, with a real Fetchbox (offline)
fetchbox plugin check .      # checked against the plugin API, with fixes
```

## Install

In Fetchbox: Settings > Plugins > Install from file, with the wheel from `uv build`.
"""

_CI = """
name: CI
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
      - run: uv sync --python 3.12
      - run: uv run fetchbox plugin check .
      - run: uv run fetchbox plugin test .
"""


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["new"]:
        args = args[1:]
    kind: Kind = "addon"
    if "--kind" in args:
        i = args.index("--kind")
        kind = args[i + 1] if i + 1 < len(args) else ""  # type: ignore[assignment]
        del args[i : i + 2]
    if len(args) != 1:
        print("usage: python -m fetchbox_plugin new <name> [--kind addon|media]", file=sys.stderr)
        return 2
    try:
        root = create(args[0], kind=kind)
    except NewPluginError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"created {root}\nnext: cd {root} && uv sync && fetchbox plugin test .")
    return 0
