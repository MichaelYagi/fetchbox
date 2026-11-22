"""The secrets key in the system keyring (Windows desktop app; FETCHBOX_KEYRING=1).
An in-memory stand-in for the keyring: tests never touch a real one."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from fetchbox.core import secrets
from fetchbox.core.secrets import KEY_FILENAME, SecretBox, SecretsError, load_or_create


class FakeKeyring:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}
        self.fail_get = False
        self.fail_set = False

    def get_password(self, service: str, account: str) -> str | None:
        if self.fail_get:
            raise RuntimeError("access denied")
        return self.store.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        if self.fail_set:
            raise RuntimeError("no keyring backend")
        self.store[(service, account)] = value


@pytest.fixture
def kr(monkeypatch: pytest.MonkeyPatch) -> FakeKeyring:
    fake = FakeKeyring()
    monkeypatch.setenv("FETCHBOX_KEYRING", "1")
    monkeypatch.setattr(secrets, "_keyring", lambda: fake)
    return fake


def test_default_is_the_windows_desktop_app_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FETCHBOX_KEYRING", raising=False)
    monkeypatch.setattr(secrets.sys, "platform", "win32")
    assert not secrets.keyring_wanted()  # running from source
    monkeypatch.setattr(secrets.sys, "frozen", True, raising=False)
    assert secrets.keyring_wanted()
    monkeypatch.setenv("FETCHBOX_KEYRING", "0")
    assert not secrets.keyring_wanted()


def test_new_install_keeps_the_key_in_the_keyring(tmp_path: Path, kr: FakeKeyring) -> None:
    box, created = load_or_create(tmp_path)
    assert created and not (tmp_path / KEY_FILENAME).exists()
    token = box.encrypt("indexer-password", aad="x")
    again, created = load_or_create(tmp_path)
    assert not created and again.decrypt(token, aad="x") == "indexer-password"
    # another data folder gets its own key
    other, created = load_or_create(tmp_path / "other")
    assert created and len(kr.store) == 2
    with pytest.raises(SecretsError):
        other.decrypt(token, aad="x")
    # backups "with key" read it from the keyring
    raw = secrets.key_file_bytes(tmp_path)
    assert raw is not None and SecretBox(base64.b64decode(raw.strip())).decrypt(token, aad="x")


def test_an_existing_key_file_moves_into_the_keyring(
    tmp_path: Path, kr: FakeKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FETCHBOX_KEYRING", "0")
    box, _ = load_or_create(tmp_path)  # an install from before: a key file
    token = box.encrypt("s3cret", aad="y")
    monkeypatch.setenv("FETCHBOX_KEYRING", "1")
    moved, created = load_or_create(tmp_path)
    assert not created and not (tmp_path / KEY_FILENAME).exists()
    assert moved.decrypt(token, aad="y") == "s3cret"
    assert load_or_create(tmp_path)[0].decrypt(token, aad="y") == "s3cret"


def test_a_key_file_wins_over_the_keyring(tmp_path: Path, kr: FakeKeyring) -> None:
    load_or_create(tmp_path)  # keyring holds key A
    key_b = bytes(range(32))  # a restored backup puts key B in a file
    (tmp_path / KEY_FILENAME).write_text(base64.b64encode(key_b).decode() + "\n")
    token = SecretBox(key_b).encrypt("from-backup", aad="z")
    box, _ = load_or_create(tmp_path)
    assert box.decrypt(token, aad="z") == "from-backup"
    assert not (tmp_path / KEY_FILENAME).exists()
    assert load_or_create(tmp_path)[0].decrypt(token, aad="z") == "from-backup"


def test_keyring_failures(tmp_path: Path, kr: FakeKeyring) -> None:
    load_or_create(tmp_path)
    kr.fail_get = True  # e.g. access refused: never a new key (old secrets unreadable)
    with pytest.raises(SecretsError, match="couldn't read Fetchbox's secrets key"):
        load_or_create(tmp_path)
    assert not (tmp_path / KEY_FILENAME).exists()

    kr.fail_get, kr.fail_set = False, True  # no usable keyring: a key file instead
    box, created = load_or_create(tmp_path / "fresh")
    assert created and (tmp_path / "fresh" / KEY_FILENAME).exists()
    token = box.encrypt("v", aad="w")
    assert load_or_create(tmp_path / "fresh")[0].decrypt(token, aad="w") == "v"
