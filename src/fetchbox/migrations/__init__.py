"""Alembic migrations, shipped inside the package so frozen builds include them."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import AsyncEngine

HERE = Path(__file__).resolve().parent


def alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(HERE))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def upgrade(url: str, revision: str = "head") -> None:
    """Blocking; env.py runs its own event loop, so call this via asyncio.to_thread."""
    command.upgrade(alembic_config(url), revision)


def head_revision() -> str | None:
    return ScriptDirectory(str(HERE)).get_current_head()


async def current_revision(engine: AsyncEngine) -> str | None:
    async with engine.connect() as conn:
        return await conn.run_sync(lambda c: MigrationContext.configure(c).get_current_revision())
