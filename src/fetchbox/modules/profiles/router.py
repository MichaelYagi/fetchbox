from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field, computed_field

from fetchbox.core.concurrency import etag
from fetchbox.core.errors import problem_responses as pr
from fetchbox.modules.auth.deps import CurrentPrincipal, require
from fetchbox.modules.profiles.formats import Condition, ReleaseInfo
from fetchbox.modules.profiles.models import CustomFormat, QualityProfile
from fetchbox.modules.profiles.profile import ProfileSpec, Tier, evaluate, language_ok
from fetchbox.modules.profiles.qualities import CATALOG, SizeLimit, default_size, size_ok
from fetchbox.modules.profiles.service import ProfilesService, to_spec
from fetchbox.modules.releases.parser import ParsedRelease, parse_release

router = APIRouter(tags=["profiles"])
_read = [Depends(require("profiles:read"))]
_write = [Depends(require("profiles:write"))]
IfMatch = Annotated[str | None, Header(description="ETag from a previous read; 412 if stale")]


def _svc(request: Request) -> ProfilesService:
    svc: ProfilesService = request.app.state.profiles
    return svc


class _Versioned(BaseModel):
    version: int = Field(exclude=True)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def etag(self) -> str:
        return etag(self.version)


# ---- qualities ----------------------------------------------------------------------------


class QualityOut(BaseModel):
    name: str
    resolution: int | None
    default_size: SizeLimit


@router.get("/qualities", response_model=list[QualityOut], dependencies=_read)
async def list_qualities() -> list[QualityOut]:
    """Every quality name a release can parse to, worst first, with default size bounds."""
    return [
        QualityOut(name=q.name, resolution=q.resolution, default_size=default_size(q.name))
        for q in CATALOG
    ]


# ---- quality profiles ---------------------------------------------------------------------


class ProfileOut(ProfileSpec, _Versioned):
    model_config = ConfigDict(from_attributes=True)

    id: str
    created_at: datetime
    updated_at: datetime


