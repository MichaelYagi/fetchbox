from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from fetchbox.core.errors import problem_responses as pr
from fetchbox.modules.auth.deps import require
from fetchbox.modules.discovery.service import DiscoveryService
from fetchbox.providers.metadata import MediaKind, MovieDetail, SeriesDetail, TitleSummary

router = APIRouter(prefix="/discovery", tags=["discovery"])
_read = [Depends(require("discovery:read"))]
_errors = pr(404, 502, 503)


def _svc(request: Request) -> DiscoveryService:
    svc: DiscoveryService = request.app.state.discovery
    return svc


@router.get("/search", response_model=list[TitleSummary], dependencies=_read, responses=_errors)
async def search(
    request: Request,
    q: str = Query(min_length=1, max_length=200),
    kind: MediaKind = "movie",
    year: int | None = Query(None, ge=1870, le=2200),
) -> list[TitleSummary]:
    """Search TMDB. 503 `metadata-not-configured` until a TMDB key is set."""
    return await _svc(request).search(q, kind, year)


@router.get("/movies/{tmdb_id}", response_model=MovieDetail, dependencies=_read, responses=_errors)
async def movie(tmdb_id: int, request: Request) -> MovieDetail:
    return await _svc(request).movie(tmdb_id)


@router.get("/series/{tmdb_id}", response_model=SeriesDetail, dependencies=_read, responses=_errors)
async def series(tmdb_id: int, request: Request) -> SeriesDetail:
    return await _svc(request).series(tmdb_id)
