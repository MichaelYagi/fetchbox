from __future__ import annotations

from typing import Any

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from fetchbox.core.concurrency import check_if_match
from fetchbox.core.db import Database
from fetchbox.core.errors import ConflictError, FieldError, NotFoundError, ProblemError
from fetchbox.core.events import Actor, EventBus, Subject
from fetchbox.modules.profiles import defaults
from fetchbox.modules.profiles.formats import Condition, FormatSpec
from fetchbox.modules.profiles.models import CustomFormat, QualityProfile
from fetchbox.modules.profiles.profile import ProfileSpec

_CONDITIONS: TypeAdapter[list[Condition]] = TypeAdapter(list[Condition])
PROFILE_FIELDS = tuple(ProfileSpec.model_fields)


def invalid(exc: ValidationError, prefix: str = "body") -> ProblemError:
    errors = [
        FieldError(
            field=".".join(str(p) for p in (prefix, *e["loc"]) if p != "__root__"),
            message=e["msg"].removeprefix("Value error, "),
        )
        for e in exc.errors()
    ]
    return ProblemError(422, "Validation Failed", slug="validation", errors=errors)


def to_spec(row: QualityProfile) -> ProfileSpec:
    return ProfileSpec.model_validate({f: getattr(row, f) for f in PROFILE_FIELDS})


def to_format_spec(row: CustomFormat) -> FormatSpec:
    return FormatSpec(
        id=row.id,
        name=row.name,
        conditions=tuple(_CONDITIONS.validate_python(row.conditions)),
        default_score=row.default_score,
    )


def _apply(row: QualityProfile, spec: ProfileSpec) -> None:
    data = spec.model_dump(mode="json")
    for f in PROFILE_FIELDS:
        setattr(row, f, data[f])


OLD_DEFAULTS = {"Standard HD", "4K"}
NEW_DEFAULTS = ("1080p", "2160p", "720p", "480p")


