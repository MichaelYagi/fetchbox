from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from fetchbox.core.errors import ProblemError
from fetchbox.core.errors import problem_responses as pr
from fetchbox.modules.auth.deps import CurrentPrincipal, require
from fetchbox.modules.system.portable import ImportError_, Portable

router = APIRouter(prefix="/config", tags=["system"])
_read = [Depends(require("system:read"))]
_write = [Depends(require("system:write"))]


def _svc(request: Request) -> Portable:
    svc: Portable = request.app.state.portable
    return svc


class ExportIn(BaseModel):
    password: str = Field(min_length=8, description="Encrypts the secrets in the export")


class ImportIn(BaseModel):
    document: dict[str, Any] = Field(description="A Fetchbox config export")
    password: str | None = Field(default=None, description="If the export holds secrets")
    apply: bool = Field(default=False, description="False: only show what would change")


class PlanPart(BaseModel):
    create: list[str]
    update: list[str]
    unchanged: list[str]


class ConfigImportOut(BaseModel):
    applied: bool
    plan: dict[str, PlanPart]
    restarting: bool = Field(default=False, description="Applied: Fetchbox restarts to use it")


@router.get("/export", dependencies=_read)
async def export_config(request: Request) -> dict[str, Any]:
    """All configuration as one JSON document (no media data). Secrets are left out:
    POST with a password to include them, encrypted."""
    return await _svc(request).export()


@router.post("/export", dependencies=_write, responses=pr(422))
async def export_config_with_secrets(body: ExportIn, request: Request) -> dict[str, Any]:
    """The same, with secrets (API keys, passwords) encrypted with ``password``."""
    return await _svc(request).export(body.password)


@router.post("/import", response_model=ConfigImportOut, dependencies=_write, responses=pr(422))
async def import_config(body: ImportIn, request: Request, p: CurrentPrincipal) -> ConfigImportOut:
    """Compare an export with this install (``apply: false``, the default) or apply it in
    one transaction. Rows match by id, then by name; nothing is deleted. After applying,
    Fetchbox restarts so every part uses the new configuration."""
    try:
        out = await _svc(request).import_(body.document, password=body.password, apply=body.apply)
    except ImportError_ as exc:
        raise ProblemError(422, "Validation Failed", exc.message, slug="validation") from None
    restarting = False
    if body.apply:
        await request.app.state.fb.bus.emit("system.config_imported", actor=p.actor())
        stop = getattr(request.app.state, "request_shutdown", None)
        if stop is not None:
            asyncio.get_running_loop().call_later(0.5, stop)
            restarting = True
    return ConfigImportOut(applied=out["applied"], plan=out["plan"], restarting=restarting)
