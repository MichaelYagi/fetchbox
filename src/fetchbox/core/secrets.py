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
import hashlib
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


# ---- where the key lives ----------------------------------------------------------------
# A file in the data folder, or (the Windows desktop app; FETCHBOX_KEYRING=1 elsewhere)
# the operating system's keyring, so a copy of the data folder alone can't open the
# secrets. A key *file* always wins and is moved into the keyring: that's how a restored
# backup's key, or a hand-placed one, takes over.

KEYRING_SERVICE = "Fetchbox secrets key"


def keyring_wanted() -> bool:
    """FETCHBOX_KEYRING=1/0 decides; by default the frozen Windows app only. (macOS asks
    again after every update of an unsigned app, so it's opt-in there for now.)"""
    env = os.environ.get("FETCHBOX_KEYRING", "").strip()
    if env in ("0", "1"):
        return env == "1"
    return sys.platform == "win32" and bool(getattr(sys, "frozen", False))


def _keyring() -> Any:
    import keyring  # type: ignore[import-not-found,unused-ignore]  # desktop extra

    return keyring


def _where() -> str:
    return {"win32": "Windows Credential Manager", "darwin": "the macOS Keychain"}.get(
        sys.platform, "the system keyring"
    )


def _account(data_dir: Path) -> str:
    """One key per data folder (two installs on one account don't share keys)."""
    return hashlib.sha256(str(data_dir.resolve()).encode()).hexdigest()[:24]


def _read_file(path: Path) -> bytes:
    if sys.platform != "win32":
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            log.warning("secrets_key_permissions", path=str(path), mode=oct(mode), hint="chmod 600")
    try:
        return base64.b64decode(path.read_bytes().strip(), validate=True)
    except ValueError as exc:
        raise SecretsError(f"{path} is not a valid secrets key file") from exc


def _store(kr: Any, account: str, key: bytes) -> bool:
    """Save to the keyring and read it back; False (logged) when that doesn't work."""
    text = base64.b64encode(key).decode()
    try:
        kr.set_password(KEYRING_SERVICE, account, text)
        ok = bool(kr.get_password(KEYRING_SERVICE, account) == text)
    except Exception as exc:  # any backend failure: keep the file instead
        log.warning("secrets_key_keyring_failed", where=_where(), error=repr(exc))
        return False
    return ok


def _load_keyring(kr: Any, data_dir: Path) -> tuple[SecretBox, bool]:
    path = data_dir / KEY_FILENAME
    account = _account(data_dir)
    if path.exists():
        key = _read_file(path)
        if _store(kr, account, key):
            path.unlink()
            log.info("secrets_key_moved", to=_where())
        return SecretBox(key), False
    try:
        stored = kr.get_password(KEYRING_SERVICE, account)
    except Exception as exc:
        # Never make a new key here: the existing secrets would become unreadable.
        raise SecretsError(
            f"couldn't read Fetchbox's secrets key from {_where()} ({exc}). Fix access to "
            f"it, or start with FETCHBOX_KEYRING=0 and put the key in {path}"
        ) from exc
    if stored:
        try:
            return SecretBox(base64.b64decode(stored, validate=True)), False
        except ValueError as exc:
            raise SecretsError(f"the secrets key in {_where()} is damaged") from exc
    key = AESGCM.generate_key(bit_length=256)
    if not _store(kr, account, key):
        _write_key_file(path, key)
    log.warning(
        "secrets_key_created",
        where=str(path) if path.exists() else _where(),
        hint="back up with the key included: secrets can't be recovered without it",
    )
    return SecretBox(key), True


def load_or_create(data_dir: Path) -> tuple[SecretBox, bool]:
    """Return (box, created). Warns when the key file is readable by others (POSIX)."""
    if keyring_wanted():
        try:
            kr = _keyring()
        except ImportError:
            log.warning("secrets_key_no_keyring", hint="install the desktop extra")
        else:
            return _load_keyring(kr, data_dir)
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
    return SecretBox(_read_file(path)), False


def key_file_bytes(data_dir: Path) -> bytes | None:
    """The key in key-file form, wherever it lives (backups "with key")."""
    path = data_dir / KEY_FILENAME
    if path.is_file():
        return path.read_bytes()
    if keyring_wanted():
        try:
            stored = _keyring().get_password(KEYRING_SERVICE, _account(data_dir))
        except Exception:
            return None
        if stored:
            return str(stored).encode() + b"\n"
    return None


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
