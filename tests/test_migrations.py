"""Every ORM change must ship with a matching Alembic migration."""

from __future__ import annotations

from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from fetchbox import migrations
from fetchbox.core.db import Base
from fetchbox.models import load_all_models


def test_no_drift_between_models_and_migrations(tmp_path: Path) -> None:
    db = (tmp_path / "drift.db").as_posix()
    migrations.upgrade(f"sqlite+aiosqlite:///{db}")
    load_all_models()
    engine = create_engine(f"sqlite:///{db}")
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn, opts={"compare_type": True})
            assert compare_metadata(ctx, Base.metadata) == []
    finally:
        engine.dispose()


def test_downgrade_to_base_and_back(tmp_path: Path) -> None:
    from alembic import command

    url = f"sqlite+aiosqlite:///{(tmp_path / 'rt.db').as_posix()}"
    migrations.upgrade(url)
    command.downgrade(migrations.alembic_config(url), "base")
    migrations.upgrade(url)
