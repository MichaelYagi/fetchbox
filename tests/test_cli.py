from __future__ import annotations

from pathlib import Path

import pytest

from fetchbox.cli import main


def test_migrate_creates_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FETCHBOX_DB_URL", raising=False)
    assert main(["--data", str(tmp_path), "migrate"]) == 0
    assert (tmp_path / "fetchbox.db").exists()


def test_serve_refuses_lan_bind_without_password(tmp_path: Path) -> None:
    assert main(["--data", str(tmp_path), "serve", "--host", "0.0.0.0"]) == 2
