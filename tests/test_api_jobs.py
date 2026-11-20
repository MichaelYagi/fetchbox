from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from fetchbox.config import Settings
from fetchbox.core.jobs import JobContext, JobRunner
from tests.conftest import running


def _handlers(r: JobRunner) -> None:
    @r.handler("t.ok")
    async def ok(ctx: JobContext) -> dict[str, Any]:
        return {"n": ctx.payload.get("n")}

    @r.handler("t.fail", max_attempts=1)
    async def fail(ctx: JobContext) -> None:
        raise RuntimeError("bad")


@pytest.fixture
async def api(settings: Settings) -> AsyncIterator[tuple[httpx.AsyncClient, JobRunner]]:
    async with running(settings, [_handlers]) as srv, srv.client() as c:
        yield c, srv.app.state.fb.jobs


async def _until(c: httpx.AsyncClient, job_id: str, status: str) -> dict[str, Any]:
    for _ in range(200):
        body: dict[str, Any] = (await c.get(f"/jobs/{job_id}")).json()
        if body["status"] == status:
            return body
        await asyncio.sleep(0.02)
    raise AssertionError(f"job never reached {status}: {body}")


async def test_get_list_and_paginate(api: tuple[httpx.AsyncClient, JobRunner]) -> None:
    c, runner = api
    ids = [(await runner.enqueue("t.ok", {"n": i})).id for i in range(5)]  # type: ignore[union-attr]
    done = await _until(c, ids[0], "succeeded")
    assert done["result"] == {"n": 0}

    only = {"filter[type]": "t.ok"}  # the startup health_check job is also listed
    page1 = (await c.get("/jobs", params={**only, "limit": 3})).json()
    assert [j["id"] for j in page1["items"]] == ids[::-1][:3]
    page2 = (
        await c.get("/jobs", params={**only, "limit": 3, "cursor": page1["next_cursor"]})
    ).json()
    assert [j["id"] for j in page2["items"]] == ids[::-1][3:]
    assert page2["next_cursor"] is None

    filtered = (await c.get("/jobs", params={"filter[type]": "t.fail"})).json()
    assert filtered["items"] == []


async def test_retry_failed_job_and_conflicts(api: tuple[httpx.AsyncClient, JobRunner]) -> None:
    c, runner = api
    job = await runner.enqueue("t.fail")
    assert job is not None
    failed = await _until(c, job.id, "failed")
    assert failed["error"] == "RuntimeError: bad"

    r = await c.post(f"/jobs/{job.id}/retry")
    assert r.status_code == 200 and r.json()["attempts"] == 0
    await _until(c, job.id, "failed")

    ok = await runner.enqueue("t.ok")
    assert ok is not None
    await _until(c, ok.id, "succeeded")
    r = await c.post(f"/jobs/{ok.id}/retry")
    assert r.status_code == 409
    assert r.headers["content-type"].startswith("application/problem+json")


async def test_missing_job_404(api: tuple[httpx.AsyncClient, JobRunner]) -> None:
    c, _ = api
    assert (await c.get("/jobs/01JD000000000000000000000X")).status_code == 404
    assert (await c.post("/jobs/01JD000000000000000000000X/cancel")).status_code == 404
