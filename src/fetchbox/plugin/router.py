"""/plugins: the installed plugins, turning them on and off, their settings, actions,
installing and removing."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from pydantic import BaseModel, Field

from fetchbox.core import kinds
from fetchbox.core.errors import FieldError, NotFoundError, ProblemError
from fetchbox.core.errors import problem_responses as pr
from fetchbox.modules.auth.deps import CurrentPrincipal, require
from fetchbox.plugin.manager import PluginError, PluginManager
from fetchbox.plugin.titles import PluginTitles

router = APIRouter(prefix="/plugins", tags=["plugins"])
_read = [Depends(require("plugins:read"))]
_write = [Depends(require("plugins:write"))]
MAX_WHEEL = 50 * 1024 * 1024


class PermissionOut(BaseModel):
    id: str
    words: str


class ContributionOut(BaseModel):
    id: str
    name: str
    kind: str = Field(description="media_kind, addon or provider")


class SettingsOut(BaseModel):
    id: str = Field(description="The contribution the settings are for")
    name: str
    kind: str
    json_schema: dict[str, Any] | None = Field(
        alias="schema", description="The settings form, as JSON Schema"
    )
    values: dict[str, Any] = Field(description="Stored values; Secrets as •••••••• when set")
    missing: list[str] = Field(description="Settings still needed before it can run")
    has_test: bool

    model_config = {"populate_by_name": True}


class ActionOut(BaseModel):
    addon: str
    method: str
    label: str
    on: str = Field(description="title, group or item")


class PluginOut(BaseModel):
    id: str
    name: str
    description: str
    version: str | None
    homepage: str | None
    api: int | None
    api_supported: bool
    permissions: list[PermissionOut]
    contributes: list[ContributionOut]
    source: str
    approved: bool
    enabled: bool
    running: bool
    paused_until: datetime | None = Field(
        None, description="Failing repeatedly: its jobs wait until then"
    )
    last_error: str | None = Field(None, description="Its last failed job's error")
    error: str | None
    settings: list[SettingsOut]
    actions: list[ActionOut]


class PluginPatch(BaseModel):
    enabled: bool = Field(description="On (approves it) or off")


class SettingsIn(BaseModel):
    values: dict[str, Any]


class TestOut(BaseModel):
    ok: bool
    message: str


class ActionIn(BaseModel):
    item: str | None = Field(None, description="The title, group or item it's run for")


class PluginJobOut(BaseModel):
    job_id: str | None


def _mgr(request: Request) -> PluginManager:
    m: PluginManager = request.app.state.plugins
    return m


def _out(m: PluginManager, pid: str) -> PluginOut:
    if pid not in m.records and pid not in m.found:
        raise NotFoundError("plugin", pid)
    return PluginOut.model_validate(m.describe(pid))


def _problem(exc: PluginError) -> ProblemError:
    return ProblemError(409, "Conflict", str(exc), slug="conflict")


class PanelOut(BaseModel):
    plugin: str
    addon: str
    method: str
    title: str
    status: str
    table: list[dict[str, Any]]
    actions: list[str] = Field(description="Labels of the add-on's actions to offer")
    error: str | None = Field(description="Why the panel couldn't be drawn")


class MediaKindOut(BaseModel):
    id: str
    name: str
    plural: str
    levels: list[str]
    plugin: str


class HitOut(BaseModel):
    ref: str
    name: str
    year: int | None
    overview: str
    poster_url: str | None
    title_id: str | None = Field(None, description="Already in the library: its title")


class PluginTitleIn(BaseModel):
    ref: str = Field(description="The kind's id for it, from search")
    monitor: Literal["all", "future", "none"] = "all"


class PluginTitleOut(BaseModel):
    id: str


class PluginItemOut(BaseModel):
    id: str = Field(description="The wanted item")
    ref: str
    number: float | None
    name: str | None
    released: date | None
    state: str
    monitored: bool
    quality: str | None = Field(None, description="Its file's quality, when it has one")


class TitleQualities(BaseModel):
    available: list[str] = Field(description="The kind's qualities, best first")
    qualities: list[str] = Field(description="The ones this title accepts, best first")
    cutoff: str = Field(description="Good enough: no upgrade is looked for past it")


class TitleQualitiesIn(BaseModel):
    qualities: list[str] = Field(min_length=1)
    cutoff: str


def _titles(request: Request) -> PluginTitles:
    t: PluginTitles = request.app.state.plugin_titles
    return t


@router.get("/kinds", response_model=list[MediaKindOut], dependencies=_read)
async def media_kinds(request: Request) -> list[MediaKindOut]:
    """The media kinds running plugins add (none without one): Add page buttons, Library tabs."""
    out = []
    for kid, _k, got in _titles(request).running():
        d = kinds.get(kid)
        out.append(MediaKindOut(id=kid, name=d.label, plural=d.plural, levels=list(d.levels),
                           plugin=got.ctx.plugin_id))  # fmt: skip
    return out


@router.get(
    "/kinds/{kind}/search",
    response_model=list[HitOut],
    dependencies=_read,
    responses=pr(404, 502, 504),
)
async def search_kind(
    kind: str, request: Request, q: str = Query(..., min_length=1)
) -> list[HitOut]:
    t = _titles(request)
    hits = await t.search(kind, q)
    out = []
    for h in hits:
        mine = await t._existing(kind, h.ref)
        out.append(HitOut(ref=h.ref, name=h.name, year=h.year, overview=h.overview,
                          poster_url=h.poster_url, title_id=mine.id if mine else None))  # fmt: skip
    return out


@router.post(
    "/kinds/{kind}/titles", response_model=PluginTitleOut, status_code=201,
    dependencies=[Depends(require("media:write"))], responses=pr(404, 409, 502, 504),
)  # fmt: skip
async def add_title(
    kind: str, body: PluginTitleIn, request: Request, who: CurrentPrincipal
) -> PluginTitleOut:
    title = await _titles(request).add(kind, body.ref, monitor=body.monitor, actor=who.actor())
    return PluginTitleOut(id=title.id)


@router.post(
    "/titles/{title_id}/refresh", response_model=PluginTitleOut,
    dependencies=[Depends(require("media:write"))], responses=pr(404, 409, 502, 504),
)  # fmt: skip
async def refresh_title(title_id: str, request: Request, who: CurrentPrincipal) -> PluginTitleOut:
    title = await _titles(request).refresh(title_id, actor=who.actor())
    return PluginTitleOut(id=title.id)


@router.get(
    "/titles/{title_id}/items",
    response_model=list[PluginItemOut],
    dependencies=[Depends(require("media:read"))],
)
async def title_items(title_id: str, request: Request) -> list[PluginItemOut]:
    return [PluginItemOut.model_validate(i) for i in await _titles(request).items(title_id)]


class PluginSearchOut(BaseModel):
    searched: int = Field(description="Queries asked")
    results: int = Field(description="Releases the indexers gave")
    grabbed: int


@router.post(
    "/titles/{title_id}/search", response_model=PluginSearchOut,
    dependencies=[Depends(require("releases:write"))], responses=pr(404),
)  # fmt: skip
async def search_title(title_id: str, request: Request, who: CurrentPrincipal) -> PluginSearchOut:
    """Search now for a plugin kind's title's missing items, and grab the best releases."""
    pipeline = request.app.state.plugin_pipeline
    return PluginSearchOut.model_validate(await pipeline.search(title_id, actor=who.actor()))


async def _title_kind(request: Request, title_id: str) -> tuple[Any, Any]:
    title = await request.app.state.plugin_titles.media.get_title(title_id)
    kind, _got = _titles(request).kind(title.kind)
    return title, kind


@router.get(
    "/titles/{title_id}/qualities",
    response_model=TitleQualities,
    dependencies=[Depends(require("media:read"))],
    responses=pr(404),
)
async def title_qualities(title_id: str, request: Request) -> TitleQualities:
    title, kind = await _title_kind(request, title_id)
    chosen, cutoff = request.app.state.plugin_pipeline.qualities(title, kind)
    return TitleQualities(available=list(kind.qualities.names), qualities=chosen, cutoff=cutoff)


@router.put(
    "/titles/{title_id}/qualities",
    response_model=TitleQualities,
    dependencies=[Depends(require("media:write"))],
    responses=pr(404, 422),
)
async def set_title_qualities(
    title_id: str, body: TitleQualitiesIn, request: Request
) -> TitleQualities:
    """Which of the kind's qualities this title accepts (best first) and when it's good
    enough. A file below the cutoff is upgraded when a better release turns up."""
    title, kind = await _title_kind(request, title_id)
    names = list(kind.qualities.names)
    bad = [q for q in body.qualities if q not in names]
    if bad or body.cutoff not in body.qualities:
        raise ProblemError(
            422,
            "Validation Failed",
            slug="validation",
            errors=[
                FieldError(
                    field="body",
                    message=f"qualities are among {', '.join(names)}; the cutoff is one of them",
                )
            ],
        )
    ordered = [q for q in names if q in body.qualities]  # the kind's order: best first
    from fetchbox.modules.media.models import Title

    async with request.app.state.plugin_titles.db.sessionmaker() as s:
        row = await s.get(Title, title.id)
        row.meta = {**(row.meta or {}), "qualities": ordered, "cutoff": body.cutoff}
        await s.commit()
    return TitleQualities(available=names, qualities=ordered, cutoff=body.cutoff)


@router.get("/panels", response_model=list[PanelOut], dependencies=_read)
async def panels(
    request: Request,
    on: Literal["title", "group", "item"] = Query("title"),
    id: str = Query(..., description="The title's, group's or item's id"),
) -> list[PanelOut]:
    """The running add-ons' panels for a title page (none when no plugin has one)."""
    return [PanelOut.model_validate(p) for p in await _mgr(request).panels(on, id)]


