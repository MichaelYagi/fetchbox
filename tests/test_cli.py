from __future__ import annotations

from pathlib import Path

import pytest

from fetchbox.cli import main


def test_migrate_creates_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FETCHBOX_DB_URL", raising=False)
    assert main(["--data", str(tmp_path), "migrate"]) == 0
    assert (tmp_path / "fetchbox.db").exists()


def test_reset_password_from_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    assert main(["--data", str(tmp_path), "reset-password", "--password-stdin"]) == 2
    monkeypatch.setattr("sys.stdin", io.StringIO("long-enough-pw\n"))
    assert main(["--data", str(tmp_path), "reset-password", "--password-stdin"]) == 0


def test_parse_prints_json_per_name(capsys: pytest.CaptureFixture[str]) -> None:
    import json

    assert main(["parse", "Heat.1995.1080p.BluRay.x264-GRP", "Show.S01E02.720p.HDTV"]) == 0
    movie, show = (json.loads(line) for line in capsys.readouterr().out.splitlines())
    assert movie["title"] == "Heat" and movie["year"] == 1995
    assert movie["quality"] == "Bluray-1080p" and movie["media_type"] == "movie"
    assert show["seasons"] == [1] and show["episodes"] == [2] and show["media_type"] == "series"


def test_data_dir_after_the_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`fetchbox migrate --data DIR` works like `fetchbox --data DIR migrate`."""
    monkeypatch.delenv("FETCHBOX_DB_URL", raising=False)
    assert main(["migrate", "--data", str(tmp_path / "after")]) == 0
    assert (tmp_path / "after" / "fetchbox.db").exists()
    assert main(["--data", str(tmp_path / "before"), "migrate"]) == 0
    assert (tmp_path / "before" / "fetchbox.db").exists()
