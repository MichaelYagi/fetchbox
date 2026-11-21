from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from fetchbox.core.errors import ProblemError
from fetchbox.core.errors import problem_responses as pr
from fetchbox.core.idempotency import IdempotencyKey, idempotent
from fetchbox.core.state import State
from fetchbox.modules.auth.deps import CurrentPrincipal, require
from fetchbox.modules.releases.decision import Decision
from fetchbox.modules.releases.grabs import GrabsService
from fetchbox.modules.releases.models import Grab
from fetchbox.modules.releases.service import ReleasesService

router = APIRouter(prefix="/releases", tags=["releases"])
_read = [Depends(require("releases:read"))]


def _svc(request: Request) -> ReleasesService:
    svc: ReleasesService = request.app.state.releases
    return svc


class ReleaseOut(Decision):
    release_id: str = Field(description="Refer to this release when grabbing it (1 hour)")


class IndexerFailureOut(BaseModel):
    indexer_id: str
    indexer_name: str
    error: str


class InteractiveOut(BaseModel):
    wanted_item_id: str
    indexers_searched: int
    releases: list[ReleaseOut] = Field(description="Best first; rejected ones last, with reasons")
    picks: list[str] = Field(description="release_ids an automatic search would grab")
    failures: list[IndexerFailureOut]


@router.get("", response_model=InteractiveOut, dependencies=_read, responses=pr(404, 422))
async def interactive_search(
    request: Request,
    p: CurrentPrincipal,
    wanted_item_id: str = Query(min_length=1),
) -> InteractiveOut:
    """Search every enabled indexer now and return every result, scored and ranked."""
    res = await _svc(request).interactive(wanted_item_id, actor=p.actor())
    by_decision = {
        id(d): rid for d, rid in zip(res.outcome.decisions, res.release_ids, strict=True)
    }
    releases = [
        ReleaseOut(**d.model_dump(), release_id=rid)
        for d, rid in zip(res.outcome.decisions, res.release_ids, strict=True)
    ]
    return InteractiveOut(
        wanted_item_id=res.wanted_item_id,
        indexers_searched=res.indexers,
        releases=releases,
        picks=[by_decision[id(d)] for d in res.outcome.picks],
        failures=[
            IndexerFailureOut(indexer_id=f.source_id, indexer_name=f.source_name, error=f.error)
            for f in res.outcome.failures
        ],
    )


# ---- grabs --------------------------------------------------------------------------------

grabs_router = APIRouter(tags=["releases"])
_write = [Depends(require("releases:write"))]


class GrabIn(BaseModel):
    release_id: str = Field(description="From GET /releases")
    wanted_item_ids: list[str] | None = Field(
        default=None, description="Defaults to the item the search was for"
    )
    reason: Literal["manual", "rss", "search", "upgrade"] = "manual"
    allow_rejected: bool = Field(default=False, description="Grab even though it was rejected")


class GrabOut(BaseModel):
    id: str
    release_title: str
    info_hash: str | None
    indexer_id: str | None
    reason: str
    score: int | None
    actor: str
    wanted_item_ids: list[str]
    release: dict[str, Any] = Field(description="Snapshot of the decision at grab time")
    created_at: datetime


def _grab_out(g: Grab, items: list[str]) -> GrabOut:
    return GrabOut(
        id=g.id,
        release_title=g.release_title,
        info_hash=g.info_hash,
        indexer_id=g.indexer_id,
        reason=g.reason,
        score=g.score,
        actor=g.actor,
        wanted_item_ids=items,
        release=g.release,
        created_at=g.created_at,
    )


def _grabs(request: Request) -> GrabsService:
    svc: GrabsService = request.app.state.grabs
    return svc


