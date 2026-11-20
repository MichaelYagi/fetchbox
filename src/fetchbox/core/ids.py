"""ULID identifiers: sortable, URL-safe, merge-friendly for config import."""

from __future__ import annotations

from ulid import ULID


def new_id() -> str:
    return str(ULID())


def is_id(value: str) -> bool:
    try:
        ULID.from_str(value)
    except ValueError:
        return False
    return True
