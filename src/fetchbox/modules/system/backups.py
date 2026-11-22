"""Backups and restore (spec: "Backups and restore").

A backup is a zip in ``<data>/backups``: a consistent snapshot of the database (SQLite's
online backup API, safe while Fetchbox runs), ``config.toml`` if there is one, and a
``manifest.json`` (app version, schema revision, when, why). The secrets key, which
decrypts stored passwords and API keys, is included only when a password is given, and
then encrypted with it (scrypt + AES-GCM): without it a backup can't leak secrets, and
restoring it on the same install still works because the key is already there.

Backups are files, not database rows, so they survive whatever happens to the database.
Made weekly (the last 8 kept), on demand, and automatically before database migrations.

Restoring is staged: the backup is unpacked into ``<data>/restore-pending`` and the
server stops; at the next start, before anything opens the database, the current files
are saved as a "before-restore" backup and replaced, then migrations bring an older
backup forward. A backup from a newer Fetchbox (an unknown schema revision) is refused.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import fetchbox
from fetchbox import migrations

BACKUP_DIR = "backups"
PENDING_DIR = "restore-pending"
MANIFEST = "manifest.json"
DB_NAME = "fetchbox.db"
KEY_NAME = "secrets.key"
KEY_WRAPPED = "secrets.key.enc"
CONFIG_NAME = "config.toml"
FORMAT = 1
KEEP = {"scheduled": 8, "pre-migration": 5, "before-restore": 5}
_ID = re.compile(r"^fetchbox-\d{8}-\d{6}-[a-z-]+(-\d+)?$")


class BackupError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass
class BackupInfo:
    id: str
    path: Path
    size: int
    created_at: datetime
    reason: str
    app_version: str
    schema_revision: str | None
    includes_secrets_key: bool


def sqlite_path(database_url: str) -> Path | None:
    """The database file of a SQLite URL (None for PostgreSQL and in-memory)."""
    m = re.match(r"^sqlite(?:\+\w+)?:///(.+)$", database_url)
    if not m or m.group(1) in (":memory:", ""):
        return None
    return Path(m.group(1))


def schema_revision(db_file: Path) -> str | None:
    if not db_file.is_file():
        return None
    con = sqlite3.connect(f"file:{db_file.as_posix()}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT version_num FROM alembic_version").fetchone()
        return str(row[0]) if row else None
    except sqlite3.DatabaseError:
        return None
    finally:
        con.close()


def known_revisions() -> set[str]:
    from alembic.script import ScriptDirectory

    script = ScriptDirectory(str(migrations.HERE))
    return {r.revision for r in script.walk_revisions()}


# ---- the secrets key, password-wrapped --------------------------------------------------------


def _derive(password: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password.encode())


def wrap_key(key: bytes, password: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    return b"FBK1" + salt + nonce + AESGCM(_derive(password, salt)).encrypt(nonce, key, b"fetchbox")


def unwrap_key(blob: bytes, password: str) -> bytes:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not blob.startswith(b"FBK1") or len(blob) < 4 + 16 + 12 + 16:
        raise BackupError("the backup's secrets key is damaged")
    salt, nonce, body = blob[4:20], blob[20:32], blob[32:]
    try:
        return AESGCM(_derive(password, salt)).decrypt(nonce, body, b"fetchbox")
    except InvalidTag:
        raise BackupError("wrong password for the backup's secrets key") from None


# ---- making and listing -----------------------------------------------------------------------


class Backups:
    def __init__(self, data_dir: Path, database_url: str) -> None:
        self.data_dir = data_dir
        self.database_url = database_url
        self.dir = data_dir / BACKUP_DIR

    @property
    def db_file(self) -> Path:
        path = sqlite_path(self.database_url)
        if path is None:
            raise BackupError(
                "backups are for the built-in SQLite database; back up PostgreSQL with pg_dump"
            )
        return path

    def create(self, reason: str = "manual", password: str | None = None) -> BackupInfo:
        """Blocking: run in a thread. Safe while Fetchbox runs."""
        self.dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC)
        stem = f"fetchbox-{now:%Y%m%d-%H%M%S}-{reason}"
        target, n = self.dir / f"{stem}.zip", 1
        while target.exists():
            n += 1
            target = self.dir / f"{stem}-{n}.zip"
        with tempfile.TemporaryDirectory(dir=self.dir) as tmp:
            snap = Path(tmp) / DB_NAME
            src = sqlite3.connect(self.db_file)
            dst = sqlite3.connect(snap)
            try:
                src.backup(dst)  # consistent even while other connections write
            finally:
                dst.close()
                src.close()
            manifest = {
                "format": FORMAT,
                "app_version": fetchbox.__version__,
                "schema_revision": schema_revision(snap),
                "created_at": now.isoformat(),
                "reason": reason,
                "includes_secrets_key": password is not None,
            }
            part = target.with_suffix(".zip.partial")
            with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as z:
                z.write(snap, DB_NAME)
                config = self.data_dir / CONFIG_NAME
                if config.is_file():
                    z.write(config, CONFIG_NAME)
                key = self.data_dir / KEY_NAME
                if password is not None:
                    if not key.is_file():
                        raise BackupError("there is no secrets key to include")
                    z.writestr(KEY_WRAPPED, wrap_key(key.read_bytes(), password))
                z.writestr(MANIFEST, json.dumps(manifest, indent=2))
            os.replace(part, target)
        self._prune(reason)
        return self.info(target)

    def _prune(self, reason: str) -> None:
        keep = KEEP.get(reason)
        if keep is None:
            return  # manual and uploaded backups are the user's to delete
        mine = [b for b in self.list() if b.reason == reason]
        for old in mine[keep:]:
            old.path.unlink(missing_ok=True)

    def info(self, path: Path) -> BackupInfo:
        try:
            with zipfile.ZipFile(path) as z:
                m = json.loads(z.read(MANIFEST))
        except (OSError, KeyError, ValueError, zipfile.BadZipFile):
            raise BackupError(f"{path.name} isn't a Fetchbox backup") from None
        return BackupInfo(
            id=path.stem,
            path=path,
            size=path.stat().st_size,
            created_at=datetime.fromisoformat(m["created_at"]),
            reason=str(m.get("reason", "manual")),
            app_version=str(m.get("app_version", "?")),
            schema_revision=m.get("schema_revision"),
            includes_secrets_key=bool(m.get("includes_secrets_key")),
        )

    def list(self) -> list[BackupInfo]:
        out = []
        for p in self.dir.glob("fetchbox-*.zip") if self.dir.is_dir() else []:
            try:
                out.append(self.info(p))
            except BackupError:
                continue
        return sorted(out, key=lambda b: b.created_at, reverse=True)

    def get(self, backup_id: str) -> BackupInfo:
        if not _ID.match(backup_id):
            raise BackupError(f"no backup {backup_id!r}")
        path = self.dir / f"{backup_id}.zip"
        if not path.is_file():
            raise BackupError(f"no backup {backup_id!r}")
        return self.info(path)

    def delete(self, backup_id: str) -> None:
        self.get(backup_id).path.unlink()

    def add_upload(self, data: bytes, filename: str) -> BackupInfo:
        """Keep an uploaded backup (checked first) alongside the others."""
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / f".upload-{secrets.token_hex(4)}.zip"
        tmp.write_bytes(data)
        try:
            info = self.info(tmp)
            self._check(tmp, info)
            stem = f"fetchbox-{info.created_at:%Y%m%d-%H%M%S}-uploaded"
            target, n = self.dir / f"{stem}.zip", 1
            while target.exists():
                n += 1
                target = self.dir / f"{stem}-{n}.zip"
            os.replace(tmp, target)
            return self.info(target)
        finally:
            tmp.unlink(missing_ok=True)

    # ---- restore ------------------------------------------------------------------------------

    def _check(self, path: Path, info: BackupInfo) -> None:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            if DB_NAME not in names:
                raise BackupError("the backup has no database")
            bad = [n for n in names if n.startswith(("/", "\\")) or ".." in Path(n).parts]
            if bad:
                raise BackupError("the backup contains unsafe paths")
        if info.schema_revision and info.schema_revision not in known_revisions():
            raise BackupError(
                f"this backup is from a newer Fetchbox ({info.app_version}); update Fetchbox "
                "first, then restore it"
            )

    def stage_restore(self, backup_id: str, password: str | None = None) -> BackupInfo:
        """Unpack a backup to be applied at the next start (see ``apply_pending``)."""
        info = self.get(backup_id)
        self._check(info.path, info)
        pending = self.data_dir / PENDING_DIR
        shutil.rmtree(pending, ignore_errors=True)
        pending.mkdir(parents=True)
        with zipfile.ZipFile(info.path) as z:
            (pending / DB_NAME).write_bytes(z.read(DB_NAME))
            if CONFIG_NAME in z.namelist():
                (pending / CONFIG_NAME).write_bytes(z.read(CONFIG_NAME))
            if KEY_WRAPPED in z.namelist():
                if password is None:
                    shutil.rmtree(pending, ignore_errors=True)
                    raise BackupError("this backup's secrets key needs its password")
                (pending / KEY_NAME).write_bytes(unwrap_key(z.read(KEY_WRAPPED), password))
            (pending / MANIFEST).write_bytes(z.read(MANIFEST))
        return info

    def apply_pending(self) -> dict[str, Any] | None:
        """At startup, before the database is opened: swap in a staged restore. The files
        it replaces are saved first as a before-restore backup. Returns the manifest."""
        pending = self.data_dir / PENDING_DIR
        if not (pending / DB_NAME).is_file():
            return None
        manifest: dict[str, Any] = json.loads((pending / MANIFEST).read_text())
        if self.db_file.is_file():
            self.create("before-restore")
        for wal in ("-wal", "-shm"):
            Path(f"{self.db_file}{wal}").unlink(missing_ok=True)
        shutil.copyfile(pending / DB_NAME, self.db_file)
        for name in (CONFIG_NAME, KEY_NAME):
            if (pending / name).is_file():
                shutil.copyfile(pending / name, self.data_dir / name)
        shutil.rmtree(pending, ignore_errors=True)
        return manifest

    def before_migrations(self) -> BackupInfo | None:
        """A pre-migration backup when the database is behind the code (not on a first
        start, and not when it's up to date)."""
        path = sqlite_path(self.database_url)
        if path is None or not path.is_file():
            return None
        current = schema_revision(path)
        if current is None or current == migrations.head_revision():
            return None
        return self.create("pre-migration")
