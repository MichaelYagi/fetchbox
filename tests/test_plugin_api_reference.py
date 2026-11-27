"""docs/plugin-api.md is the plugin API as it is: regenerated whenever the API changes."""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "plugin-api" / "scripts" / "reference.py"


def test_the_reference_is_up_to_date() -> None:
    spec = importlib.util.spec_from_file_location("reference", SCRIPT)
    assert spec is not None and spec.loader is not None
    ref = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ref)
    assert ref.OUT.read_text(encoding="utf-8") == ref.render(), (
        "the plugin API changed: run `uv run python plugin-api/scripts/reference.py`"
    )
