from __future__ import annotations

from pathlib import Path

import pytest

from fetchbox.cli import main


def test_migrate_creates_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FETCHBOX_DB_URL", raising=False)
    assert main(["--data", str(tmp_path), "migrate"]) == 0
    assert (tmp_path / "fetchbox.db").exists()


def test_serve_refuses_lan_bind_without_password(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--data", str(tmp_path), "serve", "--host", "0.0.0.0", "--port", "18799"]) == 2
    assert "set an admin password first" in capsys.readouterr().err


def test_reset_password_from_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    assert main(["--data", str(tmp_path), "reset-password", "--password-stdin"]) == 2
    monkeypatch.setattr("sys.stdin", io.StringIO("long-enough-pw\n"))
    assert main(["--data", str(tmp_path), "reset-password", "--password-stdin"]) == 0
