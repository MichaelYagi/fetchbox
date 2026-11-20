"""Cursor (keyset) pagination and sorting for list endpoints.

``?limit=50&cursor=…&sort=-created_at``. The cursor is an opaque base64url token
holding the last row's sort value and id, so pages stay stable while rows are inserted
(unlike OFFSET). Sorting is by one allowed field, with the ULID primary key as the
tie-breaker in the same direction.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import Select, tuple_
from sqlalchemy.orm import InstrumentedAttribute

from fetchbox.core.errors import FieldError, ProblemError

DEFAULT_LIMIT = 50
MAX_LIMIT = 500


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None


def _bad(field: str, message: str) -> ProblemError:
    return ProblemError(
        422,
        "Validation Failed",
        slug="validation",
        errors=[FieldError(field=field, message=message)],
    )


def _enc(v: Any) -> Any:
    return {"dt": v.isoformat()} if isinstance(v, datetime) else v


def _dec(v: Any) -> Any:
    return datetime.fromisoformat(v["dt"]) if isinstance(v, dict) and "dt" in v else v


def encode_cursor(sort_key: str, value: Any, row_id: str) -> str:
    raw = json.dumps([sort_key, _enc(value), row_id], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str, sort_key: str) -> tuple[Any, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        key, value, row_id = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, TypeError) as exc:
        raise _bad("cursor", "malformed cursor") from exc
    if key != sort_key:
        raise _bad("cursor", "cursor was issued for a different sort order")
    return _dec(value), str(row_id)


class Sort:
    """Allowed sort fields for one endpoint, e.g. Sort({"created_at": Job.created_at}, ...)."""

    def __init__(
        self,
        fields: dict[str, InstrumentedAttribute[Any]],
        id_col: InstrumentedAttribute[str],
        default: str,
    ) -> None:
        self.fields = fields
        self.id_col = id_col
        self.default = default
        self.parse(default)

    @property
    def options(self) -> list[str]:
        return sorted({*self.fields, *(f"-{k}" for k in self.fields)})

    def parse(self, sort: str | None) -> tuple[str, bool]:
        sort = sort or self.default
        key, desc = (sort[1:], True) if sort.startswith("-") else (sort, False)
        if key not in self.fields:
            raise _bad("sort", f"must be one of {', '.join(self.options)}")
        return key, desc


async def paginate(
    session: Any,
    query: Select[Any],
    sort: Sort,
    *,
    sort_param: str | None,
    limit: int,
    cursor: str | None,
) -> tuple[list[Any], str | None]:
    """Run `query` (a select of one ORM entity) for one page. Returns (rows, next_cursor)."""
    if not 1 <= limit <= MAX_LIMIT:
        raise _bad("limit", f"must be between 1 and {MAX_LIMIT}")
    key, desc = sort.parse(sort_param)
    col = sort.fields[key]
    if cursor:
        value, row_id = decode_cursor(cursor, f"{'-' if desc else ''}{key}")
        if col is sort.id_col:
            query = query.where(col < row_id if desc else col > row_id)
        else:
            pair = tuple_(col, sort.id_col)
            query = query.where(pair < (value, row_id) if desc else pair > (value, row_id))
    order = [col.desc(), sort.id_col.desc()] if desc else [col.asc(), sort.id_col.asc()]
    if col is sort.id_col:
        order = order[:1]
    rows = list((await session.execute(query.order_by(*order).limit(limit + 1))).scalars())
    if len(rows) <= limit:
        return rows, None
    rows = rows[:limit]
    last = rows[-1]
    sort_key = f"{'-' if desc else ''}{key}"
    return rows, encode_cursor(sort_key, getattr(last, col.key), getattr(last, sort.id_col.key))
