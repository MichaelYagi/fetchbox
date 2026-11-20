from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from fetchbox.core.errors import problem_responses as pr
from fetchbox.modules.auth.deps import CurrentPrincipal, require
from fetchbox.modules.releases.decision import Decision
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
