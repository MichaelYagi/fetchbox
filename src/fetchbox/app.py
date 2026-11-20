"""Application factory. The web UI, tray and CLI are all clients of the API built here."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
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
from fetchbox.core.jobs import JobRunner
from fetchbox.core.providers import registry as provider_registry
from fetchbox.core.schedules import Scheduler
from fetchbox.core.secrets import load_or_create as load_secrets
from fetchbox.core.state import AppState
from fetchbox.modules.auth import router as auth_router
from fetchbox.modules.auth.service import AuthService
from fetchbox.modules.automation import router as automation_router
from fetchbox.modules.events import router as events_router
from fetchbox.modules.profiles import router as profiles_router
from fetchbox.modules.profiles.service import ProfilesService
from fetchbox.modules.system import activity_router, jobs_router
from fetchbox.modules.system import jobs as system_jobs
from fetchbox.modules.system import router as system_router
from fetchbox.modules.system.activity import ActivityWriter
from fetchbox.modules.system.service import register_core_checks

API_PREFIX = "/api/v1"
log = structlog.get_logger("fetchbox.app")


# Every module router mounted under /api/v1. tests/test_route_scopes.py checks that this
# list accounts for every operation in the OpenAPI document.
API_ROUTERS: tuple[APIRouter, ...] = (
    auth_router.router,
    system_router.router,
    jobs_router.router,
    events_router.router,
    activity_router.router,
    automation_router.router,
    profiles_router.router,
)


def build_api_router() -> APIRouter:
    api = APIRouter(prefix=API_PREFIX)
    for r in API_ROUTERS:
        api.include_router(r)
    return api


def print_bootstrap_key(key: str) -> None:
    """Show the first-run key once, on stdout, bypassing logging (which redacts keys)."""
    bar = "=" * 72
    print(
        f"\n{bar}\n Fetchbox first run\n\n"
        f" API key 'default' (scope *), shown only once:\n\n   {key}\n\n"
        f" Set the admin password in the web UI, or: fetchbox reset-password\n{bar}\n",
        flush=True,
    )


class StartupRefused(RuntimeError):
    pass


def create_app(
    settings: Settings,
    *,
    job_handler_hooks: Sequence[Callable[[JobRunner], None]] = (),
    on_bootstrap_key: Callable[[str], None] = print_bootstrap_key,
) -> FastAPI:
    """`job_handler_hooks` let modules (and tests) register job handlers before start."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        if settings.run_migrations:
            # TODO(phase 4): automatic pre-migration backup before upgrading.
            await asyncio.to_thread(migrations.upgrade, settings.database_url)

        db = Database(settings.database_url)
        bus = EventBus()
        jobs = JobRunner(db, bus)
        box, _created = load_secrets(settings.data_dir)
        state = AppState(
            data_dir=settings.data_dir,
            db=db,
            bus=bus,
            health=HealthRegistry(),
            providers=provider_registry,
            jobs=jobs,
            scheduler=Scheduler(db, jobs),
            secrets=box,
        )
        auth = AuthService(db, bus)
        app.state.auth = auth
        if (first_key := await auth.bootstrap()) is not None:
            on_bootstrap_key(first_key)
        if not settings.is_loopback and not await auth.has_password():
            await db.dispose()
            raise StartupRefused(
                f"refusing to bind {settings.host}: set an admin password first "
                "(fetchbox reset-password), or bind to 127.0.0.1"
            )

        profiles = ProfilesService(db, bus)
        await profiles.seed_defaults()
        app.state.profiles = profiles

        activity_writer = ActivityWriter(db, bus)
        activity_writer.start()  # before anything else emits, so startup is logged
        app.state.activity = activity_writer

        plugins = state.providers.load_entry_points()
        register_core_checks(state)
        app.state.fb = state
        system_jobs.register(state)
        for hook in job_handler_hooks:
            hook(state.jobs)
        await state.jobs.start()
        await state.scheduler.start()

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
            await state.scheduler.stop()
            await state.jobs.stop(timeout=30)
            await activity_writer.stop()
            await auth.drain()
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
