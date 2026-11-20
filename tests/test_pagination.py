from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from fetchbox import migrations
from fetchbox.core.db import Database, utcnow
from fetchbox.core.errors import ProblemError
from fetchbox.core.ids import new_id
from fetchbox.core.jobs import Job
from fetchbox.core.pagination import Sort, decode_cursor, encode_cursor, paginate

SORT = Sort({"id": Job.id, "run_after": Job.run_after}, id_col=Job.id, default="-id")


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'p.db').as_posix()}"
    await asyncio.to_thread(migrations.upgrade, url)
    d = Database(url)
    now = utcnow()
    async with d.sessionmaker() as s:
        # 7 jobs; run_after has duplicates to exercise the id tie-breaker
        for i in range(7):
            s.add(
                Job(
                    id=new_id(),
                    type="t",
                    queue="io",
                    status="pending",
                    payload={"i": i},
                    attempts=0,
                    max_attempts=1,
                    cancel_requested=False,
                    run_after=now + timedelta(minutes=i // 2),
                    created_at=now,
                )
            )
        await s.commit()
    yield d
    await d.dispose()


async def _all_pages(db: Database, sort: str | None, limit: int) -> list[int]:
    out: list[int] = []
    cursor = None
    async with db.sessionmaker() as s:
        while True:
            rows, cursor = await paginate(
                s, select(Job), SORT, sort_param=sort, limit=limit, cursor=cursor
            )
            out += [r.payload["i"] for r in rows]
            if cursor is None:
                return out


@pytest.mark.parametrize("limit", [1, 2, 3, 7, 50])
async def test_pages_cover_everything_once(db: Database, limit: int) -> None:
    assert await _all_pages(db, None, limit) == [6, 5, 4, 3, 2, 1, 0]
    assert await _all_pages(db, "id", limit) == [0, 1, 2, 3, 4, 5, 6]
    assert await _all_pages(db, "run_after", limit) == [0, 1, 2, 3, 4, 5, 6]
    assert await _all_pages(db, "-run_after", limit) == [6, 5, 4, 3, 2, 1, 0]


async def test_rejects_bad_sort_cursor_and_limit(db: Database) -> None:
    async with db.sessionmaker() as s:
        with pytest.raises(ProblemError) as e:
            await paginate(s, select(Job), SORT, sort_param="payload", limit=5, cursor=None)
        assert e.value.problem.errors and e.value.problem.errors[0].field == "sort"
        _, cur = await paginate(s, select(Job), SORT, sort_param="id", limit=2, cursor=None)
        assert cur is not None
        with pytest.raises(ProblemError) as e:  # cursor from another sort order
            await paginate(s, select(Job), SORT, sort_param="-id", limit=2, cursor=cur)
        assert "different sort order" in str(e.value.problem.errors)
        with pytest.raises(ProblemError):
            await paginate(s, select(Job), SORT, sort_param=None, limit=2, cursor="%%%")
        with pytest.raises(ProblemError):
            await paginate(s, select(Job), SORT, sort_param=None, limit=0, cursor=None)


def test_cursor_roundtrip_with_datetime() -> None:
    now = utcnow()
    cur = encode_cursor("-run_after", now, "01JABC")
    assert decode_cursor(cur, "-run_after") == (now, "01JABC")
    assert "=" not in cur and "/" not in cur
