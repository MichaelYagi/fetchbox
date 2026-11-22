from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, Response, status
from pydantic import BaseModel, Field

from fetchbox.core.concurrency import etag_of
from fetchbox.core.errors import ProblemError, problem_responses
from fetchbox.modules.auth import scopes as sc
from fetchbox.modules.auth.deps import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    CurrentPrincipal,
    authenticated,
    client_ip,
    get_auth,
    public,
    require,
)
from fetchbox.modules.auth.schemas import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyOut,
    ApiKeyPatch,
    LoginIn,
    Me,
    ScopeInfo,
    SessionOut,
    SetupIn,
)
from fetchbox.modules.auth.service import SESSION_TTL, AuthService

router = APIRouter(prefix="/auth", tags=["auth"])
IfMatch = Annotated[str | None, Header(description="ETag from a previous read; 412 if stale")]
LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _set_session_cookies(request: Request, response: Response, token: str, csrf: str) -> None:
    secure = request.url.scheme == "https"
    max_age = int(SESSION_TTL.total_seconds())
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max_age,
        httponly=True,
        samesite="strict",
        secure=secure,
        path="/",
    )
    # Readable by the SPA so it can echo it in X-CSRF-Token.
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        max_age=max_age,
        httponly=False,
        samesite="strict",
        secure=secure,
        path="/",
    )


def _auth(request: Request) -> AuthService:
    return get_auth(request)


@router.get("/scopes", response_model=list[ScopeInfo], dependencies=[Depends(authenticated())])
async def list_scopes() -> list[ScopeInfo]:
    return [ScopeInfo(scope=k, description=v) for k, v in sc.CATALOG.items()]


@router.get("/me", response_model=Me, dependencies=[Depends(authenticated())])
async def me(p: CurrentPrincipal, request: Request) -> Me:
    return Me(type=p.type, id=p.id, name=p.name, scopes=sorted(p.scopes), via=p.via,
              ip=client_ip(request),
              connected_from=request.client.host if request.client else None)  # fmt: skip


class AuthStatus(BaseModel):
    setup_required: bool = Field(description="No admin password yet: show first-run setup")


@router.get("/status", response_model=AuthStatus, dependencies=[Depends(public())])
async def auth_status(request: Request) -> AuthStatus:
    """Public: lets the web UI choose between the setup and sign-in screens."""
    return AuthStatus(setup_required=not await _auth(request).has_password())


@router.post(
    "/setup",
    response_model=SessionOut,
    dependencies=[Depends(public())],
    responses=problem_responses(403, 409, 422),
)
async def setup(body: SetupIn, request: Request, response: Response) -> SessionOut:
    """First-run only: set the admin password. Allowed from localhost until a password exists."""
    auth = _auth(request)
    if await auth.has_password():
        raise ProblemError(
            409, "Conflict", "setup already completed; sign in instead", slug="setup-complete"
        )
    if client_ip(request) not in LOOPBACK:
        raise ProblemError(
            403,
            "Forbidden",
            "Set the admin password on the computer Fetchbox runs on (open it there, at "
            "http://127.0.0.1), then sign in from here",
            slug="setup-local-only",
        )
    user = await auth.set_password(body.password, username=body.username)
    token, csrf = await auth.create_session(
        user, ip=client_ip(request), user_agent=request.headers.get("user-agent")
    )
    _set_session_cookies(request, response, token, csrf)
    return SessionOut(csrf_token=csrf, user=user.username)


@router.post(
    "/login",
    response_model=SessionOut,
    dependencies=[Depends(public())],
    responses=problem_responses(401, 429),
)
async def login(body: LoginIn, request: Request, response: Response) -> SessionOut:
    token, csrf = await _auth(request).login(
        body.username,
        body.password,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    _set_session_cookies(request, response, token, csrf)
    return SessionOut(csrf_token=csrf, user=body.username)


@router.post(
    "/logout", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(authenticated())]
)
async def logout(request: Request, response: Response) -> Response:
    if token := request.cookies.get(SESSION_COOKIE):
        await _auth(request).logout(token)
    out = Response(status_code=204)
    out.delete_cookie(SESSION_COOKIE, path="/")
    out.delete_cookie(CSRF_COOKIE, path="/")
    return out


_admin = [Depends(require(sc.ADMIN))]


