from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy.engine import Connection

from fetchbox.core.db import Base, is_sqlite, make_engine
from fetchbox.models import load_all_models

load_all_models()
target_metadata = Base.metadata
config = context.config


def _configure(connection: Connection) -> None:
    url = config.get_main_option("sqlalchemy.url") or ""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=is_sqlite(url),  # SQLite needs batch mode for ALTER
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async(url: str) -> None:
    engine = make_engine(url)
    async with engine.connect() as conn:
        await conn.run_sync(_configure)
    await engine.dispose()


def run_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_offline()
else:
    asyncio.run(_run_async(config.get_main_option("sqlalchemy.url") or ""))
