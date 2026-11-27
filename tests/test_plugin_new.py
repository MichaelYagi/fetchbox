"""``fetchbox plugin new``: the project it makes passes ``plugin check`` and its own tests
(run with ``fetchbox plugin test``, so with the real test kit) straight away."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from fetchbox import cli
from fetchbox_plugin.check import check_plugin
from fetchbox_plugin.new import NewPluginError, create, names


def test_names() -> None:
    n = names("open-subtitles")
    assert (n["id"], n["package"], n["module"], n["cls"], n["title"]) == (
        "open-subtitles", "fetchbox-open-subtitles", "fetchbox_open_subtitles",
        "OpenSubtitles", "Open Subtitles",
    )  # fmt: skip
    assert names("fetchbox-hello")["id"] == "hello"
    with pytest.raises(NewPluginError, match="2 to 40"):
        names("Hello World")


@pytest.mark.parametrize("kind", ["addon", "media"])
def test_a_new_plugin_passes_check_and_its_tests(tmp_path: Path, kind: str) -> None:
    root = create(f"try-{kind}", tmp_path, kind=kind)  # type: ignore[arg-type]
    assert (root / "pyproject.toml").is_file() and (root / ".github/workflows/ci.yml").is_file()
    report = check_plugin(root)
    assert report.ok and report.problems == [], report.text()
    # its tests, through the command (a separate pytest, the plugin's src on the path)
    r = subprocess.run(
        [sys.executable, "-m", "fetchbox", "plugin", "test", str(root), "--", "-q"],
        capture_output=True, text=True, timeout=300,
    )  # fmt: skip
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
    assert " passed" in r.stdout and "failed" not in r.stdout


def test_new_refuses_an_existing_folder(tmp_path: Path) -> None:
    create("hello", tmp_path)
    with pytest.raises(NewPluginError, match="already exists"):
        create("hello", tmp_path)


def test_the_commands(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["plugin", "new", "hello", "--dir", str(tmp_path)]) == 0
    assert "created" in capsys.readouterr().out
    assert cli.main(["plugin", "new", "Bad Name", "--dir", str(tmp_path)]) == 1
    assert cli.main(["plugin", "test", str(tmp_path / "nowhere")]) == 2
    r = subprocess.run([sys.executable, "-m", "fetchbox_plugin", "new", "via-module"],
                       cwd=tmp_path, capture_output=True, text=True)  # fmt: skip
    assert r.returncode == 0 and (tmp_path / "fetchbox-via-module").is_dir(), r.stderr


def test_a_new_plugin_gets_fetchbox_from_its_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fetchbox and the API aren't on PyPI: uv is pointed at the running Fetchbox's release
    files, so `uv sync` works; nothing for a version that isn't a release."""
    import importlib.metadata as md
    import tomllib

    from fetchbox_plugin import new

    versions = {"fetchbox": "0.10.3", "fetchbox-plugin-api": "1.0.0"}
    monkeypatch.setattr(md, "version", lambda name: versions[name])
    root = new.create("hello", tmp_path)
    sources = tomllib.loads((root / "pyproject.toml").read_text())["tool"]["uv"]["sources"]
    base = "https://github.com/MichaelYagi/fetchbox/releases/download/v0.10.3"
    assert sources == {
        "fetchbox": {"url": f"{base}/fetchbox-0.10.3-py3-none-any.whl"},
        "fetchbox-plugin-api": {"url": f"{base}/fetchbox_plugin_api-1.0.0-py3-none-any.whl"},
    }
    versions["fetchbox"] = "0.10.4.dev3"  # a build from source: no release to point at
    root = new.create("other", tmp_path)
    assert "sources" not in tomllib.loads((root / "pyproject.toml").read_text()).get(
        "tool", {}
    ).get("uv", {})