@router.get("/keys", response_model=list[ApiKeyOut], dependencies=_admin)
async def list_keys(request: Request) -> list[ApiKeyOut]:
    return [ApiKeyOut.model_validate(k) for k in await _auth(request).list_keys()]


@router.post(
    "/keys",
    response_model=ApiKeyCreated,
    status_code=201,
    dependencies=_admin,
    responses=problem_responses(403, 422),
)
async def create_key(body: ApiKeyCreate, request: Request, p: CurrentPrincipal) -> ApiKeyCreated:
    key, full = await _auth(request).create_key(
        p, name=body.name, scopes=body.scopes, expires_at=body.expires_at
    )
    return ApiKeyCreated(**ApiKeyOut.model_validate(key).model_dump(), key=full)


@router.get(
    "/keys/{key_id}",
    response_model=ApiKeyOut,
    dependencies=_admin,
    responses=problem_responses(404),
)
async def get_key(key_id: str, request: Request, response: Response) -> ApiKeyOut:
    key = await _auth(request).get_key(key_id)
    response.headers["ETag"] = etag_of(key)
    return ApiKeyOut.model_validate(key)


@router.patch(
    "/keys/{key_id}",
    response_model=ApiKeyOut,
    dependencies=_admin,
    responses=problem_responses(403, 404, 412, 422),
)
async def update_key(
    key_id: str,
    body: ApiKeyPatch,
    request: Request,
    response: Response,
    p: CurrentPrincipal,
    if_match: IfMatch = None,
) -> ApiKeyOut:
    key = await _auth(request).update_key(
        p,
        key_id,
        name=body.name,
        scopes=body.scopes,
        expires_at=body.expires_at,
        clear_expiry=body.clear_expiry,
        if_match=if_match,
    )
    response.headers["ETag"] = etag_of(key)
    return ApiKeyOut.model_validate(key)


@router.delete(
    "/keys/{key_id}", status_code=204, dependencies=_admin, responses=problem_responses(404, 412)
)
async def delete_key(
    key_id: str, request: Request, p: CurrentPrincipal, if_match: IfMatch = None
) -> Response:
    await _auth(request).delete_key(p, key_id, if_match=if_match)
    return Response(status_code=204)


@router.post(
    "/keys/{key_id}/rotate",
    response_model=ApiKeyCreated,
    dependencies=_admin,
    responses=problem_responses(404, 409, 412),
)
async def rotate_key(
    key_id: str, request: Request, response: Response, p: CurrentPrincipal, if_match: IfMatch = None
) -> ApiKeyCreated:
    key, full = await _auth(request).rotate_key(p, key_id, if_match=if_match)
    response.headers["ETag"] = etag_of(key)
    return ApiKeyCreated(**ApiKeyOut.model_validate(key).model_dump(), key=full)


@router.post(
    "/keys/{key_id}/revoke",
    response_model=ApiKeyOut,
    dependencies=_admin,
    responses=problem_responses(404, 412),
)
async def revoke_key(
    key_id: str, request: Request, response: Response, p: CurrentPrincipal, if_match: IfMatch = None
) -> ApiKeyOut:
    key = await _auth(request).revoke_key(p, key_id, if_match=if_match)
    response.headers["ETag"] = etag_of(key)
    return ApiKeyOut.model_validate(key)


class LoginLinkOut(BaseModel):
    path: str = Field(description="Open it within 60 s: /login?code=…, signs the admin in once")


class LoginCodeIn(BaseModel):
    code: str = Field(min_length=10, max_length=200)


@router.post(
    "/login-link",
    response_model=LoginLinkOut,
    dependencies=[Depends(require("system:write"))],
)
async def login_link(request: Request) -> LoginLinkOut:
    """A one-time sign-in link (the tray's "Open Fetchbox": no password prompt on the
    computer Fetchbox runs on). Valid 60 seconds, once."""
    return LoginLinkOut(path=f"/login?code={_auth(request).issue_login_code()}")


@router.post(
    "/login-code",
    response_model=SessionOut,
    dependencies=[Depends(public())],
    responses=problem_responses(401),
)
async def login_with_code(body: LoginCodeIn, request: Request, response: Response) -> SessionOut:
    """Trade a one-time code from a sign-in link for a session."""
    auth = _auth(request)
    token, csrf = await auth.redeem_login_code(
        body.code, ip=client_ip(request), user_agent=request.headers.get("user-agent")
    )
    _set_session_cookies(request, response, token, csrf)
    return SessionOut(csrf_token=csrf, user="admin")
