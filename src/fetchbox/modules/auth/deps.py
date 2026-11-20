"""Route authorization.

Every API route must depend on exactly one of:

- ``require("resource:action")``: authenticated principal holding that scope
- ``authenticated()``: any authenticated principal
- ``public()``: no credentials (login, first-run setup, liveness ping)

``tests/test_route_scopes.py`` walks the OpenAPI routes and fails if one has none.

Credentials, in order: ``Authorization: Bearer fb_...``, ``X-Api-Key: fb_...``, then the
``fb_session`` cookie. Cookie-authenticated unsafe requests must echo the session's CSRF
token in ``X-CSRF-Token``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import APIKeyCookie, APIKeyHeader, HTTPBearer

from fetchbox.core.errors import ProblemError
from fetchbox.modules.auth.service import (
    AuthService,
    Principal,
    constant_time_equals,
    unauthorized,
)

SESSION_COOKIE = "fb_session"
CSRF_COOKIE = "fb_csrf"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Declared so OpenAPI documents the schemes; resolution is done by hand below.
_bearer = HTTPBearer(auto_error=False, description="API key: `fb_<prefix>_<secret>`")
_x_api_key = APIKeyHeader(name="X-Api-Key", auto_error=False)
_cookie = APIKeyCookie(name=SESSION_COOKIE, auto_error=False)


def get_auth(request: Request) -> AuthService:
    svc: AuthService = request.app.state.auth
    return svc


def client_ip(request: Request) -> str:
    # TODO(phase 0): honour X-Forwarded-For only from configured trusted_proxies.
    return request.client.host if request.client else "unknown"


async def _resolve(request: Request) -> Principal | None:
    auth = get_auth(request)
    ip = client_ip(request)
    header = request.headers.get("authorization", "")
    raw: str | None = None
    if header.lower().startswith("bearer "):
        raw = header[7:].strip()
    elif "x-api-key" in request.headers:
        raw = request.headers["x-api-key"].strip()
    if raw is not None:
        principal = await auth.resolve_api_key(raw, ip=ip)
        if principal is None:
            raise unauthorized("Invalid, expired or revoked API key")
        return principal

    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    principal = await auth.resolve_session(token)
    if principal is None:
        raise unauthorized("Session expired; sign in again")
    if request.method not in SAFE_METHODS:
        sent = request.headers.get(CSRF_HEADER, "")
        if not principal.csrf_token or not constant_time_equals(sent, principal.csrf_token):
            raise ProblemError(403, "Forbidden", "Missing or invalid CSRF token", slug="csrf")
    return principal


class Require:
    """Dependency marker + resolver. `scope=None` means public; `""` means any principal."""

    def __init__(self, scope: str | None) -> None:
        self.scope = scope

    def __repr__(self) -> str:
        return f"Require({self.scope!r})"

    async def __call__(
        self,
        request: Request,
        _b: Annotated[object, Depends(_bearer)] = None,
        _k: Annotated[object, Depends(_x_api_key)] = None,
        _c: Annotated[object, Depends(_cookie)] = None,
    ) -> Principal | None:
        if self.scope is None:
            return None
        principal = await _resolve(request)
        if principal is None:
            raise unauthorized()
        if self.scope and not principal.has(self.scope):
            raise ProblemError(
                403, "Forbidden", f"requires scope {self.scope!r}", slug="missing-scope"
            )
        request.state.principal = principal
        return principal


def require(scope: str) -> Require:
    if not scope:
        raise ValueError("use authenticated() for scope-less routes")
    return Require(scope)


def authenticated() -> Require:
    return Require("")


def public() -> Require:
    return Require(None)


def principal_of(request: Request) -> Principal:
    p: Principal | None = getattr(request.state, "principal", None)
    if p is None:
        raise unauthorized()
    return p


CurrentPrincipal = Annotated[Principal, Depends(principal_of)]
