"""Users, API keys and UI sessions.

API keys look like ``fb_<prefix>_<secret>``: the 8-char prefix is a non-secret lookup id,
the secret is 32 random bytes stored only as an argon2id hash. Because argon2 is slow on
purpose, successful verifications are cached in memory (keyed by SHA-256 of the full key)
for a short TTL; any key mutation clears the cache.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import hmac
import secrets
import string
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

import structlog
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import delete, func, select, update

from fetchbox.core.concurrency import check_if_match
from fetchbox.core.db import Database, utcnow
from fetchbox.core.errors import FieldError, NotFoundError, ProblemError
from fetchbox.core.events import Actor, EventBus, Subject
from fetchbox.core.ids import new_id
from fetchbox.modules.auth import scopes as sc
from fetchbox.modules.auth.models import ApiKey, AppUser, AuthSession

log = structlog.get_logger(__name__)

KEY_PREFIX = "fb"
PREFIX_LEN = 8
_PREFIX_ALPHABET = string.ascii_lowercase + string.digits
ADMIN_USERNAME = "admin"
DEFAULT_KEY_NAME = "default"
MIN_PASSWORD_LEN = 8

SESSION_TTL = timedelta(days=14)
SESSION_TOUCH_EVERY = timedelta(minutes=5)
KEY_TOUCH_EVERY = timedelta(seconds=60)
KEY_CACHE_TTL_S = 300.0

STREAM_TOKEN_TTL_S = 60.0

LOGIN_MAX_FAILURES = 10
LOGIN_WINDOW_S = 15 * 60

_hasher = PasswordHasher()  # argon2id with library defaults


@dataclass(frozen=True)
class Principal:
    type: Literal["user", "api_key"]
    id: str
    name: str
    scopes: frozenset[str]
    via: Literal["session", "api_key", "stream_token"]
    csrf_token: str | None = None
    ip: str | None = None

    def actor(self) -> Actor:
        return Actor(type=self.type, id=self.id, name=self.name, ip=self.ip)

    def has(self, scope: str) -> bool:
        return sc.has_scope(self.scopes, scope)


@dataclass
class _CachedKey:
    principal: Principal
    expires_at: datetime | None
    cached_at: float


@dataclass
class LoginLimiter:
    """At most LOGIN_MAX_FAILURES failed logins per LOGIN_WINDOW_S per IP."""

    max_failures: int = LOGIN_MAX_FAILURES
    window_s: float = LOGIN_WINDOW_S
    _fails: dict[str, deque[float]] = field(default_factory=lambda: defaultdict(deque))

    def _prune(self, ip: str, now: float) -> deque[float]:
        q = self._fails[ip]
        while q and now - q[0] > self.window_s:
            q.popleft()
        return q

    def retry_after(self, ip: str) -> int | None:
        now = time.monotonic()
        q = self._prune(ip, now)
        if len(q) >= self.max_failures:
            return max(int(self.window_s - (now - q[0])) + 1, 1)
        return None

    def failed(self, ip: str) -> None:
        self._fails[ip].append(time.monotonic())

    def succeeded(self, ip: str) -> None:
        self._fails.pop(ip, None)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _new_prefix() -> str:
    return "".join(secrets.choice(_PREFIX_ALPHABET) for _ in range(PREFIX_LEN))


def format_key(prefix: str, secret: str) -> str:
    return f"{KEY_PREFIX}_{prefix}_{secret}"


def parse_key(raw: str) -> tuple[str, str] | None:
    parts = raw.split("_", 2)
    if len(parts) != 3 or parts[0] != KEY_PREFIX or len(parts[1]) != PREFIX_LEN or not parts[2]:
        return None
    return parts[1], parts[2]


async def hash_secret(secret: str) -> str:
    return await asyncio.to_thread(_hasher.hash, secret)


async def verify_secret(stored_hash: str, secret: str) -> bool:
    try:
        return await asyncio.to_thread(_hasher.verify, stored_hash, secret)
    except (VerificationError, InvalidHashError):
        return False


def unauthorized(detail: str = "Missing or invalid credentials") -> ProblemError:
    return ProblemError(
        401, "Unauthorized", detail, slug="unauthorized", headers={"WWW-Authenticate": "Bearer"}
    )


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LEN:
        raise ProblemError(
            422,
            "Validation Failed",
            slug="validation",
            errors=[FieldError(field="password", message=f"min {MIN_PASSWORD_LEN} characters")],
        )


class AuthService:
    def __init__(self, db: Database, bus: EventBus) -> None:
        self.db = db
        self.bus = bus
        self.limiter = LoginLimiter()
        self._key_cache: dict[str, _CachedKey] = {}
        self._stream_tokens: dict[str, tuple[Principal, float]] = {}
        # Long-lived refs so `last_used_at` background writes aren't garbage-collected.
        self._bg: set[asyncio.Task[None]] = set()

    # ---- bootstrap / admin user ----------------------------------------------------

    async def bootstrap(self) -> str | None:
        """First run: create the admin user (no password yet) and the `default` key with `*`.

        Returns the full default key exactly once, or None if already bootstrapped.
        """
        async with self.db.sessionmaker() as s:
            if (await s.execute(select(func.count()).select_from(AppUser))).scalar_one():
                return None
            user = AppUser(id=new_id(), username=ADMIN_USERNAME, created_at=utcnow())
            s.add(user)
            key, full = await self._new_key(DEFAULT_KEY_NAME, [sc.ALL], created_by=user.id)
            s.add(key)
            await s.commit()
        log.info("auth_bootstrapped", admin=ADMIN_USERNAME, key_prefix=key.prefix)
        return full

    async def admin(self) -> AppUser | None:
        async with self.db.sessionmaker() as s:
            return (await s.execute(select(AppUser).order_by(AppUser.id).limit(1))).scalar()

    async def has_password(self) -> bool:
        user = await self.admin()
        return bool(user and user.password_hash)

    async def set_password(self, password: str, *, username: str | None = None) -> AppUser:
        validate_password(password)
        pw_hash = await hash_secret(password)
        async with self.db.sessionmaker() as s:
            user = (await s.execute(select(AppUser).order_by(AppUser.id).limit(1))).scalar()
            if user is None:
                user = AppUser(
                    id=new_id(), username=username or ADMIN_USERNAME, created_at=utcnow()
                )
                s.add(user)
            if username:
                user.username = username
            user.password_hash = pw_hash
            # Changing the password signs out every existing session.
            await s.execute(delete(AuthSession).where(AuthSession.user_id == user.id))
            await s.commit()
        # Open event streams of this user listen for this and disconnect.
        await self.bus.emit(
            "auth.password_changed",
            subject=Subject(type="user", id=user.id),
            username=user.username,
        )
        return user

    # ---- sessions ------------------------------------------------------------------

    async def login(
        self, username: str, password: str, *, ip: str, user_agent: str | None
    ) -> tuple[str, str]:
        """Returns (session_token, csrf_token)."""
        if (wait := self.limiter.retry_after(ip)) is not None:
            raise ProblemError(
                429,
                "Too Many Requests",
                "Too many failed logins; try later",
                slug="rate-limited",
                headers={"Retry-After": str(wait)},
            )
        async with self.db.sessionmaker() as s:
            user = (await s.execute(select(AppUser).where(AppUser.username == username))).scalar()
        ok = bool(user and user.password_hash and await verify_secret(user.password_hash, password))
        if not ok or user is None:
            if user is None or not user.password_hash:
                await hash_secret(password)  # equalize timing with the real-user path
            self.limiter.failed(ip)
            raise unauthorized("Invalid username or password")
        self.limiter.succeeded(ip)
        return await self.create_session(user, ip=ip, user_agent=user_agent)

    async def create_session(
        self, user: AppUser, *, ip: str | None, user_agent: str | None
    ) -> tuple[str, str]:
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        now = utcnow()
        async with self.db.sessionmaker() as s:
            s.add(
                AuthSession(
                    id=new_id(),
                    user_id=user.id,
                    token_hash=_sha256(token),
                    csrf_token=csrf,
                    created_at=now,
                    expires_at=now + SESSION_TTL,
                    last_seen_at=now,
                    ip=ip,
                    user_agent=user_agent,
                )
            )
            await s.commit()
        return token, csrf

    async def logout(self, token: str) -> None:
        async with self.db.sessionmaker() as s:
            await s.execute(delete(AuthSession).where(AuthSession.token_hash == _sha256(token)))
            await s.commit()

    async def resolve_session(self, token: str) -> Principal | None:
        now = utcnow()
        async with self.db.sessionmaker() as s:
            row = (
                await s.execute(
                    select(AuthSession, AppUser)
                    .join(AppUser, AppUser.id == AuthSession.user_id)
                    .where(AuthSession.token_hash == _sha256(token))
                )
            ).first()
            if row is None:
                return None
            sess, user = row
            if sess.expires_at <= now:
                await s.delete(sess)
                await s.commit()
                return None
            if now - sess.last_seen_at >= SESSION_TOUCH_EVERY:  # sliding expiry
                sess.last_seen_at, sess.expires_at = now, now + SESSION_TTL
                await s.commit()
        # Single-admin v1: the user holds every scope.
        return Principal(
            type="user",
            id=user.id,
            name=user.username,
            scopes=frozenset({sc.ALL}),
            via="session",
            csrf_token=sess.csrf_token,
        )

    # ---- event-stream tokens ----------------------------------------------------------

    def issue_stream_token(self, principal: Principal) -> tuple[str, int]:
        """Short-lived, single-use token for `?token=` on the event stream.

        Browsers can't set headers on WebSocket/EventSource, and API keys must never go in
        URLs, so the client trades its real credential for one of these first.
        """
        now = time.monotonic()
        for t, (_, exp) in list(self._stream_tokens.items()):
            if exp <= now:
                del self._stream_tokens[t]
        token = secrets.token_urlsafe(32)
        self._stream_tokens[_sha256(token)] = (principal, now + STREAM_TOKEN_TTL_S)
        return token, int(STREAM_TOKEN_TTL_S)

    def redeem_stream_token(self, token: str) -> Principal | None:
        entry = self._stream_tokens.pop(_sha256(token), None)
        if entry is None or entry[1] <= time.monotonic():
            return None
        return dataclasses.replace(entry[0], via="stream_token", csrf_token=None)

    # ---- API keys: authentication -----------------------------------------------------

    async def resolve_api_key(self, raw: str, *, ip: str | None) -> Principal | None:
        parsed = parse_key(raw)
        if parsed is None:
            return None
        cache_id = _sha256(raw)
        now = utcnow()
        hit = self._key_cache.get(cache_id)
        if hit and time.monotonic() - hit.cached_at < KEY_CACHE_TTL_S:
            if hit.expires_at is not None and hit.expires_at <= now:
                self._key_cache.pop(cache_id, None)
                return None
            self._touch_key_later(hit.principal.id, ip)
            return hit.principal

        prefix, secret = parsed
        async with self.db.sessionmaker() as s:
            key = (await s.execute(select(ApiKey).where(ApiKey.prefix == prefix))).scalar()
        if key is None or key.revoked_at is not None:
            return None
        if key.expires_at is not None and key.expires_at <= now:
            return None
        if not await verify_secret(key.key_hash, secret):
            return None
        principal = Principal(
            type="api_key", id=key.id, name=key.name, scopes=frozenset(key.scopes), via="api_key"
        )
        self._key_cache[cache_id] = _CachedKey(principal, key.expires_at, time.monotonic())
        self._touch_key_later(key.id, ip, force=True)
        return principal

    def _touch_key_later(self, key_id: str, ip: str | None, *, force: bool = False) -> None:
        task = asyncio.create_task(self._touch_key(key_id, ip, force=force))
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def _touch_key(self, key_id: str, ip: str | None, *, force: bool) -> None:
        """Record last use, at most once per KEY_TOUCH_EVERY per key, to limit writes."""
        now = utcnow()
        try:
            async with self.db.sessionmaker() as s:
                q = update(ApiKey).where(ApiKey.id == key_id)
                if not force:
                    q = q.where(
                        (ApiKey.last_used_at.is_(None))
                        | (ApiKey.last_used_at < now - KEY_TOUCH_EVERY)
                    )
                await s.execute(q.values(last_used_at=now, last_used_ip=ip))
                await s.commit()
        except Exception:
            log.warning("api_key_touch_failed", key_id=key_id, exc_info=True)

    async def drain(self) -> None:
        if self._bg:
            await asyncio.gather(*self._bg, return_exceptions=True)

    # ---- API keys: management ---------------------------------------------------------

    async def _new_key(
        self,
        name: str,
        scopes: list[str],
        *,
        created_by: str | None,
        expires_at: datetime | None = None,
    ) -> tuple[ApiKey, str]:
        secret = secrets.token_urlsafe(32)
        prefix = _new_prefix()
        key = ApiKey(
            id=new_id(),
            name=name,
            prefix=prefix,
            key_hash=await hash_secret(secret),
            scopes=sc.normalize(scopes),
            created_by=created_by,
            created_at=utcnow(),
            expires_at=expires_at,
        )
        return key, format_key(prefix, secret)

    def _check_grant(self, actor: Principal, requested: list[str]) -> None:
        unknown = sc.unknown_scopes(requested)
        if unknown:
            raise ProblemError(
                422,
                "Validation Failed",
                slug="validation",
                errors=[
                    FieldError(field="scopes", message=f"unknown scope {u!r}") for u in unknown
                ],
            )
        if not requested:
            raise ProblemError(
                422,
                "Validation Failed",
                slug="validation",
                errors=[FieldError(field="scopes", message="at least one scope")],
            )
        missing = sc.can_grant(actor.scopes, requested)
        if missing:
            raise ProblemError(
                403,
                "Forbidden",
                f"cannot grant scopes you don't hold: {', '.join(missing)}",
                slug="scope-escalation",
            )

    async def list_keys(self) -> list[ApiKey]:
        async with self.db.sessionmaker() as s:
            return list((await s.execute(select(ApiKey).order_by(ApiKey.id))).scalars())

    async def get_key(self, key_id: str) -> ApiKey:
        async with self.db.sessionmaker() as s:
            key = await s.get(ApiKey, key_id)
        if key is None:
            raise NotFoundError("api key", key_id)
        return key

    async def create_key(
        self, actor: Principal, *, name: str, scopes: list[str], expires_at: datetime | None
    ) -> tuple[ApiKey, str]:
        self._check_grant(actor, scopes)
        if expires_at is not None and expires_at <= utcnow():
            raise ProblemError(
                422,
                "Validation Failed",
                slug="validation",
                errors=[FieldError(field="expires_at", message="must be in the future")],
            )
        key, full = await self._new_key(name, scopes, created_by=actor.id, expires_at=expires_at)
        async with self.db.sessionmaker() as s:
            s.add(key)
            await s.commit()
        await self._audit("auth.key_created", actor, key, scopes=key.scopes)
        return key, full

    async def update_key(
        self,
        actor: Principal,
        key_id: str,
        *,
        name: str | None = None,
        scopes: list[str] | None = None,
        expires_at: datetime | None = None,
        clear_expiry: bool = False,
        if_match: str | None = None,
    ) -> ApiKey:
        if scopes is not None:
            self._check_grant(actor, scopes)
        async with self.db.sessionmaker() as s:
            key = await s.get(ApiKey, key_id)
            if key is None:
                raise NotFoundError("api key", key_id)
            check_if_match(key, if_match)
            before = list(key.scopes)
            if name is not None:
                key.name = name
            if scopes is not None:
                key.scopes = sc.normalize(scopes)
            if clear_expiry:
                key.expires_at = None
            elif expires_at is not None:
                key.expires_at = expires_at
            await s.commit()
        self._key_cache.clear()
        if scopes is not None and before != key.scopes:
            await self._audit(
                "auth.key_scopes_changed", actor, key, before=before, after=key.scopes
            )
        return key

    async def rotate_key(
        self, actor: Principal, key_id: str, *, if_match: str | None = None
    ) -> tuple[ApiKey, str]:
        """New secret and prefix; same id, name and scopes."""
        async with self.db.sessionmaker() as s:
            key = await s.get(ApiKey, key_id)
            if key is None:
                raise NotFoundError("api key", key_id)
            check_if_match(key, if_match)
            if key.revoked_at is not None:
                raise ProblemError(
                    409, "Conflict", "revoked keys cannot be rotated", slug="conflict"
                )
            secret = secrets.token_urlsafe(32)
            key.prefix = _new_prefix()
            key.key_hash = await hash_secret(secret)
            await s.commit()
        self._key_cache.clear()
        await self._audit("auth.key_rotated", actor, key)
        return key, format_key(key.prefix, secret)

    async def revoke_key(
        self, actor: Principal, key_id: str, *, if_match: str | None = None
    ) -> ApiKey:
        async with self.db.sessionmaker() as s:
            key = await s.get(ApiKey, key_id)
            if key is None:
                raise NotFoundError("api key", key_id)
            check_if_match(key, if_match)
            if key.revoked_at is None:
                key.revoked_at = utcnow()
                await s.commit()
        self._key_cache.clear()
        await self._audit("auth.key_revoked", actor, key)
        return key

    async def delete_key(
        self, actor: Principal, key_id: str, *, if_match: str | None = None
    ) -> None:
        async with self.db.sessionmaker() as s:
            key = await s.get(ApiKey, key_id)
            if key is None:
                raise NotFoundError("api key", key_id)
            check_if_match(key, if_match)
            await s.delete(key)  # versioned DELETE: concurrent edit -> 412
            await s.commit()
        self._key_cache.clear()
        await self._audit("auth.key_deleted", actor, key)

    async def _audit(self, event: str, actor: Principal, key: ApiKey, **data: object) -> None:
        await self.bus.emit(
            event,
            subject=Subject(type="api_key", id=key.id),
            actor=actor.actor(),
            name=key.name,
            prefix=key.prefix,
            **data,
        )


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
