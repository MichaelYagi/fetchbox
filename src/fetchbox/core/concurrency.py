"""Optimistic concurrency: ETag / If-Match backed by a row version column.

Tables that clients edit mix in ``VersionMixin``. SQLAlchemy then issues
``UPDATE ... WHERE id = :id AND version = :seen`` and bumps the version, raising
``StaleDataError`` if someone else changed the row first. The error handler maps that
to 412, so a check-then-write race can never silently overwrite.

Flow for PATCH/PUT/DELETE: load the row, ``check_if_match(row, header)``, mutate,
commit. The ETag is ``"v<version>"``; ``If-Match`` is optional (absent = last write wins).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Integer
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from fetchbox.core.errors import ProblemError


class VersionMixin:
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    @declared_attr.directive
    def __mapper_args__(cls) -> dict[str, Any]:
        return {"version_id_col": cls.version}


def etag(version: int) -> str:
    return f'"v{version}"'


def etag_of(obj: VersionMixin) -> str:
    return etag(obj.version)


class PreconditionFailed(ProblemError):
    def __init__(self, detail: str = "The resource changed since you last read it") -> None:
        super().__init__(412, "Precondition Failed", detail, slug="precondition-failed")


def _tags(header: str) -> set[str]:
    return {t.strip().removeprefix("W/") for t in header.split(",") if t.strip()}


def check_if_match(obj: VersionMixin | None, if_match: str | None) -> None:
    """Raise 412 unless `If-Match` is absent, `*` (with an existing row), or the row's ETag."""
    if if_match is None:
        return
    tags = _tags(if_match)
    if "*" in tags:
        if obj is None:
            raise PreconditionFailed("If-Match: * but the resource does not exist")
        return
    if obj is None or etag_of(obj) not in tags:
        raise PreconditionFailed()
