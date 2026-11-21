"""Application factory. The web UI, tray and CLI are all clients of the API built here."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

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
from fetchbox.modules.automation.search import AutoSearch
from fetchbox.modules.automation.unlock import UnlockService
from fetchbox.modules.discovery import router as discovery_router
from fetchbox.modules.discovery.service import DiscoveryService
from fetchbox.modules.downloads import router as downloads_router
from fetchbox.modules.downloads.service import DownloadsService
from fetchbox.modules.events import router as events_router
from fetchbox.modules.library import router as library_router
from fetchbox.modules.library.service import LibraryService
from fetchbox.modules.media import router as media_router
from fetchbox.modules.media.service import MediaService
from fetchbox.modules.profiles import router as profiles_router
from fetchbox.modules.profiles.service import ProfilesService
from fetchbox.modules.releases import router as releases_router
from fetchbox.modules.releases.grabs import GrabsService
from fetchbox.modules.releases.service import ReleasesService
from fetchbox.modules.sources import router as sources_router
from fetchbox.modules.sources.service import SourcesService
from fetchbox.modules.storage import router as storage_router
from fetchbox.modules.storage.service import StorageService
from fetchbox.modules.system import activity_router, jobs_router
from fetchbox.modules.system import jobs as system_jobs
from fetchbox.modules.system import router as system_router
from fetchbox.modules.system.activity import ActivityWriter
from fetchbox.modules.system.config import ConfigService
from fetchbox.modules.system.service import register_core_checks
from fetchbox.providers import builtin as _builtin_providers  # noqa: F401 (registers them)
from fetchbox.ui import STATIC_DIR, install_ui

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
    automation_router.search_router,
    profiles_router.router,
    discovery_router.router,
    media_router.router,
    sources_router.router,
    sources_router.definitions_router,
    releases_router.router,
    releases_router.grabs_router,
    downloads_router.router,
    storage_router.router,
    library_router.router,
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
    static_dir: Path | None = None,
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

        env = {}
        if settings.tmdb_api_key is not None:
            env["tmdb.api_key"] = settings.tmdb_api_key.get_secret_value()
        config = ConfigService(db, bus, box, env=env)
        await config.load()
        app.state.config = config
        discovery = DiscoveryService(config)
        app.state.discovery = discovery
        state.health.register("metadata", discovery.health, fix_url="/settings/general#tmdb")
        media = MediaService(db, bus, discovery, profiles)
        media.register_jobs(jobs, state.scheduler)
        app.state.media = media
        sources = SourcesService(db, bus, box, provider_registry)
        await sources.sync_definitions(seed=settings.seed_indexers)
        app.state.sources = sources
        state.health.register("indexers", sources.health, fix_url="/settings/indexers")
        grabs = GrabsService(db, bus)
        app.state.grabs = grabs
        releases = ReleasesService(db, bus, grabs, media, profiles, config, sources)
        app.state.releases = releases
        autosearch = AutoSearch(
            db,
            bus,
            jobs,
            releases=releases,
            grabs=grabs,
            media=media,
            sources=sources,
            config=config,
        )
        autosearch.register_jobs()
        app.state.autosearch = autosearch
        downloads = DownloadsService(
            db,
            bus,
            box,
            provider_registry,
            jobs,
            sources,
            downloads_root=settings.downloads_dir,
            state_dir=settings.data_dir / "torrents",
            isolated=settings.torrent_isolated,
        )
        downloads.register_jobs()
        app.state.downloads = downloads
        storage = StorageService(
            db,
            bus,
            downloads_root=settings.downloads_dir,
            torrents_dir=settings.downloads_dir / "Torrents",
        )
        app.state.storage = storage
        library = LibraryService(db, bus, jobs, storage, downloads, profiles, media)
        library.register_jobs()
        app.state.library = library
        app.state.unlock = UnlockService(
            db, bus, library=library, grabs=grabs, autosearch=autosearch
        )
        await storage.seed(await library.seed())
        await storage.recover()

        activity_writer = ActivityWriter(db, bus)
        activity_writer.start()  # before anything else emits, so startup is logged
        app.state.activity = activity_writer

        plugins = state.providers.load_entry_points()
        register_core_checks(state)
        # Replaces the core "bindings import" check with the running engine's state.
        state.health.register(
            "torrent_engine", downloads.health, fix_url="/settings/download-clients"
        )
        state.health.register("storage", storage.health, fix_url="/settings/storage")
        app.state.fb = state
        system_jobs.register(state)
        for hook in job_handler_hooks:
            hook(state.jobs)
        await state.jobs.start()
        # Engine and import before the scheduler: its run-at-start health check would
        # otherwise see the torrent engine "not running".
        await downloads.start()
        await library.start()
        await autosearch.start()
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
            await autosearch.stop()
            await library.stop()
            await downloads.stop()  # saves the engine's resume data
            await activity_writer.stop()
            await auth.drain()
            await discovery.aclose()
            await sources.aclose()
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
    install_ui(app, static_dir or STATIC_DIR)
    return app
