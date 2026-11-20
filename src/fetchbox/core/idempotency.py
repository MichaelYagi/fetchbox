"""Idempotency-Key support for POSTs that create things (grabs, imports, searches, jobs).

``(actor, key) -> request hash + response`` is kept for 24 hours:

- same key, same request  -> the original response is replayed (``Idempotent-Replayed``)
- same key, other request -> 422
- same key while the first is still running -> 409 with Retry-After
- the first attempt raised an error -> nothing is stored; the client may retry

Keys are scoped per actor (API key or user), not per API key only, so cookie-session
requests from the UI are covered too (spec fix 2).
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import Header, Request
from fastapi.responses import Response
from sqlalchemy import JSON, Integer, String, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from fetchbox.core.db import Base, Database, UTCDateTime, utcnow
from fetchbox.core.errors import ProblemError

TTL = timedelta(hours=24)
HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"
_KEEP_HEADERS = ("content-type", "location", "etag")

IdempotencyKey = Annotated[
    str | None,
    Header(
        alias=HEADER,
        min_length=1,
        max_length=255,
        description="Retry-safe key: repeats with the same body return the first response",
    ),
]


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_record"

    actor_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None = in progress
    response: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


async def request_hash(request: Request) -> str:
    h = hashlib.sha256()
    h.update(request.method.encode())
    h.update(b"\n" + request.url.path.encode())
    h.update(
        b"\n" + "&".join(sorted(f"{k}={v}" for k, v in request.query_params.multi_items())).encode()
    )
    h.update(b"\n" + await request.body())
    return h.hexdigest()


def _replay(rec: IdempotencyRecord) -> Response:
    data = rec.response or {}
    headers = dict(data.get("headers", {}))
    headers[REPLAYED_HEADER] = "true"
    return Response(
        content=data.get("body", "").encode(), status_code=rec.status or 200, headers=headers
    )


async def idempotent(
    db: Database,
    request: Request,
    *,
    actor_id: str,
    key: str | None,
    run: Callable[[], Awaitable[Response]],
    _attempt: int = 0,
) -> Response:
    if key is None:
        return await run()
    if _attempt > 3:
        raise ProblemError(
            409,
            "Conflict",
            "could not claim the idempotency key; retry",
            slug="idempotency-in-progress",
            headers={"Retry-After": "1"},
        )
    digest = await request_hash(request)
    now = utcnow()

    async with db.sessionmaker() as s:
        s.add(
            IdempotencyRecord(
                actor_id=actor_id, key=key, request_hash=digest, status=None, created_at=now
            )
        )
        try:
            await s.commit()
            claimed = True
        except IntegrityError:
            await s.rollback()
            claimed = False

    if not claimed:
        async with db.sessionmaker() as s:
            rec = await s.get(IdempotencyRecord, (actor_id, key))
            if rec is not None and rec.created_at < now - TTL:
                await s.delete(rec)  # expired: treat the key as fresh
                await s.commit()
                return await idempotent(
                    db, request, actor_id=actor_id, key=key, run=run, _attempt=_attempt + 1
                )
        if rec is None:  # deleted between our insert and read (first attempt failed)
            return await idempotent(
                db, request, actor_id=actor_id, key=key, run=run, _attempt=_attempt + 1
            )
        if rec.request_hash != digest:
            raise ProblemError(
                422,
                "Idempotency Key Reused",
                f"{HEADER} was already used for a different request",
                slug="idempotency-mismatch",
            )
        if rec.status is None:
            raise ProblemError(
                409,
                "Conflict",
                "a request with this key is still in progress",
                slug="idempotency-in-progress",
                headers={"Retry-After": "1"},
            )
        return _replay(rec)

    try:
        response = await run()
    except BaseException:
        async with db.sessionmaker() as s:
            await s.execute(
                delete(IdempotencyRecord).where(
                    IdempotencyRecord.actor_id == actor_id, IdempotencyRecord.key == key
                )
            )
            await s.commit()
        raise

    body = bytes(response.body).decode() if hasattr(response, "body") else ""
    headers = {k: v for k, v in response.headers.items() if k.lower() in _KEEP_HEADERS}
    async with db.sessionmaker() as s:
        rec = await s.get(IdempotencyRecord, (actor_id, key))
        if rec is not None:
            rec.status = response.status_code
            rec.response = {"body": body, "headers": headers}
            await s.commit()
    return response


async def prune(db: Database, older_than: timedelta = TTL) -> int:
    async with db.sessionmaker() as s:
        res = await s.execute(
            delete(IdempotencyRecord).where(IdempotencyRecord.created_at < utcnow() - older_than)
        )
        await s.commit()
    return int(res.rowcount or 0)  # type: ignore[attr-defined]
