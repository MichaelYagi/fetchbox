"""Encryption of secrets at rest (indexer passwords, cookies, passkeys, client credentials).

AES-256-GCM with a per-install key in ``<data>/secrets.key`` (mode 0600, outside the DB,
included in backups only password-wrapped). Ciphertexts look like ``enc:v1:<base64>``
where the payload is ``nonce(12) || ciphertext+tag``.

Each value is bound to its location with associated data (e.g. ``indexer:<id>:password``),
so a ciphertext copied into another row or field fails to decrypt instead of leaking.

Provider settings models mark secret fields with ``pydantic.SecretStr``.
``seal_settings`` encrypts exactly those fields for storage; ``open_settings`` reverses
it; ``mask_settings`` produces the API view, where secrets are never returned.
"""

from __future__ import annotations

import base64
import os
import secrets as pysecrets
import stat
import sys
from pathlib import Path
from typing import Any

import structlog
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel, SecretStr

log = structlog.get_logger(__name__)

KEY_FILENAME = "secrets.key"
PREFIX = "enc:v1:"
MASK = "********"
_NONCE = 12


class SecretsError(Exception):
    pass


class SecretBox:
    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise SecretsError("secrets key must be 32 bytes")
        self._aead = AESGCM(key)

    @classmethod
    def generate(cls) -> SecretBox:
        return cls(AESGCM.generate_key(bit_length=256))

    def encrypt(self, plaintext: str, *, aad: str = "") -> str:
        nonce = pysecrets.token_bytes(_NONCE)
        ct = self._aead.encrypt(nonce, plaintext.encode(), aad.encode())
        return PREFIX + base64.urlsafe_b64encode(nonce + ct).decode()

    def decrypt(self, token: str, *, aad: str = "") -> str:
        if not is_encrypted(token):
            raise SecretsError("value is not an encrypted secret")
        try:
            raw = base64.urlsafe_b64decode(token[len(PREFIX) :])
            return self._aead.decrypt(raw[:_NONCE], raw[_NONCE:], aad.encode()).decode()
        except (InvalidTag, ValueError) as exc:
            raise SecretsError("cannot decrypt secret (wrong key, field or corrupted)") from exc


def is_encrypted(value: object) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def _write_key_file(path: Path, key: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)  # created 0600 atomically; never world-readable
    try:
        os.write(fd, base64.b64encode(key) + b"\n")
    finally:
        os.close(fd)


def load_or_create(data_dir: Path) -> tuple[SecretBox, bool]:
    """Return (box, created). Warns when the key file is readable by others (POSIX)."""
    path = data_dir / KEY_FILENAME
    if not path.exists():
        key = AESGCM.generate_key(bit_length=256)
        try:
            _write_key_file(path, key)
        except FileExistsError:  # created concurrently; fall through and read it
            pass
        else:
            log.warning(
                "secrets_key_created",
                path=str(path),
                hint="back this file up; secrets cannot be recovered without it",
            )
            return SecretBox(key), True
    if sys.platform != "win32":
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            log.warning("secrets_key_permissions", path=str(path), mode=oct(mode), hint="chmod 600")
    try:
        key = base64.b64decode(path.read_bytes().strip(), validate=True)
    except ValueError as exc:
        raise SecretsError(f"{path} is not a valid secrets key file") from exc
    return SecretBox(key), False


# ---- settings models -------------------------------------------------------------------


def _secret_fields(model: type[BaseModel]) -> list[str]:
    out = []
    for name, field in model.model_fields.items():
        ann = field.annotation
        args = getattr(ann, "__args__", ())
        if ann is SecretStr or SecretStr in args:
            out.append(name)
    return out


def seal_settings(box: SecretBox, settings: BaseModel, *, context: str) -> dict[str, Any]:
    """Model -> JSON-ready dict with every SecretStr field encrypted (AAD = context:field)."""
    data = settings.model_dump(mode="python")
    for name in _secret_fields(type(settings)):
        value = data.get(name)
        if value is None:
            continue
        plain = value.get_secret_value() if isinstance(value, SecretStr) else str(value)
        data[name] = box.encrypt(plain, aad=f"{context}:{name}")
    return {k: (v.get_secret_value() if isinstance(v, SecretStr) else v) for k, v in data.items()}


def open_settings[M: BaseModel](
    box: SecretBox, model: type[M], stored: dict[str, Any], *, context: str
) -> M:
    data = dict(stored)
    for name in _secret_fields(model):
        if is_encrypted(data.get(name)):
            data[name] = box.decrypt(data[name], aad=f"{context}:{name}")
    return model.model_validate(data)


def mask_settings(model: type[BaseModel], stored: dict[str, Any]) -> dict[str, Any]:
    """API view: secret fields become MASK when set, None when unset."""
    out = dict(stored)
    for name in _secret_fields(model):
        out[name] = MASK if out.get(name) else None
    return out


def merge_update(
    model: type[BaseModel], current: dict[str, Any], incoming: dict[str, Any]
) -> dict[str, Any]:
    """Apply a client update where sending MASK back for a secret means "keep it"."""
    merged = {**current, **incoming}
    for name in _secret_fields(model):
        if incoming.get(name) == MASK:
            merged[name] = current.get(name)
    return merged


def strip_key(v: object) -> object:
    """Pydantic ``mode="before"`` validator body for API keys and tokens: drop the
    whitespace and line breaks a copy-paste brings along (keys never contain any).
    Not for passwords, which may."""
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v
