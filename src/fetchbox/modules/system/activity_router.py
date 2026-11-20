from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from fetchbox.core.pagination import Page, Sort, paginate
from fetchbox.core.state import State
from fetchbox.modules.auth.deps import require
from fetchbox.modules.system.activity import ActivityEntry

router = APIRouter(tags=["activity"])


class ActivityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    category: str
    action: str
    actor: str | None
    subject_type: str | None
    subject_id: str | None
    outcome: str
    message: str
    data: dict[str, Any]
    created_at: datetime


class ActivityPage(Page[ActivityOut]):
    pass


ACTIVITY_SORT = Sort({"id": ActivityEntry.id}, id_col=ActivityEntry.id, default="-id")


@router.get(
    "/activity", response_model=ActivityPage, dependencies=[Depends(require("system:read"))]
)
async def list_activity(
    state: State,
    category: str | None = Query(None, alias="filter[category]"),
    outcome: str | None = Query(None, alias="filter[outcome]"),
    actor: str | None = Query(None, alias="filter[actor]"),
    subject_id: str | None = Query(None, alias="filter[subject_id]"),
    limit: int = Query(50, ge=1, le=500),
    cursor: str | None = None,
) -> ActivityPage:
    """Newest first."""
    q = select(ActivityEntry)
    for col, val in (
        (ActivityEntry.category, category),
        (ActivityEntry.outcome, outcome),
        (ActivityEntry.actor, actor),
        (ActivityEntry.subject_id, subject_id),
    ):
        if val:
            q = q.where(col == val)
    async with state.db.sessionmaker() as s:
        rows, nxt = await paginate(s, q, ACTIVITY_SORT, sort_param=None, limit=limit, cursor=cursor)
    return ActivityPage(items=[ActivityOut.model_validate(r) for r in rows], next_cursor=nxt)
