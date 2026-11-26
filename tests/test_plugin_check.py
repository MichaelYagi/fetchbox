"""``fetchbox plugin check``: real plugin projects on disk, each with one kind of mistake,
and the fix the check prints for it."""

from __future__ import annotations

import itertools
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from fetchbox import cli
from fetchbox_plugin.check import Report, check_plugin

_n = itertools.count()

GOOD = """
from fetchbox_plugin import Addon, Permission, Plugin, on


class Hello(Addon):
    id = "hello"
    name = "Hello"

    @on("file.imported")
    async def wave(self, ctx, event):
        await ctx.files.add_sidecar(event.file, "hello.txt", b"Hello")


class HelloPlugin(Plugin):
    id = "{pid}"
    name = "Hello"
    description = "Writes hello.txt beside every imported file."
    permissions = {{Permission.LIBRARY_FILES}}
    contributes = [Hello]
"""


def project(
    tmp_path: Path,
    code: str = GOOD,
    *,
    deps: str = '["fetchbox-plugin-api>=1,<2"]',
    entry: str | None = None,
    name: str = "fetchbox-hello",
) -> tuple[Path, str]:
    """A plugin project: pyproject.toml and src/<module>/__init__.py (module names differ
    per test, since the check imports them)."""
    mod = f"fetchbox_check_t{next(_n)}"
    pid = "hello"
    root = tmp_path / name
    (root / "src" / mod).mkdir(parents=True)
    (root / "src" / mod / "__init__.py").write_text(textwrap.dedent(code).format(pid=pid))
    eps = (
        ""
        if entry == ""
        else '\n[project.entry-points."fetchbox.plugins"]\n'
        + (entry or f'{pid} = "{mod}:HelloPlugin"')
        + "\n"
    )
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "0.1.0"\ndependencies = {deps}\n{eps}'
    )
    return root, mod


def problems(r: Report) -> list[tuple[str, str]]:
    return [(p.level, p.message) for p in r.problems]


def test_a_good_plugin_passes(tmp_path: Path) -> None:
    root, _ = project(tmp_path)
    r = check_plugin(root)
    assert r.ok and r.problems == [] and r.plugin == "hello"
    assert r.text() == "hello: no problems found (API 1)"


def test_not_a_project(tmp_path: Path) -> None:
    r = check_plugin(tmp_path)
    assert not r.ok and "no pyproject.toml" in r.problems[0].message


def test_the_package_needs_the_api_and_an_entry_point(tmp_path: Path) -> None:
    root, _ = project(tmp_path, deps='["httpx"]', entry="", name="hello-plugin")
    r = check_plugin(root)
    msgs = problems(r)
    assert ("warning", "the package is 'hello-plugin'") in msgs
    assert ("error", "fetchbox-plugin-api isn't a dependency") in msgs
    assert any("no fetchbox.plugins entry point" in m for _, m in msgs)
    assert "fetchbox-plugin-api>=1,<2" in r.text()  # the fix is printed


def test_the_entry_point_is_named_after_the_id(tmp_path: Path) -> None:
    root, _ = project(tmp_path, entry=None)
    text = (root / "pyproject.toml").read_text().replace('hello = "', 'greeter = "')
    (root / "pyproject.toml").write_text(text)
    r = check_plugin(root)
    assert ("error", "the entry point is 'greeter' but the plugin's id is 'hello'") in problems(r)


def test_a_declaration_mistake_is_reported_with_its_own_message(tmp_path: Path) -> None:
    root, mod = project(tmp_path, GOOD.replace('"file.imported"', '"file.imprted"'))
    r = check_plugin(root)
    assert not r.ok
    assert r.problems[0].where == mod
    assert "not public events: file.imprted" in r.problems[0].message