class ProfilePatch(BaseModel):
    """Any subset of profile fields; the result is validated as a whole."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    ladder: list[Tier] | None = None
    cutoff: str | None = None
    upgrade_allowed: bool | None = None
    min_format_score: int | None = None
    cutoff_format_score: int | None = None
    upgrade_min_score_increment: int | None = Field(default=None, ge=1)
    format_scores: dict[str, int] | None = None
    languages: list[str] | None = None
    sizes: dict[str, SizeLimit] | None = None


def _profile_out(row: QualityProfile, response: Response | None = None) -> ProfileOut:
    out = ProfileOut.model_validate(
        {
            **to_spec(row).model_dump(),
            "id": row.id,
            "version": row.version,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
    )
    if response is not None:
        response.headers["ETag"] = out.etag
    return out


@router.get("/quality-profiles", response_model=list[ProfileOut], dependencies=_read)
async def list_profiles(request: Request) -> list[ProfileOut]:
    return [_profile_out(r) for r in await _svc(request).list_profiles()]


@router.post(
    "/quality-profiles",
    response_model=ProfileOut,
    status_code=201,
    dependencies=_write,
    responses=pr(409, 422),
)
async def create_profile(
    body: ProfileSpec, request: Request, response: Response, p: CurrentPrincipal
) -> ProfileOut:
    row = await _svc(request).create_profile(body, p.actor())
    response.headers["Location"] = f"{request.url.path}/{row.id}"
    return _profile_out(row, response)


@router.get(
    "/quality-profiles/{profile_id}",
    response_model=ProfileOut,
    dependencies=_read,
    responses=pr(404),
)
async def get_profile(profile_id: str, request: Request, response: Response) -> ProfileOut:
    return _profile_out(await _svc(request).get_profile(profile_id), response)


@router.patch(
    "/quality-profiles/{profile_id}",
    response_model=ProfileOut,
    dependencies=_write,
    responses=pr(404, 409, 412, 422),
)
async def update_profile(
    profile_id: str,
    body: ProfilePatch,
    request: Request,
    response: Response,
    p: CurrentPrincipal,
    if_match: IfMatch = None,
) -> ProfileOut:
    changes = body.model_dump(mode="json", exclude_unset=True)
    row = await _svc(request).update_profile(
        profile_id, changes, if_match=if_match, actor=p.actor()
    )
    return _profile_out(row, response)


@router.delete(
    "/quality-profiles/{profile_id}",
    status_code=204,
    dependencies=_write,
    responses=pr(404, 412),
)
async def delete_profile(
    profile_id: str, request: Request, p: CurrentPrincipal, if_match: IfMatch = None
) -> Response:
    await _svc(request).delete_profile(profile_id, if_match=if_match, actor=p.actor())
    return Response(status_code=204)


# ---- evaluate -----------------------------------------------------------------------------


class ReleaseIn(BaseModel):
    title: str = Field(min_length=1, max_length=1000)
    size: int | None = Field(default=None, ge=0, description="Bytes")
    indexer_flags: list[str] = []


class EvaluateIn(BaseModel):
    releases: list[ReleaseIn] = Field(min_length=1, max_length=500)
    original_language: str | None = Field(default=None, pattern="^[a-z]{2}$")
    runtime_minutes: int | None = Field(default=None, gt=0)


class MatchedFormatOut(BaseModel):
    id: str
    name: str
    score: int


class EvaluationOut(BaseModel):
    title: str
    parsed: ParsedRelease
    quality: str
    tier: str | None
    allowed: bool
    format_score: int
    matched_formats: list[MatchedFormatOut]
    meets_min_score: bool
    cutoff_met: bool
    language_ok: bool
    size_ok: bool | None = Field(description="None when size or runtime is unknown")


@router.post(
    "/quality-profiles/{profile_id}/evaluate",
    response_model=list[EvaluationOut],
    dependencies=_read,
    responses=pr(404, 422),
)
async def evaluate_releases(
    profile_id: str, body: EvaluateIn, request: Request
) -> list[EvaluationOut]:
    """Preview how this profile treats release names: quality tier, formats, score."""
    svc = _svc(request)
    spec = to_spec(await svc.get_profile(profile_id))
    formats = await svc.format_specs()
    out = []
    for rel in body.releases:
        parsed = parse_release(rel.title)
        info = ReleaseInfo(parsed, rel.size, frozenset(f.lower() for f in rel.indexer_flags))
        ev = evaluate(spec, info, formats)
        fits: bool | None = None
        if rel.size is not None and body.runtime_minutes:
            fits = size_ok(spec.size_limit(ev.quality), rel.size, body.runtime_minutes)
        out.append(
            EvaluationOut(
                title=rel.title,
                parsed=parsed,
                quality=ev.quality,
                tier=ev.tier,
                allowed=ev.allowed,
                format_score=ev.format_score.total,
                matched_formats=[MatchedFormatOut(**m.__dict__) for m in ev.format_score.matched],
                meets_min_score=ev.meets_min_score,
                cutoff_met=ev.cutoff_met,
                language_ok=language_ok(spec, parsed, body.original_language),
                size_ok=fits,
            )
        )
    return out


# ---- custom formats -----------------------------------------------------------------------


class FormatIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    conditions: list[Condition] = Field(min_length=1, max_length=50)
    default_score: int = 0


class FormatPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    conditions: list[Condition] | None = Field(default=None, min_length=1, max_length=50)
    default_score: int | None = None


class FormatOut(_Versioned):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    conditions: list[Condition]
    default_score: int
    created_at: datetime
    updated_at: datetime


def _format_out(row: CustomFormat, response: Response | None = None) -> FormatOut:
    out = FormatOut.model_validate(row)
    if response is not None:
        response.headers["ETag"] = out.etag
    return out


@router.get("/custom-formats", response_model=list[FormatOut], dependencies=_read)
async def list_formats(request: Request) -> list[FormatOut]:
    return [_format_out(r) for r in await _svc(request).list_formats()]


@router.post(
    "/custom-formats",
    response_model=FormatOut,
    status_code=201,
    dependencies=_write,
    responses=pr(409, 422),
)
async def create_format(
    body: FormatIn, request: Request, response: Response, p: CurrentPrincipal
) -> FormatOut:
    data: dict[str, Any] = body.model_dump(mode="json", exclude_none=True)
    row = await _svc(request).create_format(
        name=body.name,
        conditions=data["conditions"],
        default_score=body.default_score,
        actor=p.actor(),
    )
    response.headers["Location"] = f"{request.url.path}/{row.id}"
    return _format_out(row, response)


@router.get(
    "/custom-formats/{format_id}",
    response_model=FormatOut,
    dependencies=_read,
    responses=pr(404),
)
async def get_format(format_id: str, request: Request, response: Response) -> FormatOut:
    return _format_out(await _svc(request).get_format(format_id), response)


@router.patch(
    "/custom-formats/{format_id}",
    response_model=FormatOut,
    dependencies=_write,
    responses=pr(404, 409, 412, 422),
)
async def update_format(
    format_id: str,
    body: FormatPatch,
    request: Request,
    response: Response,
    p: CurrentPrincipal,
    if_match: IfMatch = None,
) -> FormatOut:
    changes = {
        k: v
        for k, v in body.model_dump(mode="json", exclude_unset=True, exclude_none=False).items()
        if v is not None
    }
    if "conditions" in changes:  # drop unset optional keys inside each condition
        changes["conditions"] = [
            {k: v for k, v in c.items() if v is not None} for c in changes["conditions"]
        ]
    row = await _svc(request).update_format(format_id, changes, if_match=if_match, actor=p.actor())
    return _format_out(row, response)


@router.delete(
    "/custom-formats/{format_id}",
    status_code=204,
    dependencies=_write,
    responses=pr(404, 412),
)
async def delete_format(
    format_id: str, request: Request, p: CurrentPrincipal, if_match: IfMatch = None
) -> Response:
    """Also removes the format's score from every profile."""
    await _svc(request).delete_format(format_id, if_match=if_match, actor=p.actor())
    return Response(status_code=204)
