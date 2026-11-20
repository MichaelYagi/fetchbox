from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from fetchbox.core.errors import ConflictError, NotFoundError, problem_responses
from fetchbox.core.jobs import Job
from fetchbox.core.pagination import Page, Sort, paginate
from fetchbox.core.state import State
from fetchbox.modules.auth.deps import require

router = APIRouter(prefix="/jobs", tags=["jobs"])
_read = [Depends(require("system:read"))]
_write = [Depends(require("system:write"))]


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    type: str
    queue: str
    status: str
    payload: dict[str, Any]
    result: dict[str, Any] | None
    error: str | None
    progress: float | None
    attempts: int
    max_attempts: int
    run_after: datetime
    cancel_requested: bool
    parent_id: str | None
    dedupe_key: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


def accepted(job: Job) -> JSONResponse:
    """Spec convention for long operations: 202 + Job body + Location of the job."""
    return JSONResponse(
        JobOut.model_validate(job).model_dump(mode="json"),
        status_code=202,
        headers={"Location": f"/api/v1/jobs/{job.id}"},
    )


class JobPage(Page[JobOut]):
    pass


JOB_SORT = Sort(
    {
        "id": Job.id,
        "created_at": Job.created_at,
        "run_after": Job.run_after,
        "finished_at": Job.finished_at,
    },
    id_col=Job.id,
    default="-id",
)


@router.get("", response_model=JobPage, dependencies=_read, responses=problem_responses(422))
async def list_jobs(
    state: State,
    status: str | None = Query(None, alias="filter[status]"),
    type: str | None = Query(None, alias="filter[type]"),
    parent_id: str | None = Query(None, alias="filter[parent_id]"),
    sort: str | None = Query(None, description=f"One of {', '.join(JOB_SORT.options)}"),
    limit: int = Query(50, ge=1, le=500),
    cursor: str | None = None,
) -> JobPage:
    """Newest first by default. `cursor` is the `next_cursor` of the previous page."""
    q = select(Job)
    if status:
        q = q.where(Job.status == status)
    if type:
        q = q.where(Job.type == type)
    if parent_id:
        q = q.where(Job.parent_id == parent_id)
    async with state.db.sessionmaker() as s:
        rows, nxt = await paginate(s, q, JOB_SORT, sort_param=sort, limit=limit, cursor=cursor)
    return JobPage(items=[JobOut.model_validate(r) for r in rows], next_cursor=nxt)


@router.get(
    "/{job_id}", response_model=JobOut, dependencies=_read, responses=problem_responses(404)
)
async def get_job(job_id: str, state: State) -> JobOut:
    job = await state.jobs.get(job_id)
    if job is None:
        raise NotFoundError("job", job_id)
    return JobOut.model_validate(job)


@router.post(
    "/{job_id}/cancel",
    response_model=JobOut,
    dependencies=_write,
    responses=problem_responses(404),
)
async def cancel_job(job_id: str, state: State) -> JobOut:
    job = await state.jobs.cancel(job_id)
    if job is None:
        raise NotFoundError("job", job_id)
    return JobOut.model_validate(job)


@router.post(
    "/{job_id}/retry",
    response_model=JobOut,
    dependencies=_write,
    responses=problem_responses(404, 409),
)
async def retry_job(job_id: str, state: State) -> JobOut:
    existing = await state.jobs.get(job_id)
    if existing is None:
        raise NotFoundError("job", job_id)
    if existing.status not in ("failed", "cancelled"):
        raise ConflictError(f"job is {existing.status}; only failed or cancelled jobs can retry")
    job = await state.jobs.retry(job_id)
    if job is None:
        raise ConflictError("an identical job (same dedupe key) is already active")
    return JobOut.model_validate(job)
