"""fetchbox corpus capture: release names from your indexers, new ones only."""

from __future__ import annotations

from pathlib import Path

from fetchbox.config import Settings
from fetchbox.corpus import _known, capture


def test_known_names_come_from_the_fixtures() -> None:
    known = _known()
    assert len(known) > 500 and "[SubsPlease] Jujutsu Kaisen - 24 (1080p) [ABCD1234].mkv" in known


def test_capture_without_indexers_writes_an_empty_file(settings: Settings, tmp_path: Path) -> None:
    import asyncio

    from fetchbox import migrations

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    migrations.upgrade(settings.database_url)  # runs its own event loop: not in async
    out = tmp_path / "captured.jsonl"
    assert asyncio.run(capture(settings, out, ["Dune"], 10)) == 0
    assert out.read_text() == ""
