"""/plugins: the installed plugins, turning them on and off, their settings, actions,
installing and removing."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from pydantic import BaseModel, Field

from fetchbox.core.errors import FieldError, NotFoundError, ProblemError
from fetchbox.core.errors import problem_responses as pr
from fetchbox.modules.auth.deps import require
from fetchbox.plugin.manager import PluginError, PluginManager

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
