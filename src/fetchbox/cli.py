"""`fetchbox` command-line entry point."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

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

    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # Imports deferred so `fetchbox --version` stays fast.
    from fetchbox.config import load_settings
    from fetchbox.core.logging import configure_logging

    cli = {k: v for k, v in vars(args).items() if k not in {"command", "data_dir", "revision"}}
    settings = load_settings(args.data_dir, **cli)
    configure_logging(settings.log_level, settings.log_format, settings.logs_dir)

    if args.command == "serve":
        if not settings.is_loopback:
            # TODO(phase 0, auth): allow once an admin password exists.
            print(
                "error: binding beyond localhost requires an admin password, "
                "which is not implemented yet",
                file=sys.stderr,
            )
            return 2
        import uvicorn

        from fetchbox.app import create_app

        uvicorn.run(
            create_app(settings),
            host=settings.host,
            port=settings.port,
            log_config=None,
            timeout_graceful_shutdown=30,
        )
        return 0

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
