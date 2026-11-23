from __future__ import annotations

from pathlib import Path

import pytest

from fetchbox.config import load_settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("FETCHBOX_DATA_DIR", "FETCHBOX_PORT", "FETCHBOX_HOST", "FETCHBOX_DB_URL"):
        monkeypatch.delenv(var, raising=False)


def test_defaults(tmp_path: Path) -> None:
    s = load_settings(tmp_path)
    assert s.host == "0.0.0.0"  # other devices on the network can open it
    assert s.port == 8787
    assert not s.is_loopback
    assert s.database_url.startswith("sqlite+aiosqlite:///")
    assert s.database_url.endswith("fetchbox.db")


def test_precedence_cli_over_env_over_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "config.toml").write_text('port = 1111\nhost = "127.0.0.1"\nlog_level = "debug"\n')
    assert load_settings(tmp_path).port == 1111
    assert load_settings(tmp_path).log_level == "debug"

    monkeypatch.setenv("FETCHBOX_PORT", "2222")
    assert load_settings(tmp_path).port == 2222

    assert load_settings(tmp_path, port=3333).port == 3333
    # None means "flag not passed" and must not override lower layers
    assert load_settings(tmp_path, port=None).port == 2222


def test_data_dir_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FETCHBOX_DATA_DIR", str(tmp_path / "x"))
    assert load_settings().data_dir == (tmp_path / "x").resolve()


def test_lan_bind_is_not_loopback(tmp_path: Path) -> None:
    assert not load_settings(tmp_path, host="0.0.0.0").is_loopback
