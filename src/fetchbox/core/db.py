"""Database engine, session factory and declarative base.

SQLite runs in WAL mode with foreign keys on; PostgreSQL is supported via asyncpg.
Schema changes go through Alembic only — never `create_all` outside tests.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, MetaData, String, event
from sqlalchemy.engine import Dialect, Engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

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


class UTCDateTime(TypeDecorator[datetime]):
    """Always-aware UTC datetimes.

    PostgreSQL stores ``timestamptz``. SQLite has no tz support, so values are stored as
    naive UTC ISO text, which also keeps lexical comparison in SQL correct.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime; use fetchbox.core.db.utcnow()")
        value = value.astimezone(UTC)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    # Writers queue for the lock instead of failing: 30 s rides out a stalled event loop
    # (a slow disk, a busy torrent engine) where 5 s gave "database is locked".
    cur.execute("PRAGMA busy_timeout=30000")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()


def make_engine(url: str, *, echo: bool = False) -> AsyncEngine:
    engine = create_async_engine(url, echo=echo, pool_pre_ping=not is_sqlite(url))
    if is_sqlite(url):
        sync_engine: Engine = engine.sync_engine
        event.listen(sync_engine, "connect", _sqlite_pragmas)
        if os.environ.get("FETCHBOX_DB_GUARD"):
            _guard(sync_engine)
    return engine


# ---- debug guard (FETCHBOX_DB_GUARD=1): find what holds SQLite's write lock -----------------

GUARD_SLOW_S = 1.0
guard_reports: list[str] = []


def _report(text: str) -> None:
    guard_reports.append(text)
    out = os.environ.get("FETCHBOX_DB_GUARD_OUT")
    if out:  # written as found, so a hung or killed run still leaves its findings
        with open(out, "a", encoding="utf-8") as f:
            f.write(text + "\n\n========\n\n")


def _app_stack() -> str:
    """Fetchbox (and test) frames of the async code that issued this statement (SQLAlchemy runs it
    in a greenlet whose own stack only shows SQLAlchemy). No source lookups: cheap."""
    import greenlet  # type: ignore[import-untyped]

    frame = getattr(greenlet.getcurrent().parent, "gr_frame", None) or sys._getframe()
    out = []
    while frame is not None:
        name = frame.f_code.co_filename.replace("\\", "/")
        ours = "src/fetchbox/" in name or "/tests/" in name
        if ours and not name.endswith("core/db.py"):
            short = name.split("src/fetchbox/")[-1].split("/tests/")[-1]
            out.append(f"  {short}:{frame.f_lineno} {frame.f_code.co_name}")
        frame = frame.f_back
    return "\n".join(reversed(out[:12]))


def _safe(fn: Any) -> Any:
    """A guard listener never breaks a statement: its own errors are only reported."""

    def wrapper(*a: Any, **kw: Any) -> None:
        try:
            fn(*a, **kw)
        except Exception as exc:
            _report(f"GUARD ERROR in {fn.__name__}: {type(exc).__name__}: {exc}")

    wrapper.__name__ = fn.__name__
    return wrapper


def _guard(engine: Engine) -> None:
    """Report write transactions held over GUARD_SLOW_S, and a task writing on a second
    connection while it already holds the write lock on another (it waits on itself
    until busy_timeout: "database is locked")."""
    writing: dict[int, tuple[float, str, object]] = {}  # connection -> (since, stack, task)

    def task() -> object:
        try:
            return asyncio.current_task()
        except RuntimeError:
            return None

    def is_write(statement: str) -> bool:
        return statement.lstrip()[:6].upper() not in ("SELECT", "PRAGMA")

    @event.listens_for(engine, "before_cursor_execute")
    @_safe
    def nested(conn: Any, cursor: Any, statement: str, *a: Any) -> None:
        """Checked before running: a nested write blocks, so it would never get further."""
        if not is_write(statement):
            return
        key, me = id(conn.connection.dbapi_connection), task()
        if key in writing or me is None:
            return
        for other, (_, stack, t) in list(writing.items()):
            if t is me and other != key:
                _report(
                    "NESTED WRITE: this task already holds a write transaction begun at\n"
                    + stack
                    + "\n...and now writes on another connection (it waits on itself) at\n"
                    + _app_stack()
                )

    @event.listens_for(engine, "after_cursor_execute")
    @_safe
    def began(conn: Any, cursor: Any, statement: str, *a: Any) -> None:
        """The first write of a transaction holds the lock from here (not while it waited)."""
        if not is_write(statement):
            return
        key = id(conn.connection.dbapi_connection)
        if key not in writing:
            writing[key] = (time.monotonic(), _app_stack(), task())

    @_safe
    def end(conn: Any) -> None:
        if conn.invalidated:  # cancelled mid-statement: the pool's reset clears it
            return
        key = id(conn.connection.dbapi_connection)
        held = writing.pop(key, None)
        if held and time.monotonic() - held[0] > GUARD_SLOW_S:
            _report(
                f"SLOW WRITE: held the write lock {time.monotonic() - held[0]:.1f}s, begun at\n"
                + held[1]
            )

    event.listen(engine, "commit", end)
    event.listen(engine, "rollback", end)

    @event.listens_for(engine.pool, "reset")
    @_safe
    def reset(dbapi_conn: Any, record: Any, reset_state: Any) -> None:
        held = writing.pop(id(dbapi_conn), None)
        if held:  # the session closed without commit: its writes were rolled back
            _report(
                f"ROLLED BACK ON RETURN after {time.monotonic() - held[0]:.1f}s, "
                f"begun at\n{held[1]}"
            )


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
