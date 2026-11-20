from __future__ import annotations

import httpx
from fastapi import FastAPI
from pydantic import BaseModel

from fetchbox.core.errors import NotFoundError, install_error_handlers


class Body(BaseModel):
    name: str
    size: int


def _app() -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)

    @app.post("/things")
    async def create(body: Body) -> dict[str, str]:
        return {"ok": "yes"}

    @app.get("/things/{tid}")
    async def get(tid: str) -> None:
        raise NotFoundError("thing", tid)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("secret internals")

    return app


async def _client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=_app(), raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://t")


async def test_validation_problem() -> None:
    async with await _client() as c:
        r = await c.post("/things", json={"name": 1})
    body = r.json()
    assert r.status_code == 422
    assert body["type"].endswith("/validation")
    assert {e["field"] for e in body["errors"]} == {"name", "size"}


async def test_service_problem() -> None:
    async with await _client() as c:
        r = await c.get("/things/abc")
    assert r.status_code == 404
    assert r.json()["detail"] == "thing 'abc' does not exist"


async def test_unhandled_does_not_leak() -> None:
    async with await _client() as c:
        r = await c.get("/boom")
    assert r.status_code == 500
    assert "secret" not in r.text
