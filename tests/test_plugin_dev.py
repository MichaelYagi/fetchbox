"""``fetchbox plugin dev``: the plugin is loaded from its source and loaded again when a file
of it is saved; a mistake is reported and the session carries on."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from fetchbox import cli
from fetchbox.plugin_dev import DevError, DevSession
from fetchbox.testing import start
from fetchbox_plugin.new import create


def _save(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))  # surely newer


async def test_a_saved_change_is_loaded_again(tmp_path: Path) -> None:
    project = create("devhello", tmp_path / "work")
    code = project / "src" / "fetchbox_devhello" / "__init__.py"
    async with start([], root=tmp_path / "fb") as app:
        session = DevSession(app.host, project)
        assert await session.load() == "devhello"
        assert await session.check() is None  # nothing changed
        f = await app.add_file("Dune", 2021)
        assert f.path.with_suffix(".hello.txt").read_text() == "Hello from Devhello!"

        _save(code, code.read_text().replace("Hello from Devhello!", "Changed!"))
        assert await session.check() == "reloaded devhello"
        f = await app.add_file("Arrival", 2016)
        assert f.path.with_suffix(".hello.txt").read_text() == "Changed!"

        good = code.read_text()
        _save(code, good + "\ndef broken(:\n")
        msg = await session.check()
        assert msg is not None and msg.startswith("not loaded: SyntaxError")
        assert "devhello" not in app.host.loaded  # off until it's fixed

        _save(code, good)
        assert await session.check() == "reloaded devhello"
        assert "devhello" in app.host.loaded


def test_a_project_needs_its_entry_point(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')
    with pytest.raises(DevError, match="entry point"):
        DevSession(None, tmp_path)  # type: ignore[arg-type]


def test_the_command_is_there(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["plugin", "dev", "--help"])
    out = capsys.readouterr().out
    assert "--port" in out and "--no-browser" in out
