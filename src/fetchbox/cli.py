"""`fetchbox` command-line entry point."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import Any

from fetchbox import __version__


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fetchbox", description="Self-hosted media manager")
    p.add_argument("--version", action="version", version=f"fetchbox {__version__}")
    p.add_argument("--data", dest="data_dir", help="data directory (env: FETCHBOX_DATA_DIR)")
    sub = p.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the server")
    serve.add_argument("--host", help="bind address (default 127.0.0.1)")
    serve.add_argument("--port", type=int, help="port (default 8787)")
    serve.add_argument("--db-url", help="database URL (default SQLite in the data dir)")
    serve.add_argument("--log-level", choices=["debug", "info", "warning", "error"])
    serve.add_argument("--log-format", choices=["auto", "json", "console"])

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

    skip = {"command", "data_dir", "revision", "username", "password_stdin"}
    cli = {k: v for k, v in vars(args).items() if k not in skip}
    settings = load_settings(args.data_dir, **cli)
    configure_logging(settings.log_level, settings.log_format, settings.logs_dir)

    if args.command == "serve":
        import uvicorn

        from fetchbox.app import create_app

        if not settings.is_loopback and not _has_admin_password(settings):
            print(
                f"error: refusing to bind {settings.host}: set an admin password first "
                "(fetchbox reset-password), or bind to 127.0.0.1",
                file=sys.stderr,
            )
            return 2

        config = uvicorn.Config(
            create_app(settings),
            host=settings.host,
            port=settings.port,
            log_config=None,
            timeout_graceful_shutdown=30,
            lifespan="on",
        )
        server = uvicorn.Server(config)
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
