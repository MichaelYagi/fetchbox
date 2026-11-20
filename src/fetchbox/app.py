"""Application factory. The web UI, tray and CLI are all clients of the API built here."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import APIRouter, FastAPI

import fetchbox
from fetchbox import migrations
from fetchbox.config import Settings
from fetchbox.core.db import Database
from fetchbox.core.errors import install_error_handlers
from fetchbox.core.events import EventBus
from fetchbox.core.health import HealthRegistry
from fetchbox.core.providers import registry as provider_registry
from fetchbox.core.state import AppState
from fetchbox.modules.system import router as system_router
from fetchbox.modules.system.service import register_core_checks

API_PREFIX = "/api/v1"
log = structlog.get_logger("fetchbox.app")


def build_api_router() -> APIRouter:
    api = APIRouter(prefix=API_PREFIX)
    api.include_router(system_router.router)
    return api


def create_app(settings: Settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        if settings.run_migrations:
            # TODO(phase 4): automatic pre-migration backup before upgrading.
            await asyncio.to_thread(migrations.upgrade, settings.database_url)

        state = AppState(
            data_dir=settings.data_dir,
            db=Database(settings.database_url),
            bus=EventBus(),
            health=HealthRegistry(),
            providers=provider_registry,
        )
        plugins = state.providers.load_entry_points()
        register_core_checks(state)
        app.state.fb = state

        log.info(
            "started",
            version=fetchbox.__version__,
            data_dir=str(settings.data_dir),
            bind=f"{settings.host}:{settings.port}",
            plugins=plugins,
        )
        await state.bus.emit("system.started", version=fetchbox.__version__)
        try:
            yield
        finally:
            await state.bus.emit("system.stopping")
            await state.db.dispose()
            log.info("stopped")

    app = FastAPI(
        title="Fetchbox",
        version=fetchbox.__version__,
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url="/api/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    install_error_handlers(app)
    app.include_router(build_api_router())
    return app