@router.get("", response_model=list[PluginOut], dependencies=_read)
async def list_plugins(request: Request) -> list[PluginOut]:
    return [PluginOut.model_validate(p) for p in _mgr(request).all()]


@router.get("/{plugin_id}", response_model=PluginOut, dependencies=_read, responses=pr(404))
async def get_plugin(plugin_id: str, request: Request) -> PluginOut:
    return _out(_mgr(request), plugin_id)


@router.patch("/{plugin_id}", response_model=PluginOut, dependencies=_write, responses=pr(404, 409))
async def set_plugin(plugin_id: str, body: PluginPatch, request: Request) -> PluginOut:
    m = _mgr(request)
    _out(m, plugin_id)
    try:
        await m.set_enabled(plugin_id, body.enabled)
    except PluginError as exc:
        raise _problem(exc) from None
    return _out(m, plugin_id)


@router.put(
    "/{plugin_id}/settings/{part}",
    response_model=PluginOut,
    dependencies=_write,
    responses=pr(404, 409, 422),
)
async def save_settings(plugin_id: str, part: str, body: SettingsIn, request: Request) -> PluginOut:
    m = _mgr(request)
    _out(m, plugin_id)
    try:
        problems = await m.save_settings(plugin_id, part, body.values)
    except PluginError as exc:
        raise _problem(exc) from None
    if problems:
        raise ProblemError(
            422,
            "Validation Failed",
            slug="validation",
            errors=[
                FieldError(field=f"body.values.{p.split(':')[0]}", message=p) for p in problems
            ],
        )
    return _out(m, plugin_id)


