"""Config export / import (spec: "Config export / import").

Export is one JSON document with all configuration, never media data: quality profiles,
custom formats, naming, indexer definitions, indexers, download clients, storage nodes,
library folders, media servers, notifications, schedules and system settings. Runtime
fields (status, failure counts, last errors) are left out. Secrets are unsealed with
this install's key and only exported when asked for, then as one password-encrypted
block (scrypt + AES-GCM), never in the readable part.

Import compares the document with this install and returns a plan (a dry run by
default); applying writes everything in one transaction. A row matches by id, else by
its unique name (slug for definitions): a fresh install's own defaults ("Movies",
"Built-in", ...) are updated rather than duplicated, and references between tables are
remapped to the local ids. Secrets are sealed again with this install's key; a secret
the document doesn't carry keeps its current value. Nothing is deleted.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Table, insert, select, update

import fetchbox
from fetchbox.core.db import Base, Database
from fetchbox.core.providers import ProviderRegistry
from fetchbox.core.secrets import (
    SecretBox,
    _secret_fields,
    is_encrypted,
    open_settings,
    seal_settings,
)
from fetchbox.modules.system.backups import BackupError, unwrap_key, wrap_key
from fetchbox.modules.system.config import (
    ROW_ID,
    SECRETS,
    ConfigService,
    SystemConfig,
    _deep_merge,
)
from fetchbox.modules.system.models import ConfigRow

FORMAT = "fetchbox-config"
VERSION = 1

# table -> (natural key, provider category for sealed settings, {column: referenced table})
TABLES: dict[str, tuple[str, str | None, dict[str, str]]] = {
    "naming_template": ("name", None, {}),
    "custom_format": ("name", None, {}),
    "quality_profile": ("name", None, {}),
    "indexer_definition": ("slug", None, {}),
    "indexer": ("name", "indexer", {"definition_id": "indexer_definition"}),
    "download_client": ("name", "download_client", {}),
    "storage_node": ("name", None, {}),
    "destination": (
        "name",
        None,
        {"node_id": "storage_node", "naming_template_id": "naming_template"},
    ),
    "media_server": ("name", "media_server", {}),
    "notification_target": ("name", "notifier", {}),
}
RUNTIME = {
    "status", "status_message", "checked_at", "mount_point", "failure_count",
    "disabled_until", "last_error", "last_sent_at", "last_refresh_at", "created_at",
    "updated_at",
}  # fmt: skip
VERSIONED = set(TABLES) - {"indexer_definition"}  # its `version` is the definition's own


class ImportError_(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _table(name: str) -> Table:
    t = Base.metadata.tables[name]
    assert isinstance(t, Table)
    return t


def _json(v: Any) -> Any:
    return v.isoformat() if isinstance(v, datetime) else v


class Portable:
    def __init__(
        self, db: Database, box: SecretBox, providers: ProviderRegistry, config: ConfigService
    ) -> None:
        self.db = db
        self.box = box
        self.providers = providers
        self.config = config

    def _secret_names(self, table: str, row: dict[str, Any]) -> tuple[type[Any] | None, list[str]]:
        category = TABLES[table][1]
        if category is None:
            return None, []
        try:
            cls = self.providers.get(category, row["kind"]).Settings  # type: ignore[arg-type]
        except LookupError:
            return None, []
        return cls, _secret_fields(cls)

    # ---- export -------------------------------------------------------------------------------

    async def export(self, password: str | None = None) -> dict[str, Any]:
        tables: dict[str, list[dict[str, Any]]] = {}
        secrets: dict[str, str] = {}
        async with self.db.sessionmaker() as s:
            for name in TABLES:
                t = _table(name)
                rows = []
                for r in (await s.execute(select(t).order_by(t.c.id))).mappings():
                    row = {k: _json(v) for k, v in r.items() if k not in RUNTIME}
                    if name in VERSIONED:
                        row.pop("version", None)
                    cls, names = self._secret_names(name, row)
                    if cls is not None and names:
                        ctx = f"{name}:{row['id']}"  # sealed under the table's name
                        plain = open_settings(self.box, cls, row["settings"], context=ctx)
                        settings = dict(row["settings"])
                        for f in names:
                            v = getattr(plain, f, None)
                            if v is not None and password is not None:
                                secrets[f"{name}/{row['id']}/{f}"] = v.get_secret_value()
                            settings[f] = None
                        row["settings"] = settings
                    rows.append(row)
                tables[name] = rows
            sched = _table("schedule")
            schedules = {
                r["name"]: {"interval_seconds": r["interval_seconds"], "enabled": r["enabled"]}
                for r in (await s.execute(select(sched))).mappings()
            }
        system = self.config.current.model_dump(mode="json")
        for section, field in SECRETS:
            v = getattr(getattr(self.config.current, section), field)
            if v is not None and password is not None:
                secrets[f"system/{section}.{field}"] = v.get_secret_value()
            system[section][field] = None
        doc: dict[str, Any] = {
            "format": FORMAT,
            "version": VERSION,
            "app_version": fetchbox.__version__,
            "exported_at": datetime.now(UTC).isoformat(),
            "tables": tables,
            "schedules": schedules,
            "system": system,
            "secrets": None,
        }
        if password is not None:
            blob = wrap_key(json.dumps(secrets).encode(), password)
            doc["secrets"] = base64.b64encode(blob).decode()
        return doc

    # ---- import -------------------------------------------------------------------------------

    def _secrets(self, doc: dict[str, Any], password: str | None) -> dict[str, str]:
        if not doc.get("secrets"):
            return {}
        if password is None:
            raise ImportError_("this export holds secrets: give its password")
        try:
            data: dict[str, str] = json.loads(
                unwrap_key(base64.b64decode(doc["secrets"]), password)
            )
        except BackupError as exc:
            msg = exc.message.replace("backup's secrets key", "export's secrets")
            raise ImportError_(msg) from None
        return data

    async def import_(
        self, doc: dict[str, Any], *, password: str | None = None, apply: bool = False
    ) -> dict[str, Any]:
        if doc.get("format") != FORMAT or doc.get("version") != VERSION:
            raise ImportError_("not a Fetchbox config export (or from an unsupported version)")
        secrets = self._secrets(doc, password)
        plan: dict[str, dict[str, list[str]]] = {}
        id_map: dict[str, dict[str, str]] = {name: {} for name in TABLES}
        async with self.db.sessionmaker() as s, s.begin():
            for name, (natural, category, refs) in TABLES.items():
                t = _table(name)
                current = {r["id"]: dict(r) for r in (await s.execute(select(t))).mappings()}
                by_key = {r[natural]: r for r in current.values()}
                created, updated, same = [], [], []
                for incoming in doc.get("tables", {}).get(name, []):
                    row = {k: v for k, v in incoming.items() if k in t.c and k not in RUNTIME}
                    for col, target in refs.items():
                        if row.get(col) is not None:
                            row[col] = id_map[target].get(row[col], row[col])
                    local = current.get(row["id"]) or by_key.get(row[natural])
                    local_id = local["id"] if local else row["id"]
                    id_map[name][row["id"]] = local_id
                    row["id"] = local_id
                    if "settings" in row and category is not None:
                        row["settings"] = self._seal(name, row, local, secrets.get, incoming["id"])
                    label = str(row.get(natural))
                    if local is None:
                        created.append(label)
                        if apply:
                            values = {k: _parse(t, k, v) for k, v in row.items()}
                            if name in VERSIONED:
                                values["version"] = 1
                            if "created_at" in t.c:
                                values["created_at"] = datetime.now(UTC)
                            await s.execute(insert(t).values(**values))
                        continue
                    changes = {k: v for k, v in row.items()
                               if k != "id" and _differs(local.get(k), v, k)}  # fmt: skip
                    if not changes:
                        same.append(label)
                        continue
                    updated.append(label)
                    if apply:
                        values = {k: _parse(t, k, v) for k, v in changes.items()}
                        if name in VERSIONED:
                            values["version"] = int(local.get("version") or 1) + 1
                        await s.execute(update(t).where(t.c.id == local_id).values(**values))
                plan[name] = {"create": created, "update": updated, "unchanged": same}
            sched = _table("schedule")
            existing = {r["name"]: dict(r) for r in (await s.execute(select(sched))).mappings()}
            sched_changes = []
            for sname, want in (doc.get("schedules") or {}).items():
                cur = existing.get(sname)
                if cur is None:
                    continue  # a schedule this version doesn't have
                diff = {k: want[k] for k in ("interval_seconds", "enabled")
                        if k in want and cur[k] != want[k]}  # fmt: skip
                if diff:
                    sched_changes.append(sname)
                    if apply:
                        await s.execute(update(sched).where(sched.c.name == sname).values(**diff))
            plan["schedule"] = {"create": [], "update": sched_changes, "unchanged": []}
            sys_changes = self._system(doc, secrets)
            plan["system"] = {"create": [], "update": sorted(sys_changes), "unchanged": []}
            if apply and sys_changes:
                merged = _deep_merge(self.config._open(self.config._stored), sys_changes)
                stored = self.config._seal(SystemConfig.model_validate(merged))
                cfg_row = await s.get(ConfigRow, ROW_ID)
                if cfg_row is None:
                    s.add(ConfigRow(id=ROW_ID, data=stored))
                else:
                    cfg_row.data = stored
            if not apply:
                await s.rollback()
        return {"applied": apply, "plan": plan}

    def _seal(
        self, table: str, row: dict[str, Any], local: dict[str, Any] | None,
        secret: Any, source_id: str,
    ) -> dict[str, Any]:  # fmt: skip
        """Settings sealed with this install's key: secrets from the document, else the
        local row's current ones."""
        cls, names = self._secret_names(table, row)
        if cls is None:
            return dict(row["settings"])
        context = f"{table}:{row['id']}"
        plain = dict(row["settings"])
        current = (
            open_settings(self.box, cls, local["settings"], context=context) if local else None
        )
        for f in names:
            v = secret(f"{table}/{source_id}/{f}")
            if v is None and current is not None:
                cv = getattr(current, f, None)
                v = cv.get_secret_value() if cv is not None else None
            plain[f] = v
        try:
            model = cls.model_validate(plain)
        except Exception:
            # incomplete without its secret (export without secrets, new row): kept as is,
            # the user adds the secret in Settings
            return {**row["settings"], **{f: None for f in names}}
        sealed: dict[str, Any] = seal_settings(self.box, model, context=context)
        return sealed

    def _system(self, doc: dict[str, Any], secrets: dict[str, str]) -> dict[str, dict[str, Any]]:
        """Changed system settings (secrets only when the document carries them)."""
        incoming = doc.get("system") or {}
        current = self.config.current.model_dump(mode="json")
        out: dict[str, dict[str, Any]] = {}
        secret_fields = {(sec, f) for sec, f in SECRETS}
        for section, values in incoming.items():
            if section not in current or not isinstance(values, dict):
                continue
            for field, v in values.items():
                if (section, field) in secret_fields:
                    v = secrets.get(f"system/{section}.{field}")
                    if v is None:
                        continue
                    cur = getattr(getattr(self.config.current, section), field)
                    if cur is not None and cur.get_secret_value() == v:
                        continue
                elif current[section].get(field) == v:
                    continue
                out.setdefault(section, {})[field] = v
        return out


def _differs(a: Any, b: Any, key: str) -> bool:
    if isinstance(a, datetime):
        a = a.isoformat()
    if key == "settings" and isinstance(a, dict) and isinstance(b, dict):
        # sealed values differ every time (fresh nonce): compare the readable parts only
        return {k: v for k, v in a.items() if not is_encrypted(v)} != {
            k: v for k, v in b.items() if not is_encrypted(v)
        }
    return bool(a != b)


def _parse(t: Table, key: str, v: Any) -> Any:
    col = t.c[key]
    if isinstance(v, str) and col.type.python_type is datetime:
        return datetime.fromisoformat(v)
    return v
