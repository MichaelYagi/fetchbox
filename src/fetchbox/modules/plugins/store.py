"""``ctx.store``: a small key-value store per plugin, kept in Fetchbox's database."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import delete, select

from fetchbox.core.db import Database, utcnow
from fetchbox.modules.plugins.models import PluginValue

MAX_KEY = 200
MAX_VALUE = 64 * 1024  # bytes of JSON: a store for state, not for files (ctx.folder is)


class StoreError(ValueError):
    pass


def _encoded(key: str, value: Any) -> Any:
    if not key or len(key) > MAX_KEY:
        raise StoreError(f"a key is 1 to {MAX_KEY} characters")
    try:
        text = json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise StoreError(f"{key}: the value isn't JSON ({exc})") from None
    if len(text.encode()) > MAX_VALUE:
        raise StoreError(f"{key}: the value is over {MAX_VALUE // 1024} KB; use ctx.folder")
    return json.loads(text)  # what's stored is what get() gives back (tuples become lists)


class PluginStore:
    def __init__(self, db: Database, plugin: str) -> None:
        self.db = db
        self.plugin = plugin

    async def get(self, key: str, default: Any = None) -> Any:
        async with self.db.sessionmaker() as s:
            row = await s.scalar(
                select(PluginValue).where(PluginValue.plugin == self.plugin, PluginValue.key == key)
            )
            return default if row is None else row.value

    async def set(self, key: str, value: Any) -> None:
        value = _encoded(key, value)
        async with self.db.sessionmaker() as s:
            row = await s.scalar(
                select(PluginValue).where(PluginValue.plugin == self.plugin, PluginValue.key == key)
            )
            if row is None:
                s.add(PluginValue(plugin=self.plugin, key=key, value=value))
            else:
                row.value = value
                row.updated_at = utcnow()
            await s.commit()

    async def delete(self, key: str) -> bool:
        async with self.db.sessionmaker() as s:
            r = await s.execute(
                delete(PluginValue).where(PluginValue.plugin == self.plugin, PluginValue.key == key)
            )
            await s.commit()
            return bool(r.rowcount)  # type: ignore[attr-defined]

    async def keys(self, prefix: str = "") -> list[str]:
        async with self.db.sessionmaker() as s:
            q = select(PluginValue.key).where(PluginValue.plugin == self.plugin)
            if prefix:
                q = q.where(PluginValue.key.startswith(prefix, autoescape=True))
            return sorted((await s.execute(q)).scalars())

    async def clear(self) -> int:
        """Everything the plugin kept (when it's removed)."""
        async with self.db.sessionmaker() as s:
            r = await s.execute(delete(PluginValue).where(PluginValue.plugin == self.plugin))
            await s.commit()
            return int(r.rowcount)  # type: ignore[attr-defined]
