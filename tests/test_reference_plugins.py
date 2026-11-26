"""The reference plugins in plugins/ are built with the plugin API alone: each passes
`fetchbox plugin check` and its own tests, so a change to the API that breaks them fails here."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from fetchbox_plugin.check import check_plugin

PLUGINS = sorted(
    p for p in (Path(__file__).parents[1] / "plugins").iterdir() if (p / "pyproject.toml").is_file()
)


def test_there_are_reference_plugins() -> None:
    assert {p.name for p in PLUGINS} >= {"fetchbox-subtitles"}


@pytest.mark.parametrize("plugin", PLUGINS, ids=lambda p: p.name)
def test_a_reference_plugin_passes_check_and_its_tests(plugin: Path) -> None:
    report = check_plugin(plugin)
    assert report.ok and report.problems == [], report.text()
    r = subprocess.run(
        [sys.executable, "-m", "fetchbox", "plugin", "test", str(plugin), "--", "-q"],
        capture_output=True, text=True, timeout=600,
    )  # fmt: skip
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]
    assert " passed" in r.stdout and "failed" not in r.stdout
