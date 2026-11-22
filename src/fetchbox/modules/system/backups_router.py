from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from fetchbox.core.errors import NotFoundError, ProblemError
from fetchbox.core.errors import problem_responses as pr
from fetchbox.core.jobs import JobContext, JobRunner
from fetchbox.core.schedules import ScheduleDef, Scheduler
from fetchbox.modules.auth.deps import CurrentPrincipal, require
from fetchbox.modules.system.backups import BackupError, BackupInfo, Backups

router = APIRouter(prefix="/backups", tags=["system"])
_read = [Depends(require("system:read"))]
_write = [Depends(require("system:write"))]
BACKUP_JOB = "system.backup"
MAX_UPLOAD = 2 * 1024**3


def register(jobs: JobRunner, scheduler: Scheduler, backups: Backups) -> None:
    """Weekly backup (the last 8 kept)."""

    @jobs.handler(BACKUP_JOB, queue="maintenance", max_attempts=1)
    async def run(ctx: JobContext) -> dict[str, Any]:
        b = await asyncio.to_thread(backups.create, "scheduled")
        return {"backup": b.id, "size": b.size}

    what = "Back up the database and settings (the last 8 are kept)"
    scheduler.define(ScheduleDef("backup", BACKUP_JOB, timedelta(days=7), what))


class BackupOut(BaseModel):
    id: str
    size: int
    created_at: datetime
    reason: str = Field(description="manual, scheduled, pre-migration, before-restore, uploaded")
    app_version: str
    schema_revision: str | None
    includes_secrets_key: bool


class BackupIn(BaseModel):
    password: str | None = Field(
        default=None, min_length=8,
        description="Include the secrets key, encrypted with this password (needed to "
        "restore on another computer)",
    )  # fmt: skip


class RestoreIn(BaseModel):
    password: str | None = Field(default=None, description="The backup's key password")


class RestoreOut(BaseModel):
    staged: BackupOut
    restarting: bool = Field(description="False: restart Fetchbox yourself to finish")


def _svc(request: Request) -> Backups:
    svc: Backups = request.app.state.backups
    return svc


def _out(b: BackupInfo) -> BackupOut:
    return BackupOut(id=b.id, size=b.size, created_at=b.created_at, reason=b.reason,
                     app_version=b.app_version, schema_revision=b.schema_revision,
                     includes_secrets_key=b.includes_secrets_key)  # fmt: skip


def _problem(exc: BackupError) -> ProblemError:
    if exc.message.startswith("no backup"):
        return NotFoundError("backup", exc.message.split("'")[1] if "'" in exc.message else "")
    return ProblemError(422, "Validation Failed", exc.message, slug="validation")


@router.get("", response_model=list[BackupOut], dependencies=_read)
async def list_backups(request: Request) -> list[BackupOut]:
    return [_out(b) for b in await asyncio.to_thread(_svc(request).list)]


@router.post("", response_model=BackupOut, status_code=201, dependencies=_write,
             responses=pr(422))  # fmt: skip
async def make_backup(request: Request, body: BackupIn | None = None) -> BackupOut:
    """Back up the database and settings now (safe while Fetchbox runs)."""
    try:
        b = await asyncio.to_thread(_svc(request).create, "manual", body.password if body else None)
    except BackupError as exc:
        raise _problem(exc) from None
    return _out(b)


@router.get("/{backup_id}", dependencies=_read, responses=pr(404),
            response_class=FileResponse)  # fmt: skip
async def download_backup(backup_id: str, request: Request) -> FileResponse:
    try:
        b = _svc(request).get(backup_id)
    except BackupError as exc:
        raise _problem(exc) from None
    return FileResponse(b.path, media_type="application/zip", filename=b.path.name)


@router.delete("/{backup_id}", status_code=204, dependencies=_write, responses=pr(404))
async def delete_backup(backup_id: str, request: Request) -> Response:
    try:
        await asyncio.to_thread(_svc(request).delete, backup_id)
    except BackupError as exc:
        raise _problem(exc) from None
    return Response(status_code=204)


@router.post("/upload", response_model=BackupOut, status_code=201, dependencies=_write,
             responses=pr(422))  # fmt: skip
async def upload_backup(file: UploadFile, request: Request) -> BackupOut:
    """Add a backup made elsewhere (checked before it's kept)."""
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise ProblemError(422, "Validation Failed", "the file is too large", slug="validation")
    try:
        b = await asyncio.to_thread(_svc(request).add_upload, data, file.filename or "upload.zip")
    except BackupError as exc:
        raise _problem(exc) from None
    return _out(b)


@router.post("/{backup_id}/restore", response_model=RestoreOut, status_code=202,
             dependencies=_write, responses=pr(404, 409, 422))  # fmt: skip
async def restore_backup(
    backup_id: str, request: Request, p: CurrentPrincipal, body: RestoreIn | None = None
) -> RestoreOut:
    """Restore a backup: it is staged, Fetchbox stops, and the next start applies it
    (keeping the current data as a before-restore backup) and brings it up to date."""
    svc = _svc(request)
    try:
        info = await asyncio.to_thread(
            svc.stage_restore, backup_id, body.password if body else None
        )
    except BackupError as exc:
        raise _problem(exc) from None
    await request.app.state.fb.bus.emit("system.restore_staged", actor=p.actor(), backup=info.id)
    stop = getattr(request.app.state, "request_shutdown", None)
    if stop is not None:
        asyncio.get_running_loop().call_later(0.5, stop)
    return RestoreOut(staged=_out(info), restarting=stop is not None)
