from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field, computed_field
from sqlalchemy import select

from fetchbox.core.concurrency import check_if_match, etag, etag_of
from fetchbox.core.db import utcnow
from fetchbox.core.errors import ConflictError, FieldError, NotFoundError, ProblemError
from fetchbox.core.errors import problem_responses as pr
from fetchbox.core.idempotency import IdempotencyKey, idempotent
from fetchbox.core.schedules import Schedule, validate_interval
from fetchbox.core.state import State
from fetchbox.modules.auth.deps import CurrentPrincipal, require
from fetchbox.modules.automation.search import AutoSearch
from fetchbox.modules.system.jobs_router import JobOut, accepted

router = APIRouter(prefix="/automation", tags=["automation"])
_read = [Depends(require("automation:read"))]
_write = [Depends(require("automation:write"))]


class ScheduleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    job_type: str
    description: str
    interval_seconds: int
    default_interval_seconds: int
    enabled: bool
    payload: dict[str, Any]
    last_run_at: datetime | None
    next_run_at: datetime
    last_job_id: str | None
    version: int = Field(exclude=True)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def etag(self) -> str:
        return etag(self.version)


class SchedulePatch(BaseModel):
    enabled: bool | None = None
    interval_seconds: int | None = Field(default=None, description="30 s to 90 days")
    reset_interval: bool = Field(default=False, description="Restore the built-in default")


async def _get(state: State, name: str) -> Schedule:
    async with state.db.sessionmaker() as s:
        row = await s.get(Schedule, name)
    if row is None:
        raise NotFoundError("schedule", name)
    return row


@router.get("/schedules", response_model=list[ScheduleOut], dependencies=_read)
async def list_schedules(state: State) -> list[ScheduleOut]:
    async with state.db.sessionmaker() as s:
        rows = (await s.execute(select(Schedule).order_by(Schedule.name))).scalars()
        return [ScheduleOut.model_validate(r) for r in rows]


@router.get("/schedules/{name}", response_model=ScheduleOut, dependencies=_read, responses=pr(404))
async def get_schedule(name: str, state: State, response: Response) -> ScheduleOut:
    row = await _get(state, name)
    response.headers["ETag"] = etag_of(row)
    return ScheduleOut.model_validate(row)


@router.patch(
    "/schedules/{name}",
    response_model=ScheduleOut,
    dependencies=_write,
    responses=pr(404, 412, 422),
)
async def update_schedule(
    name: str,
    body: SchedulePatch,
    state: State,
    response: Response,
    if_match: str | None = Header(default=None),
) -> ScheduleOut:
    async with state.db.sessionmaker() as s:
        row = await s.get(Schedule, name)
        if row is None:
            raise NotFoundError("schedule", name)
        check_if_match(row, if_match)
        interval = row.default_interval_seconds if body.reset_interval else body.interval_seconds
        if interval is not None:
            try:
                validate_interval(interval)
            except ValueError as exc:
                raise ProblemError(
                    422,
                    "Validation Failed",
                    slug="validation",
                    errors=[FieldError(field="interval_seconds", message=str(exc))],
                ) from None
            row.interval_seconds = interval
        if body.enabled is not None:
            row.enabled = body.enabled
        if interval is not None or body.enabled:
            # Next run follows the (new) interval from the last run, never in the past.
            base = row.last_run_at or utcnow()
            row.next_run_at = max(base + timedelta(seconds=row.interval_seconds), utcnow())
        await s.commit()  # versioned UPDATE: a concurrent edit raises -> 412
        out = ScheduleOut.model_validate(row)
    state.scheduler.poke()
    response.headers["ETag"] = out.etag
    return out


@router.post(
    "/schedules/{name}/run",
    status_code=202,
    response_model=JobOut,
    dependencies=_write,
    responses=pr(404, 409),
)
async def run_schedule(
    name: str,
    request: Request,
    state: State,
    p: CurrentPrincipal,
    idempotency_key: IdempotencyKey = None,
) -> Response:
    """Run now, outside the regular cadence. 409 if this schedule's job is already active."""

    async def run() -> Response:
        try:
            job = await state.scheduler.run_now(name)
        except LookupError:
            raise NotFoundError("schedule", name) from None
        if job is None:
            raise ConflictError(f"schedule {name!r} already has a pending or running job")
        return accepted(job)

    return await idempotent(state.db, request, actor_id=p.id, key=idempotency_key, run=run)


# ---- search now ---------------------------------------------------------------------------

search_router = APIRouter(tags=["releases"])
_search = [Depends(require("releases:write"))]


def _auto(request: Request) -> AutoSearch:
    svc: AutoSearch = request.app.state.autosearch
    return svc


@search_router.post(
    "/wanted/{wanted_id}/search",
    status_code=202,
    response_model=JobOut,
    dependencies=_search,
    responses=pr(404, 409),
)
async def search_wanted(
    wanted_id: str,
    request: Request,
    state: State,
    p: CurrentPrincipal,
    idempotency_key: IdempotencyKey = None,
) -> Response:
    """Search every indexer now and grab the best acceptable release. Runs even when
    the item is unmonitored; for an acquired item only real upgrades are grabbed. 409
    while the item is downloading or a search for it is already queued."""

    async def run() -> Response:
        return accepted(await _auto(request).request(wanted_id))

    return await idempotent(state.db, request, actor_id=p.id, key=idempotency_key, run=run)


@search_router.post(
    "/titles/{title_id}/search",
    status_code=202,
    response_model=JobOut,
    dependencies=_search,
    responses=pr(404, 409),
)
async def search_title(
    title_id: str,
    request: Request,
    state: State,
    p: CurrentPrincipal,
    idempotency_key: IdempotencyKey = None,
) -> Response:
    """Movies: the same as searching the title's wanted item."""

    async def run() -> Response:
        svc = _auto(request)
        await svc.media.get_title(title_id)
        items = (await svc.media.wanted_for([title_id])).get(title_id, [])
        if not items:
            raise ConflictError(f"title {title_id} has no wanted items")
        return accepted(await svc.request(items[0].id))

    return await idempotent(state.db, request, actor_id=p.id, key=idempotency_key, run=run)
