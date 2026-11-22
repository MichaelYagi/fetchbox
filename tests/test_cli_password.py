"""FETCHBOX_ADMIN_PASSWORD(_FILE): a headless install (Docker) gets its admin password."""

from __future__ import annotations

from pathlib import Path

import pytest

from fetchbox import cli
from fetchbox.config import Settings


def test_initial_password_from_env_or_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FETCHBOX_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("FETCHBOX_ADMIN_PASSWORD_FILE", raising=False)
    assert cli._initial_password() is None
    monkeypatch.setenv("FETCHBOX_ADMIN_PASSWORD", "from-env-123")
    assert cli._initial_password() == "from-env-123"
    secret = tmp_path / "pw"
    secret.write_text("from-file-456\n")
    monkeypatch.setenv("FETCHBOX_ADMIN_PASSWORD_FILE", str(secret))
    assert cli._initial_password() == "from-file-456"  # the file wins, newline dropped


def test_set_password_makes_the_lan_bind_allowed(settings: Settings) -> None:
    assert not cli._has_admin_password(settings)
    assert cli._set_password(settings, "a-long-enough-password") == 0
    assert cli._has_admin_password(settings)
    assert cli._set_password(settings, "x") == 2  # too short: refused, explained