@grabs_router.post(
    "/grabs",
    response_model=GrabOut,
    status_code=201,
    dependencies=_write,
    responses={
        200: {"description": "Already grabbed: the existing grab"},
        **pr(404, 409, 410, 422),
    },
)
async def create_grab(
    body: GrabIn,
    request: Request,
    state: State,
    p: CurrentPrincipal,
    idempotency_key: IdempotencyKey = None,
) -> Response:
    """Grab a release from a recent interactive search. Moves the items to `grabbed`."""

    async def run() -> Response:
        grab, created = await _svc(request).grab(
            body.release_id,
            wanted_item_ids=body.wanted_item_ids,
            reason=body.reason,
            allow_rejected=body.allow_rejected,
            actor=p.actor(),
        )
        _, items = await _grabs(request).get(grab.id)
        out = _grab_out(grab, items).model_dump(mode="json")
        return JSONResponse(
            out,
            status_code=201 if created else 200,
            headers={"Location": f"/api/v1/grabs/{grab.id}"},
        )

    return await idempotent(state.db, request, actor_id=p.id, key=idempotency_key, run=run)


@grabs_router.get("/grabs", response_model=list[GrabOut], dependencies=_read)
async def list_grabs(request: Request, wanted_item_id: str = Query(min_length=1)) -> list[GrabOut]:
    """Grabs for one wanted item, newest first."""
    return [_grab_out(g, i) for g, i in await _grabs(request).for_wanted(wanted_item_id)]


@grabs_router.get("/grabs/{grab_id}", response_model=GrabOut, dependencies=_read, responses=pr(404))
async def get_grab(grab_id: str, request: Request) -> GrabOut:
    return _grab_out(*await _grabs(request).get(grab_id))


# ---- blocklist ----------------------------------------------------------------------------


class BlockIn(BaseModel):
    release_id: str | None = Field(default=None, description="Block a release from a search")
    title_id: str | None = None
    info_hash: str | None = Field(default=None, pattern="^[0-9a-fA-F]{40}$")
    guid: str | None = Field(default=None, max_length=2000)
    release_title: str | None = Field(default=None, max_length=1000)
    reason: str | None = Field(default=None, max_length=500)


class BlockOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title_id: str | None
    info_hash: str | None
    guid: str | None
    release_title: str | None
    indexer_id: str | None
    reason: str | None
    created_at: datetime


@grabs_router.get("/blocklist", response_model=list[BlockOut], dependencies=_read)
async def list_blocklist(request: Request, title_id: str | None = None) -> list[BlockOut]:
    return [BlockOut.model_validate(b) for b in await _grabs(request).blocklist(title_id)]


@grabs_router.post(
    "/blocklist",
    response_model=BlockOut,
    status_code=201,
    dependencies=_write,
    responses={200: {"description": "Already blocklisted"}, **pr(410, 422)},
)
async def add_blocklist(body: BlockIn, request: Request, p: CurrentPrincipal) -> Response:
    """Never grab this release again (for its title, or everywhere without a title)."""
    fields = body.model_dump(exclude={"release_id"})
    if body.release_id:
        hit = _svc(request).cached(body.release_id)
        if hit is None:
            raise ProblemError(410, "Gone", "release is unknown or expired", slug="release-expired")
        c = hit.decision.candidate
        wanted = await request.app.state.media.get_wanted(hit.wanted_item_id)
        fields.update(
            title_id=body.title_id or wanted.title_id,
            info_hash=body.info_hash or c.info_hash,
            guid=body.guid or c.guid,
            release_title=body.release_title or c.title,
        )
        indexer_id = c.source_id or None
    else:
        indexer_id = None
    entry, created = await _grabs(request).block(**fields, indexer_id=indexer_id, actor=p.actor())
    return JSONResponse(
        BlockOut.model_validate(entry).model_dump(mode="json"), status_code=201 if created else 200
    )


@grabs_router.delete(
    "/blocklist/{entry_id}", status_code=204, dependencies=_write, responses=pr(404)
)
async def delete_blocklist(entry_id: str, request: Request, p: CurrentPrincipal) -> Response:
    await _grabs(request).unblock(entry_id, actor=p.actor())
    return Response(status_code=204)
