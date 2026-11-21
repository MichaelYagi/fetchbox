from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel, SecretStr

from fetchbox.core.secrets import (
    KEY_FILENAME,
    MASK,
    SecretBox,
    SecretsError,
    is_encrypted,
    load_or_create,
    mask_settings,
    merge_update,
    open_settings,
    seal_settings,
)


class IndexerSettings(BaseModel):
    url: str
    username: str | None = None
    password: SecretStr | None = None
    passkey: SecretStr


def test_roundtrip_and_fresh_nonce() -> None:
    box = SecretBox.generate()
    a, b = box.encrypt("hunter2", aad="x"), box.encrypt("hunter2", aad="x")
    assert a != b and is_encrypted(a) and "hunter2" not in a
    assert box.decrypt(a, aad="x") == "hunter2"


def test_aad_binds_ciphertext_to_its_field() -> None:
    box = SecretBox.generate()
    token = box.encrypt("s3cret", aad="indexer:1:password")
    with pytest.raises(SecretsError):
        box.decrypt(token, aad="indexer:2:password")


def test_wrong_key_and_garbage_fail() -> None:
    token = SecretBox.generate().encrypt("x")
    with pytest.raises(SecretsError):
        SecretBox.generate().decrypt(token)
    with pytest.raises(SecretsError):
        SecretBox.generate().decrypt("enc:v1:not-base64!!")
    with pytest.raises(SecretsError):
        SecretBox.generate().decrypt("plain")


def test_key_file_created_once_and_private(tmp_path: Path) -> None:
    box1, created1 = load_or_create(tmp_path)
    box2, created2 = load_or_create(tmp_path)
    assert created1 and not created2
    assert box2.decrypt(box1.encrypt("same key")) == "same key"
    if sys.platform != "win32":
        assert stat.S_IMODE((tmp_path / KEY_FILENAME).stat().st_mode) == 0o600


def test_corrupt_key_file(tmp_path: Path) -> None:
    (tmp_path / KEY_FILENAME).write_text("not a key")
    with pytest.raises(SecretsError):
        load_or_create(tmp_path)


PW = "correct-horse-battery-staple"
PK = "0123456789abcdef-passkey"


def test_settings_seal_open_mask_merge() -> None:
    box = SecretBox.generate()
    s = IndexerSettings(
        url="https://idx", username="me", password=SecretStr(PW), passkey=SecretStr(PK)
    )
    stored = seal_settings(box, s, context="indexer:01J")
    assert stored["url"] == "https://idx" and stored["username"] == "me"
    assert is_encrypted(stored["password"]) and is_encrypted(stored["passkey"])
    # Long, distinctive secrets: a 2-letter one can appear in random base64 by chance.
    assert PW not in str(stored) and PK not in str(stored)

    opened = open_settings(box, IndexerSettings, stored, context="indexer:01J")
    assert opened.password is not None and opened.password.get_secret_value() == PW
    with pytest.raises(SecretsError):
        open_settings(box, IndexerSettings, stored, context="indexer:OTHER")

    view = mask_settings(IndexerSettings, stored)
    assert view["password"] == MASK and view["passkey"] == MASK and view["url"] == "https://idx"
    assert mask_settings(IndexerSettings, {**stored, "password": None})["password"] is None

    # Client sends the masked view back with one change: secrets are kept, not overwritten.
    merged = merge_update(IndexerSettings, stored, {**view, "username": "you"})
    again = open_settings(box, IndexerSettings, merged, context="indexer:01J")
    assert again.username == "you" and again.passkey.get_secret_value() == PK
    # A new value replaces the secret.
    merged = merge_update(IndexerSettings, stored, {"passkey": "new"})
    assert (
        open_settings(
            box, IndexerSettings, merged, context="indexer:01J"
        ).passkey.get_secret_value()
        == "new"
    )
