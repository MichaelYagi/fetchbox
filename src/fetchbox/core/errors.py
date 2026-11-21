"""RFC 9457 problem details for every error response."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm.exc import StaleDataError
from starlette.exceptions import HTTPException as StarletteHTTPException

PROBLEM_JSON = "application/problem+json"
TYPE_BASE = "https://fetchbox.dev/problems/"


class FieldError(BaseModel):
    field: str
    message: str
    code: str | None = None


class Problem(BaseModel):
    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None
    errors: list[FieldError] | None = None


class ProblemError(Exception):
    """Raise from services; rendered as problem+json."""

    def __init__(
        self,
        status: int,
        title: str,
        detail: str | None = None,
        *,
        slug: str | None = None,
        errors: list[FieldError] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail or title)
        self.problem = Problem(
            type=f"{TYPE_BASE}{slug}" if slug else "about:blank",
            title=title,
            status=status,
            detail=detail,
            errors=errors,
        )
        self.headers = headers


class NotFoundError(ProblemError):
    def __init__(self, what: str, ident: str) -> None:
        super().__init__(404, "Not Found", f"{what} {ident!r} does not exist", slug="not-found")


class ConflictError(ProblemError):
    def __init__(self, detail: str) -> None:
        super().__init__(409, "Conflict", detail, slug="conflict")


def _render(
    problem: Problem, request: Request, headers: dict[str, str] | None = None
) -> JSONResponse:
    problem.instance = problem.instance or request.url.path
    return JSONResponse(
        problem.model_dump(exclude_none=True),
        status_code=problem.status,
        media_type=PROBLEM_JSON,
        headers=headers,
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ProblemError)
    async def _problem(request: Request, exc: ProblemError) -> JSONResponse:
        return _render(exc.problem.model_copy(), request, exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        title = phrase(exc.status_code)
        detail = exc.detail if isinstance(exc.detail, str) and exc.detail != title else None
        headers: dict[str, str] | None = getattr(exc, "headers", None)
        return _render(
            Problem(title=title, status=exc.status_code, detail=detail), request, headers
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            FieldError(
                field=".".join(str(p) for p in err.get("loc", ()) if p != "body"),
                message=str(err.get("msg", "")),
                code=str(err.get("type")) if err.get("type") else None,
            )
            for err in exc.errors()
        ]
        problem = Problem(
            type=f"{TYPE_BASE}validation",
            title="Validation Failed",
            status=422,
            detail="One or more fields are invalid",
            errors=errors,
        )
        return _render(problem, request)

    @app.exception_handler(StaleDataError)
    async def _stale(request: Request, exc: StaleDataError) -> JSONResponse:
        # A versioned row changed between our read and our write (see core.concurrency).
        return _render(
            Problem(
                type=f"{TYPE_BASE}precondition-failed",
                title="Precondition Failed",
                status=412,
                detail="The resource changed while this request was being applied",
            ),
            request,
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        import structlog

        structlog.get_logger("fetchbox.errors").exception("unhandled_error", path=request.url.path)
        return _render(Problem(title="Internal Server Error", status=500), request)


def phrase(status: int) -> str:
    """The status text, the same on every Python: 3.13 renamed 422 to RFC 9110's
    "Unprocessable Content", which earlier versions call "Unprocessable Entity"."""
    from http import HTTPStatus

    return "Unprocessable Content" if status == 422 else HTTPStatus(status).phrase


def problem_responses(*codes: int) -> dict[int | str, dict[str, Any]]:
    """OpenAPI `responses=` helper so routes document their problem bodies."""
    return {
        c: {"model": Problem, "description": phrase(c), "content": {PROBLEM_JSON: {}}}
        for c in codes
    }
