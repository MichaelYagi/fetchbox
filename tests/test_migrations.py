"""Every ORM change must ship with a matching Alembic migration."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

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


def _insert(conn: Any, table: str, **values: Any) -> str:
    """A row with every required column filled in (placeholders; the check is on the kind)."""
    for _cid, name, typ, notnull, default, pk in conn.exec_driver_sql(
        f"PRAGMA table_info({table})"
    ):
        if (notnull or pk) and default is None and name not in values:
            values[name] = 1 if "INT" in typ.upper() else f"{name}-{uuid4().hex[:8]}"
    cols, params = ", ".join(values), ", ".join(f":{c}" for c in values)
    conn.execute(text(f"INSERT INTO {table} ({cols}) VALUES ({params})"), values)
    return str(values["id"])


def test_kind_columns_open_for_plugin_kinds_and_back(tmp_path: Path) -> None:
    """0022: titles and library folders take a plugin's kind; core rows are kept; going back
    refuses (and changes nothing) while a plugin's rows exist, then restores the old rule."""
    from alembic import command

    url = f"sqlite+aiosqlite:///{(tmp_path / 'kinds.db').as_posix()}"
    migrations.upgrade(url, "0021_media_file_sidecar")
    engine = create_engine(url.replace("+aiosqlite", ""))
    try:
        with engine.begin() as conn:
            profile = _insert(conn, "quality_profile", name="Any")
            node = _insert(conn, "storage_node", kind="local", name="This computer")
            title = {"profile_id": profile}
            folder = {"node_id": node, "link_mode": "copy"}
            for kind in ("movie", "series", "music"):
                _insert(conn, "title", kind=kind, name=kind, **title)
                _insert(conn, "destination", media_kind=kind, name=kind, **folder)
            with pytest.raises(IntegrityError):
                _insert(conn, "title", kind="comic", name="before", **title)

        migrations.upgrade(url)
        with engine.begin() as conn:
            _insert(conn, "title", kind="comic", name="Saga", **title)
            _insert(conn, "destination", media_kind="comic", name="Comics", **folder)
            for bad in ("", "c", "Comic", "graphicnovel"):
                with pytest.raises(IntegrityError), conn.begin_nested():
                    _insert(conn, "title", kind=bad, name=f"bad {bad}", **title)
        with engine.connect() as conn:
            rows = conn.exec_driver_sql("SELECT kind FROM title ORDER BY kind").scalars().all()
            assert rows == ["comic", "movie", "music", "series"]

        engine.dispose()
        with pytest.raises(RuntimeError, match="plugin's media kind"):
            command.downgrade(migrations.alembic_config(url), "0021_media_file_sidecar")
        with engine.connect() as conn:  # nothing changed
            assert conn.exec_driver_sql("SELECT count(*) FROM title").scalar() == 4
            version = conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
            assert version == "0022_open_media_kinds"

        with engine.begin() as conn:
            conn.exec_driver_sql("DELETE FROM title WHERE kind = 'comic'")
            conn.exec_driver_sql("DELETE FROM destination WHERE media_kind = 'comic'")
        engine.dispose()
        command.downgrade(migrations.alembic_config(url), "0021_media_file_sidecar")
        with engine.begin() as conn:
            assert conn.exec_driver_sql("SELECT count(*) FROM title").scalar() == 3
            assert conn.exec_driver_sql("SELECT count(*) FROM destination").scalar() == 3
            with pytest.raises(IntegrityError):
                _insert(conn, "title", kind="comic", name="after", **title)
    finally:
        engine.dispose()