class ProfilesService:
    def __init__(self, db: Database, bus: EventBus) -> None:
        self.db = db
        self.bus = bus

    # ---- seeding -----------------------------------------------------------------------

    async def seed_defaults(self) -> None:
        """Create the built-in formats and profiles while their tables are empty."""
        async with self.db.sessionmaker() as s:
            ids: dict[str, str] = {}
            if not await s.scalar(select(func.count()).select_from(CustomFormat)):
                for slug, f in defaults.FORMATS.items():
                    row = CustomFormat(**f)
                    s.add(row)
                    await s.flush()
                    ids[slug] = row.id
            else:
                by_name = {f["name"]: slug for slug, f in defaults.FORMATS.items()}
                for row in (await s.execute(select(CustomFormat))).scalars():
                    if row.name in by_name:
                        ids[by_name[row.name]] = row.id
            names = set((await s.execute(select(QualityProfile.name))).scalars())
            # Databases from before the resolution-named defaults (Standard HD, 4K) get
            # them once, next to the old ones (titles may use those); a later deletion of a
            # new one isn't undone, since this only runs while none of them exist.
            upgrade = bool(names & OLD_DEFAULTS) and not names & set(NEW_DEFAULTS)
            # Music profiles, while there's none (also on databases from before music).
            has_music = await s.scalar(
                select(func.count())
                .select_from(QualityProfile)
                .where(QualityProfile.media == "music")
            )
            if not has_music:
                for p in defaults.MUSIC_PROFILES:
                    if p["name"] in names:
                        continue
                    profile = QualityProfile()
                    _apply(profile, ProfileSpec.model_validate(p))
                    s.add(profile)
            if not names or upgrade:
                for p in defaults.PROFILES:
                    if p["name"] in names:
                        continue
                    scores = {ids[k]: v for k, v in p["format_scores"].items() if k in ids}
                    spec = ProfileSpec.model_validate({**p, "format_scores": scores})
                    profile = QualityProfile()
                    _apply(profile, spec)
                    s.add(profile)
            await s.commit()

    # ---- profiles ----------------------------------------------------------------------

    async def list_profiles(self) -> list[QualityProfile]:
        async with self.db.sessionmaker() as s:
            return list(
                (await s.execute(select(QualityProfile).order_by(QualityProfile.name))).scalars()
            )

    async def get_profile(self, profile_id: str) -> QualityProfile:
        async with self.db.sessionmaker() as s:
            return await self._profile(s, profile_id)

    async def _profile(self, s: AsyncSession, profile_id: str) -> QualityProfile:
        row = await s.get(QualityProfile, profile_id)
        if row is None:
            raise NotFoundError("quality profile", profile_id)
        return row

    async def _check_format_ids(self, s: AsyncSession, scores: dict[str, int]) -> None:
        if not scores:
            return
        found = set(
            (await s.execute(select(CustomFormat.id).where(CustomFormat.id.in_(scores)))).scalars()
        )
        missing = sorted(set(scores) - found)
        if missing:
            raise ProblemError(
                422,
                "Validation Failed",
                slug="validation",
                errors=[
                    FieldError(field=f"body.format_scores.{m}", message="no such custom format")
                    for m in missing
                ],
            )

    async def create_profile(self, spec: ProfileSpec, actor: Actor) -> QualityProfile:
        async with self.db.sessionmaker() as s:
            await self._check_format_ids(s, spec.format_scores)
            row = QualityProfile()
            _apply(row, spec)
            s.add(row)
            await self._commit(s, "quality profile", spec.name)
        await self._emit("profiles.profile_created", row, actor)
        return row

    async def update_profile(
        self, profile_id: str, changes: dict[str, Any], *, if_match: str | None, actor: Actor
    ) -> QualityProfile:
        async with self.db.sessionmaker() as s:
            row = await self._profile(s, profile_id)
            check_if_match(row, if_match)
            merged = {**to_spec(row).model_dump(mode="json"), **changes}
            try:
                spec = ProfileSpec.model_validate(merged)
            except ValidationError as exc:
                raise invalid(exc) from None
            await self._check_format_ids(s, spec.format_scores)
            _apply(row, spec)
            await self._commit(s, "quality profile", spec.name)
        await self._emit("profiles.profile_updated", row, actor, fields=sorted(changes))
        return row

    async def delete_profile(self, profile_id: str, *, if_match: str | None, actor: Actor) -> None:
        async with self.db.sessionmaker() as s:
            row = await self._profile(s, profile_id)
            check_if_match(row, if_match)
            name = row.name  # attributes expire on rollback
            await s.delete(row)
            try:
                await s.commit()
            except IntegrityError:  # title.profile_id references it
                await s.rollback()
                raise ConflictError(
                    f"quality profile {name!r} is used by one or more titles; "
                    "move them to another profile first"
                ) from None
        await self._emit("profiles.profile_deleted", row, actor)

    # ---- custom formats ----------------------------------------------------------------

    async def list_formats(self) -> list[CustomFormat]:
        async with self.db.sessionmaker() as s:
            return list(
                (await s.execute(select(CustomFormat).order_by(CustomFormat.name))).scalars()
            )

    async def format_specs(self) -> list[FormatSpec]:
        return [to_format_spec(r) for r in await self.list_formats()]

    async def get_format(self, format_id: str) -> CustomFormat:
        async with self.db.sessionmaker() as s:
            return await self._format(s, format_id)

    async def _format(self, s: AsyncSession, format_id: str) -> CustomFormat:
        row = await s.get(CustomFormat, format_id)
        if row is None:
            raise NotFoundError("custom format", format_id)
        return row

    async def create_format(
        self, *, name: str, conditions: list[dict[str, Any]], default_score: int, actor: Actor
    ) -> CustomFormat:
        async with self.db.sessionmaker() as s:
            row = CustomFormat(name=name, conditions=conditions, default_score=default_score)
            s.add(row)
            await self._commit(s, "custom format", name)
        await self._emit("profiles.format_created", row, actor)
        return row

    async def update_format(
        self, format_id: str, changes: dict[str, Any], *, if_match: str | None, actor: Actor
    ) -> CustomFormat:
        async with self.db.sessionmaker() as s:
            row = await self._format(s, format_id)
            check_if_match(row, if_match)
            for k, v in changes.items():
                setattr(row, k, v)
            await self._commit(s, "custom format", row.name)
        await self._emit("profiles.format_updated", row, actor, fields=sorted(changes))
        return row

    async def delete_format(self, format_id: str, *, if_match: str | None, actor: Actor) -> None:
        """Delete a format and drop its score from every profile, in one transaction."""
        async with self.db.sessionmaker() as s:
            row = await self._format(s, format_id)
            check_if_match(row, if_match)
            for p in (await s.execute(select(QualityProfile))).scalars():
                if format_id in p.format_scores:
                    p.format_scores = {k: v for k, v in p.format_scores.items() if k != format_id}
            await s.delete(row)
            await s.commit()
        await self._emit("profiles.format_deleted", row, actor)

    # ---- helpers -----------------------------------------------------------------------

    async def _commit(self, s: AsyncSession, what: str, name: str) -> None:
        try:
            await s.commit()
        except IntegrityError:
            await s.rollback()
            raise ConflictError(f"a {what} named {name!r} already exists") from None

    async def _emit(
        self, type_: str, row: QualityProfile | CustomFormat, actor: Actor, **data: Any
    ) -> None:
        kind = "quality_profile" if isinstance(row, QualityProfile) else "custom_format"
        await self.bus.emit(
            type_, subject=Subject(type=kind, id=row.id), actor=actor, name=row.name, **data
        )
