"""`fetchbox` command-line entry point."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from fetchbox import __version__


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fetchbox", description="Self-hosted media manager")
    p.add_argument("--version", action="version", version=f"fetchbox {__version__}")
    p.add_argument("--data", dest="data_dir", help="data directory (env: FETCHBOX_DATA_DIR)")
    sub = p.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the server")
    serve.add_argument("--host", help="bind address (default 0.0.0.0; 127.0.0.1: only here)")
    serve.add_argument("--port", type=int, help="port (default 8787)")
    serve.add_argument("--db-url", help="database URL (default SQLite in the data dir)")
    serve.add_argument("--log-level", choices=["debug", "info", "warning", "error"])
    serve.add_argument("--log-format", choices=["auto", "json", "console"])

    bk = sub.add_parser("backup", help="back up the database and settings now")
    bk.add_argument("--with-key", action="store_true",
                    help="include the secrets key, encrypted with a password you type")  # fmt: skip
    rs = sub.add_parser("restore", help="restore a backup (Fetchbox must be stopped)")
    rs.add_argument("file", help="a backup .zip")

    corpus = sub.add_parser("corpus", help="collect real release names for parser tests")
    corpus.add_argument("action", choices=["capture"])
    corpus.add_argument("--out", required=True, help="JSONL file to write")
    corpus.add_argument("--search", action="append", default=[], help="also search this")
    corpus.add_argument("--limit", type=int, default=500)

    tray = sub.add_parser("tray", help="desktop tray: start the server and open the UI")
    tray.add_argument("--host", help="bind address (default 0.0.0.0; 127.0.0.1: only here)")
    tray.add_argument("--port", type=int, help="port (default 8787)")

    mig = sub.add_parser("migrate", help="apply database migrations and exit")
    mig.add_argument("--db-url")
    mig.add_argument("--revision", default="head")

    rp = sub.add_parser("reset-password", help="set the admin password (signs out all sessions)")
    rp.add_argument("--db-url")
    rp.add_argument("--username", help="also rename the admin user")
    rp.add_argument("--password-stdin", action="store_true", help="read the password from stdin")

    pr = sub.add_parser("parse", help="show how release names are parsed (no server needed)")
    pr.add_argument("names", nargs="*", help="release names (default: stdin, one per line)")
    pr.add_argument("--all", action="store_true", help="include fields that are empty/default")

    sub.add_parser("openapi", help="print the API's OpenAPI document (for client generation)")

    # `--data` also works after the command (`fetchbox serve --data ./data`).
    for cmd in (serve, mig, rp):
        cmd.add_argument(
            "--data", dest="data_dir", default=argparse.SUPPRESS, help="data directory"
        )
    return p


def _openapi() -> int:
    import json
    import tempfile

    from fetchbox.app import create_app
    from fetchbox.config import load_settings

    with tempfile.TemporaryDirectory() as tmp:  # the schema needs no running app or data
        doc = create_app(load_settings(tmp)).openapi()
    print(json.dumps(doc, indent=1, sort_keys=True))
    return 0


def _parse_names(names: Sequence[str], *, show_all: bool) -> int:
    import json

    from fetchbox.modules.releases.parser import parse_release

    for name in names or (line.rstrip("\r\n") for line in sys.stdin):
        if not name.strip():
            continue
        result = parse_release(name)
        data = result.model_dump(mode="json", exclude_defaults=not show_all)
        data.update(quality=result.quality, media_type=result.media_type)
        print(json.dumps(data, ensure_ascii=False))
    return 0


def _has_admin_password(settings: Any) -> bool:
    import asyncio

    from fetchbox import migrations
    from fetchbox.core.db import Database
    from fetchbox.core.events import EventBus
    from fetchbox.modules.auth.service import AuthService

    async def check() -> bool:
        db = Database(settings.database_url)
        try:
            return await AuthService(db, EventBus()).has_password()
        finally:
            await db.dispose()

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    migrations.upgrade(settings.database_url)
    return asyncio.run(check())


def _initial_password() -> str | None:
    """FETCHBOX_ADMIN_PASSWORD, or the contents of FETCHBOX_ADMIN_PASSWORD_FILE (Docker
    secrets); used only while no password is set."""
    path = os.environ.get("FETCHBOX_ADMIN_PASSWORD_FILE")
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").rstrip("\r\n") or None
        except OSError as exc:
            print(f"error: can't read FETCHBOX_ADMIN_PASSWORD_FILE: {exc}", file=sys.stderr)
            return None
    return os.environ.get("FETCHBOX_ADMIN_PASSWORD") or None


def _set_password(settings: Any, password: str) -> int:
    import asyncio

    from fetchbox import migrations
    from fetchbox.core.db import Database
    from fetchbox.core.errors import ProblemError
    from fetchbox.core.events import EventBus
    from fetchbox.modules.auth.service import AuthService

    async def run() -> None:
        db = Database(settings.database_url)
        try:
            await AuthService(db, EventBus()).set_password(password)
        finally:
            await db.dispose()

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    migrations.upgrade(settings.database_url)
    try:
        asyncio.run(run())
    except ProblemError as exc:
        print(f"error: FETCHBOX_ADMIN_PASSWORD: {exc.problem.detail}", file=sys.stderr)
        return 2
    return 0


def _reset_password(settings: Any, username: str | None, from_stdin: bool) -> int:
    import asyncio
    import getpass

    from fetchbox import migrations
    from fetchbox.core.db import Database
    from fetchbox.core.errors import ProblemError
    from fetchbox.core.events import EventBus
    from fetchbox.modules.auth.service import AuthService

    if from_stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass("New admin password: ")
        if getpass.getpass("Repeat: ") != password:
            print("error: passwords do not match", file=sys.stderr)
            return 2

    async def run() -> str:
        db = Database(settings.database_url)
        try:
            user = await AuthService(db, EventBus()).set_password(password, username=username)
            return user.username
        finally:
            await db.dispose()

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    migrations.upgrade(settings.database_url)
    try:
        name = asyncio.run(run())
    except ProblemError as exc:
        msgs = "; ".join(e.message for e in exc.problem.errors or []) or exc.problem.detail
        print(f"error: {msgs}", file=sys.stderr)
        return 2
    print(f"password updated for {name!r}; existing sessions signed out")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "parse":
        return _parse_names(args.names, show_all=args.all)
    if args.command == "openapi":
        return _openapi()
    # Imports deferred so `fetchbox --version` stays fast.
    from fetchbox.config import load_settings
    from fetchbox.core.logging import configure_logging

    skip = {"command", "data_dir", "revision", "username", "password_stdin", "action", "out",
            "search", "limit", "with_key", "file"}  # fmt: skip
    cli = {k: v for k, v in vars(args).items() if k not in skip}
    settings = load_settings(args.data_dir, **cli)
    configure_logging(settings.log_level, settings.log_format, settings.logs_dir)

    if args.command == "backup":
        import getpass

        from fetchbox.modules.system.backups import BackupError, Backups

        password = getpass.getpass("Password for the secrets key: ") if args.with_key else None
        try:
            b = Backups(settings.data_dir, settings.database_url).create("manual", password)
        except BackupError as exc:
            print(f"error: {exc.message}", file=sys.stderr)
            return 2
        print(f"backup written: {b.path}")
        return 0

    if args.command == "restore":
        import getpass
        import shutil

        from fetchbox import migrations
        from fetchbox.modules.system.backups import BACKUP_DIR, BackupError, Backups

        svc = Backups(settings.data_dir, settings.database_url)
        src = Path(args.file)
        try:
            info = svc.info(src)
            kept = svc.dir / src.name
            if src.resolve() != kept.resolve():
                svc.dir.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, kept)
            password = (
                getpass.getpass("The backup's key password: ")
                if info.includes_secrets_key
                else None
            )
            svc.stage_restore(kept.stem, password)
            svc.apply_pending()
        except BackupError as exc:
            print(f"error: {exc.message}", file=sys.stderr)
            return 2
        migrations.upgrade(settings.database_url)
        print(f"restored {src.name} (the previous data is in {BACKUP_DIR}/, as before-restore)")
        return 0

    if args.command == "corpus":
        import asyncio

        from fetchbox.corpus import capture

        n = asyncio.run(capture(settings, Path(args.out), args.search, args.limit))
        print(f"{n} new release names written to {args.out}")
        return 0

    if args.command == "tray":
        from fetchbox import tray

        return tray.run(settings)

    if args.command == "serve":
        import uvicorn

        from fetchbox.app import create_app

        initial = _initial_password()
        if initial and not _has_admin_password(settings):
            # First start with FETCHBOX_ADMIN_PASSWORD(_FILE): Docker and other headless
            # installs get a password before they listen beyond localhost.
            code = _set_password(settings, initial)
            if code:
                return code
            print("admin password set from FETCHBOX_ADMIN_PASSWORD", file=sys.stderr)

        fastapi_app = create_app(settings)
        config = uvicorn.Config(
            fastapi_app,
            host=settings.host,
            port=settings.port,
            log_config=None,
            timeout_graceful_shutdown=30,
            lifespan="on",
        )
        server = uvicorn.Server(config)

        def request_shutdown() -> None:
            server.should_exit = True

        fastapi_app.state.request_shutdown = request_shutdown
        try:
            server.run()
        except SystemExit as exc:  # uvicorn exits(3) when lifespan startup fails
            return int(exc.code) if isinstance(exc.code, int) else 2
        return 0

    if args.command == "reset-password":
        return _reset_password(settings, args.username, args.password_stdin)

    if args.command == "migrate":
        from fetchbox import migrations

        settings.data_dir.mkdir(parents=True, exist_ok=True)
        migrations.upgrade(settings.database_url, args.revision)
        target = migrations.head_revision() if args.revision == "head" else args.revision
        print(f"database at {target}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
