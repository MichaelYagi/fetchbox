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
    sqlite = is_sqlite(url)
    if sqlite:
        # SQLite changes a table by rebuilding it (copy, drop, rename), and dropping a
        # table with foreign keys on deletes every row linked to it, through the cascades:
        # 0.10.1 lost each title's seasons, episodes, wanted items and file links that
        # way. Off for the migrations (SQLite ignores this inside a transaction, hence
        # here, before it), every link checked before committing, then on again.
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.commit()  # (that began a transaction; the migrations need their own)
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=sqlite,  # SQLite needs batch mode for ALTER
        compare_type=True,
    )
    try:
        with context.begin_transaction():
            context.run_migrations()
            if sqlite:
                broken = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
                if broken:  # raising here rolls the migrations back: nothing changes
                    raise RuntimeError(f"a migration broke {len(broken)} link(s): {broken[:5]}")
    finally:
        if sqlite:
            connection.rollback()  # (a failed migration's leftovers, if any)
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.commit()


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