def test_permissions_are_compared_with_the_code(tmp_path: Path) -> None:
    code = GOOD.replace(
        '        await ctx.files.add_sidecar(event.file, "hello.txt", b"Hello")',
        '        r = await ctx.http.get("https://example.com")\n'
        '        await ctx.files.add_sidecar(event.file, "hello.txt", r.content)\n'
        '        await ctx.library.search("x")\n'
        '        await ctx.notify("Hello")',
    ).replace("{{Permission.LIBRARY_FILES}}", "{{Permission.LIBRARY_FILES, Permission.LIBRARY}}")
    root, mod = project(tmp_path, code)
    r = check_plugin(root)
    errors = {p.message: p for p in r.problems if p.level == "error"}
    assert set(errors) == {
        "uses NETWORK but doesn't declare it",
        "uses NOTIFY but doesn't declare it",
    }
    net = errors["uses NETWORK but doesn't declare it"]
    assert net.where == f"{mod}/__init__.py:11 (ctx.http)"
    assert net.fix == "permissions = {..., Permission.NETWORK}"


def test_a_permission_never_used_is_a_warning(tmp_path: Path) -> None:
    code = GOOD.replace(
        "{{Permission.LIBRARY_FILES}}", "{{Permission.LIBRARY_FILES, Permission.NOTIFY}}"
    )
    r = check_plugin(project(tmp_path, code)[0])
    assert r.ok
    assert problems(r) == [("warning", "declares NOTIFY but the code doesn't seem to use it")]


def test_importing_a_network_library_is_network(tmp_path: Path) -> None:
    code = "import httpx\n" + GOOD
    r = check_plugin(project(tmp_path, code)[0])
    assert ("error", "uses NETWORK but doesn't declare it") in problems(r)


def test_fetchbox_internals_are_flagged(tmp_path: Path) -> None:
    code = (
        "from fetchbox.core.kinds import CORE_KEYS\nfrom fetchbox.plugin import Plugin as P\n"
        + GOOD
    )
    r = check_plugin(project(tmp_path, code)[0])
    assert r.ok  # a warning: it may break, it isn't wrong today
    [w] = [p for p in r.problems if "internals" in p.message]
    assert w.message.startswith("imports fetchbox.core.kinds") and w.where.endswith(":1")


@pytest.mark.parametrize(
    ("change", "want"),
    [
        (("    fetchbox_api = 1\n", "    fetchbox_api = 2\n"), "fetchbox_api = 2: this API is [1]"),
        (("    contributes = [Hello]", "    contributes = [Hello()]"), "a class, not an instance"),
        (("    contributes = [Hello]", "    contributes = [dict]"), "isn't a MediaKind, an Addon"),
        (("    contributes = [Hello]", "    contributes = []"), "contributes nothing"),
        (('    id = "{pid}"', '    id = "Hello World"'), "id 'Hello World'"),
    ],
)
def test_manifest_mistakes(tmp_path: Path, change: tuple[str, str], want: str) -> None:
    code = GOOD.replace(
        '    name = "Hello"\n    description',
        '    name = "Hello"\n    fetchbox_api = 1\n    description',
    )
    assert change[0] in code
    r = check_plugin(project(tmp_path, code.replace(*change))[0])
    assert not r.ok and any(want in p.message for p in r.problems), r.text()


def test_fetchbox_plugin_check_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good, _ = project(tmp_path / "a")
    assert cli.main(["plugin", "check", str(good)]) == 0
    assert "no problems found" in capsys.readouterr().out
    bad, _ = project(tmp_path / "b", deps="[]")
    assert cli.main(["plugin", "check", str(bad)]) == 1
    assert (
        "error: pyproject.toml: fetchbox-plugin-api isn't a dependency" in capsys.readouterr().out
    )


def test_runs_without_fetchbox(tmp_path: Path) -> None:
    """python -m fetchbox_plugin check: the API package alone (no fetchbox import)."""
    root, _ = project(tmp_path)
    probe = (
        "import sys, runpy; sys.modules['fetchbox'] = None; sys.argv = ['x', 'check', sys.argv[1]];"
        " runpy.run_module('fetchbox_plugin', run_name='__main__')"
    )
    r = subprocess.run([sys.executable, "-c", probe, str(root)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "hello: no problems found" in r.stdout
