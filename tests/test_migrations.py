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
    return str(values.get("id", ""))  # (link tables have none)


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


def _rebuild_points() -> list[str]:
    """The revision just before each migration that rebuilds a table (SQLite batch mode:
    copy, drop, rename), where a dropped table could take its linked rows with it."""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory(str(migrations.HERE))
    out = []
    for rev in script.walk_revisions():
        source = Path(rev.path).read_text(encoding="utf-8")
        if "batch_alter_table" in source and rev.down_revision:
            out.append(str(rev.down_revision))
    return sorted(set(out))


# Columns with a CHECK on their values: a value each allows
_VALID = {
    "kind": "movie", "media_kind": "movie", "state": "missing", "status": "queued",
    "link_mode": "copy", "lock": None, "monitor_mode": "all", "media": "video",
    "outcome": "ok", "role": "admin", "reason": "manual", "queue": "default",
    "info_hash": "0" * 40,
}  # fmt: skip


def _fill_every_table(conn: Any) -> dict[str, str]:
    """One row in every table, each linked to a row of every table it refers to (so a
    cascade anywhere would show). Parents first. Returns {table: row id}."""
    tables = [r[0] for r in conn.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
        "AND name != 'alembic_version'")]  # fmt: skip
    fks = {t: list(conn.exec_driver_sql(f"PRAGMA foreign_key_list({t})")) for t in tables}
    ids: dict[str, str] = {}
    pending = list(tables)
    while pending:
        ready = [t for t in pending if all(f[2] in ids or f[2] == t for f in fks[t])]
        assert ready, f"circular references among {pending}"
        for t in ready:
            values: dict[str, Any] = {}
            cols = {c[1]: c for c in conn.exec_driver_sql(f"PRAGMA table_info({t})")}
            for f in fks[t]:
                if f[2] != t and f[3] in cols:
                    values[f[3]] = ids[f[2]]
            for name in cols:
                if name in _VALID and name not in values:
                    values[name] = _VALID[name]
            if t == "album":
                values["kind"] = "album"
            if "id" in cols:
                values.setdefault("id", f"{t[:18]}-{uuid4().hex[:6]}")
            try:
                with conn.begin_nested():
                    _insert(conn, t, **values)
            except IntegrityError as exc:
                raise AssertionError(f"couldn't fill {t}: {exc.orig}") from None
            ids[t] = str(values.get("id", ""))
            pending.remove(t)
    return ids


def _count(conn: Any, table: str) -> int:
    return int(conn.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar() or 0)


@pytest.mark.parametrize("start", _rebuild_points())
def test_upgrading_keeps_every_row(tmp_path: Path, start: str) -> None:
    """Upgrading never deletes data. SQLite rebuilds a table to change it, and dropping the
    old one with foreign keys on cascades: 0.10.1's 0022 rebuilt `title` and so deleted
    every season, episode, album, track, wanted item, file link and blocklist entry of the
    titles already there. Every row in every table must survive, links intact."""
    url = f"sqlite+aiosqlite:///{(tmp_path / 'keep.db').as_posix()}"
    migrations.upgrade(url, start)
    engine = create_engine(url.replace("+aiosqlite", ""))
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            filled = _fill_every_table(conn)
            before = {t: _count(conn, t) for t in filled}
        engine.dispose()

        migrations.upgrade(url)
        with engine.connect() as conn:
            version = conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
            assert version == migrations.head_revision(), f"stayed at {version}: not upgraded"
            after = {t: _count(conn, t) for t in filled}
            lost = {t: f"{before[t]} → {after[t]}" for t in filled if after[t] < before[t]}
            assert not lost, f"upgrading from {start} deleted rows: {lost}"
            broken = list(conn.exec_driver_sql("PRAGMA foreign_key_check"))
            assert not broken, f"links broken after upgrading from {start}: {broken}"
    finally:
        engine.dispose()
