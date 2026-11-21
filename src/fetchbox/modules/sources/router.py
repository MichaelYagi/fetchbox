from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, Field, computed_field

from fetchbox.core.concurrency import etag
from fetchbox.core.errors import problem_responses as pr
from fetchbox.core.providers import TestResult
from fetchbox.modules.auth.deps import CurrentPrincipal, require
from fetchbox.modules.sources.models import Indexer, IndexerDefinition
from fetchbox.modules.sources.service import SourcesService
from fetchbox.providers.indexers.definition import Definition

router = APIRouter(prefix="/indexers", tags=["sources"])
_read = [Depends(require("sources:read"))]
_write = [Depends(require("sources:write"))]
IfMatch = Annotated[str | None, Header(description="ETag from a previous read; 412 if stale")]


def _svc(request: Request) -> SourcesService:
    svc: SourcesService = request.app.state.sources
    return svc


class IndexerOut(BaseModel):
    id: str
    kind: str
    name: str
    enabled: bool
    priority: int
    settings: dict[str, Any] = Field(description="Secrets read back as ********")
    tags: list[str]
    failure_count: int
    disabled_until: datetime | None = Field(description="Skipped by searches until then")
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    version: int = Field(exclude=True)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def etag(self) -> str:
        return etag(self.version)


def _out(svc: SourcesService, row: Indexer, response: Response | None = None) -> IndexerOut:
    out = IndexerOut(
        id=row.id,
        kind=row.kind,
        name=row.name,
        enabled=row.enabled,
        priority=row.priority,
        settings=svc.masked(row),
        tags=row.tags or [],
        failure_count=row.failure_count,
        disabled_until=row.disabled_until,
        last_error=row.last_error,
        created_at=row.created_at,
        updated_at=row.updated_at,
        version=row.version,
    )
    if response is not None:
        response.headers["ETag"] = out.etag
    return out


class IndexerIn(BaseModel):
    kind: str = Field(default="torznab", min_length=1, max_length=50)
    name: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    priority: int = Field(default=25, ge=1, le=50, description="Lower wins ties")
    settings: dict[str, Any]
    tags: list[str] = Field(default=[], max_length=50)


class IndexerPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=1, le=50)
    settings: dict[str, Any] | None = Field(
        default=None, description="Merged into the current settings; send ******** to keep one"
    )
    tags: list[str] | None = Field(default=None, max_length=50)


class KindOut(BaseModel):
    kind: str
    capabilities: list[str]
    settings_schema: dict[str, Any] = Field(description="JSON Schema for the settings form")


class ReorderIn(BaseModel):
    ids: list[str] = Field(min_length=1, description="Indexer ids, best first")


@router.get("/kinds", response_model=list[KindOut], dependencies=_read)
async def list_kinds(request: Request) -> list[KindOut]:
    """Indexer kinds this install can add, with the settings each one takes."""
    return [
        KindOut(
            kind=c.kind,
            capabilities=sorted(c.capabilities),
            settings_schema=c.Settings.model_json_schema(),
        )
        for c in _svc(request).kinds()
    ]


@router.get("", response_model=list[IndexerOut], dependencies=_read)
async def list_indexers(request: Request) -> list[IndexerOut]:
    """Best priority first."""
    svc = _svc(request)
    return [_out(svc, r) for r in await svc.all()]


@router.post(
    "", response_model=IndexerOut, status_code=201, dependencies=_write, responses=pr(409, 422)
)
async def add_indexer(
    body: IndexerIn, request: Request, response: Response, p: CurrentPrincipal
) -> IndexerOut:
    svc = _svc(request)
    row = await svc.create(
        kind=body.kind,
        name=body.name,
        settings=body.settings,
        enabled=body.enabled,
        priority=body.priority,
        tags=body.tags,
        actor=p.actor(),
    )
    response.headers["Location"] = f"{request.url.path}/{row.id}"
    return _out(svc, row, response)


@router.post("/reorder", response_model=list[IndexerOut], dependencies=_write, responses=pr(422))
async def reorder(body: ReorderIn, request: Request, p: CurrentPrincipal) -> list[IndexerOut]:
    """Set priorities 1..n in the order given."""
    svc = _svc(request)
    return [_out(svc, r) for r in await svc.reorder(body.ids, actor=p.actor())]


