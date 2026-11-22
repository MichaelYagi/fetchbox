"""The FETCHBOX_DB_GUARD diagnostics: they must catch what holds SQLite's write lock."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from sqlalchemy import text

from fetchbox.core import db as dbmod
from fetchbox.core import watchdog
from fetchbox.core.db import Database


@pytest.fixture
async def guarded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    monkeypatch.setenv("FETCHBOX_DB_GUARD", "1")
    monkeypatch.delenv("FETCHBOX_DB_GUARD_OUT", raising=False)
    monkeypatch.setattr(dbmod, "GUARD_SLOW_S", 0.2)
    dbmod.guard_reports.clear()
    d = Database(f"sqlite+aiosqlite:///{tmp_path / 'g.db'}")
    async with d.engine.begin() as c:
        await c.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, n INTEGER)"))
        await c.execute(text("INSERT INTO t VALUES (1, 0), (2, 0)"))
    dbmod.guard_reports.clear()
    return d


async def test_busy_timeout_is_30s(guarded: Database) -> None:
    async with guarded.engine.connect() as c:
        assert (await c.execute(text("PRAGMA busy_timeout"))).scalar_one() == 30000


async def test_slow_and_nested_writes_are_reported(guarded: Database) -> None:
    async with guarded.sessionmaker() as s:
        await s.execute(text("UPDATE t SET n = 1 WHERE id = 1"))
        await asyncio.sleep(0.3)  # holding the write lock across an await
        await s.commit()
    assert any(r.startswith("SLOW WRITE") and "test_db_guard.py" in r
               for r in dbmod.guard_reports), dbmod.guard_reports  # fmt: skip

    dbmod.guard_reports.clear()
    async with guarded.sessionmaker() as outer:
        await outer.execute(text("UPDATE t SET n = 2 WHERE id = 1"))
        async with guarded.sessionmaker() as inner:
            await inner.execute(text("PRAGMA busy_timeout=300"))  # don't wait 30 s here
            # waits on its own task's lock: reported before it blocks
            with pytest.raises(Exception, match="locked"):
                await inner.execute(text("UPDATE t SET n = 2 WHERE id = 2"))
        await outer.rollback()
    await guarded.dispose()
    [r] = [r for r in dbmod.guard_reports if r.startswith("NESTED WRITE")]
    assert r.count("test_slow_and_nested_writes_are_reported") == 2, r
    assert not any("GUARD ERROR" in r for r in dbmod.guard_reports), dbmod.guard_reports


async def test_watchdog_reports_a_blocked_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    dbmod.guard_reports.clear()
    stop = watchdog.start(asyncio.get_running_loop(), threshold=0.2)
    await asyncio.sleep(0.1)
    time.sleep(0.5)  # noqa: ASYNC251 - the point: block the loop
    await asyncio.sleep(0.1)
    stop.set()
    [r] = [r for r in dbmod.guard_reports if r.startswith("LOOP BLOCKED")]
    assert "test_db_guard.py" in r and "test_watchdog_reports_a_blocked_loop" in r
