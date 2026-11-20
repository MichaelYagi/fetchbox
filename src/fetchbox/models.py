"""Import every module's ORM models so Alembic sees the full metadata."""

from __future__ import annotations

import importlib
import pkgutil


def load_all_models() -> None:
    import fetchbox.core.jobs  # noqa: F401  (core-owned tables)
    import fetchbox.modules as modules

    for info in pkgutil.iter_modules(modules.__path__):
        try:
            importlib.import_module(f"fetchbox.modules.{info.name}.models")
        except ModuleNotFoundError as exc:
            if exc.name != f"fetchbox.modules.{info.name}.models":
                raise