@router.get("/{indexer_id}", response_model=IndexerOut, dependencies=_read, responses=pr(404))
async def get_indexer(indexer_id: str, request: Request, response: Response) -> IndexerOut:
    svc = _svc(request)
    return _out(svc, await svc.get(indexer_id), response)


@router.patch(
    "/{indexer_id}",
    response_model=IndexerOut,
    dependencies=_write,
    responses=pr(404, 409, 412, 422),
)
async def update_indexer(
    indexer_id: str,
    body: IndexerPatch,
    request: Request,
    response: Response,
    p: CurrentPrincipal,
    if_match: IfMatch = None,
) -> IndexerOut:
    """Enabling it again, or changing settings, clears any failure backoff."""
    svc = _svc(request)
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    row = await svc.update(indexer_id, changes, if_match=if_match, actor=p.actor())
    return _out(svc, row, response)


@router.delete("/{indexer_id}", status_code=204, dependencies=_write, responses=pr(404, 412))
async def delete_indexer(
    indexer_id: str, request: Request, p: CurrentPrincipal, if_match: IfMatch = None
) -> Response:
    await _svc(request).delete(indexer_id, if_match=if_match, actor=p.actor())
    return Response(status_code=204)


@router.post(
    "/{indexer_id}/test", response_model=TestResult, dependencies=_write, responses=pr(404)
)
async def test_indexer(indexer_id: str, request: Request) -> TestResult:
    """Ask the indexer for its capabilities and one result. Clears backoff on success."""
    return await _svc(request).test(indexer_id)


# ---- definitions ------------------------------------------------------------------------------

definitions_router = APIRouter(prefix="/indexer-definitions", tags=["sources"])


class DefinitionOut(BaseModel):
    slug: str
    name: str
    version: str
    source: str = Field(description="builtin or user")
    description: str
    language: str
    links: list[str] = Field(description="The only hosts it may contact")
    capabilities: list[str]
    settings: list[dict[str, Any]] = Field(description="Settings the user fills in")
    options: list[dict[str, Any]]
    yaml: str | None = Field(default=None, description="Only on single reads")


class DefinitionIn(BaseModel):
    yaml: str = Field(min_length=1, description="The definition, in Fetchbox's YAML format")


def _def_out(row: IndexerDefinition, d: Definition, *, full: bool = False) -> DefinitionOut:
    return DefinitionOut(
        slug=d.slug,
        name=d.name,
        version=d.version,
        source=row.source,
        description=d.description,
        language=d.language,
        links=d.links,
        capabilities=d.capabilities,
        settings=[x.model_dump() for x in d.settings],
        options=[x.model_dump() for x in d.options],
        yaml=row.yaml if full else None,
    )


@definitions_router.get("", response_model=list[DefinitionOut], dependencies=_read)
async def list_definitions(request: Request) -> list[DefinitionOut]:
    """Built-in and user definitions. Add an indexer from one with
    `POST /indexers {"kind": "definition", "settings": {"definition": slug, ...}}`."""
    return [_def_out(r, d) for r, d in await _svc(request).definitions()]


@definitions_router.post(
    "",
    response_model=DefinitionOut,
    status_code=201,
    dependencies=_write,
    responses=pr(409, 422),
)
async def add_definition(
    body: DefinitionIn, request: Request, response: Response, p: CurrentPrincipal
) -> DefinitionOut:
    """Add your own definition (format: see the docs). 422 lists every problem found."""
    row, d = await _svc(request).add_definition(body.yaml, actor=p.actor())
    response.headers["Location"] = f"{request.url.path}/{d.slug}"
    return _def_out(row, d, full=True)


@definitions_router.get(
    "/{slug}", response_model=DefinitionOut, dependencies=_read, responses=pr(404)
)
async def get_definition(slug: str, request: Request) -> DefinitionOut:
    row, d = await _svc(request).definition(slug)
    return _def_out(row, d, full=True)


@definitions_router.delete("/{slug}", status_code=204, dependencies=_write, responses=pr(404, 409))
async def delete_definition(slug: str, request: Request, p: CurrentPrincipal) -> Response:
    """User definitions only, and only while no indexer uses them."""
    await _svc(request).delete_definition(slug, actor=p.actor())
    return Response(status_code=204)
