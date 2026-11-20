"""Import every module's ORM models so Alembic sees the full metadata."""

from __future__ import annotations

import importlib
import pkgutil


def load_all_models() -> None:
    import fetchbox.core.jobs  # noqa: F401  (core-owned tables)
    import fetchbox.modules as modules

    for info in pkgutil.iter_modules(modules.__path__):
        for mod in ("models", "activity"):
            name = f"fetchbox.modules.{info.name}.{mod}"
            try:
                importlib.import_module(name)
            except ModuleNotFoundError as exc:
                if exc.name != name:
                    raise