@router.post(
    "/{plugin_id}/settings/{part}/test",
    response_model=TestOut,
    dependencies=_write,
    responses=pr(404, 409),
)
async def test_settings(plugin_id: str, part: str, body: SettingsIn, request: Request) -> TestOut:
    m = _mgr(request)
    _out(m, plugin_id)
    try:
        ok, message = await m.test_settings(plugin_id, part, body.values)
    except PluginError as exc:
        raise _problem(exc) from None
    return TestOut(ok=ok, message=message)


@router.post(
    "/{plugin_id}/actions/{addon}/{method}",
    response_model=PluginJobOut,
    status_code=202,
    dependencies=_write,
    responses=pr(404, 409),
)
async def run_action(
    plugin_id: str, addon: str, method: str, body: ActionIn, request: Request
) -> PluginJobOut:
    m = _mgr(request)
    _out(m, plugin_id)
    try:
        job = await m.host.run_action(plugin_id, addon, method, body.item)
    except LookupError as exc:
        raise ProblemError(
            409, "Conflict", f"{exc} (is the plugin running?)", slug="conflict"
        ) from None
    return PluginJobOut(job_id=job)


@router.post(
    "/install",
    response_model=PluginOut,
    status_code=201,
    dependencies=_write,
    responses=pr(409),  # (413 for a too large file: its name differs by Python version)
)
async def install(request: Request, file: UploadFile = File(...)) -> PluginOut:  # noqa: B008
    """Install a plugin from its wheel (.whl). It's approved and turned on; plugins run
    with Fetchbox's full access, so install only those from people you trust."""
    data = await file.read(MAX_WHEEL + 1)
    if len(data) > MAX_WHEEL:
        raise ProblemError(413, "Too Large", "a plugin wheel is at most 50 MB", slug="too-large")
    m = _mgr(request)
    try:
        pid = await m.install_wheel(data, file.filename or "plugin.whl")
    except PluginError as exc:
        raise _problem(exc) from None
    return _out(m, pid)


@router.delete("/{plugin_id}", status_code=204, dependencies=_write, responses=pr(404))
async def remove(plugin_id: str, request: Request, keep_settings: bool = False) -> None:
    m = _mgr(request)
    _out(m, plugin_id)
    await m.remove(plugin_id, keep_settings=keep_settings)
