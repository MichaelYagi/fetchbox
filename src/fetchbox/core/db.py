"""Database engine, session factory and declarative base.

SQLite runs in WAL mode with foreign keys on; PostgreSQL is supported via asyncpg.
Schema changes go through Alembic only — never `create_all` outside tests.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import MetaData, String, event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from fetchbox.core.ids import new_id

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class IdMixin:
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_id)


def utcnow() -> datetime:
    return datetime.now(UTC)


def is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()


def make_engine(url: str, *, echo: bool = False) -> AsyncEngine:
    engine = create_async_engine(url, echo=echo, pool_pre_ping=not is_sqlite(url))
    if is_sqlite(url):
        sync_engine: Engine = engine.sync_engine
        event.listen(sync_engine, "connect", _sqlite_pragmas)
    return engine


class Database:
    def __init__(self, url: str) -> None:
        self.url = url
        self.engine = make_engine(url)
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessionmaker() as s:
            yield s

    async def dispose(self) -> None:
        await self.engine.dispose()
