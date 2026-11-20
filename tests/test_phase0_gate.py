"""Phase 0 gate, end to end against real `fetchbox serve` processes.

1. A fresh start creates the admin user and the `default` key (printed once).
2. A job survives `kill -9` of the server and resumes on restart.
3. Its events arrive over WebSocket and SSE after the restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import socket
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from websockets.asyncio.client import connect as ws_connect

KEY_RE = re.compile(r"\bfb_[a-z0-9]{8}_[A-Za-z0-9_-]{20,}")
TIMEOUT = 60.0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Server:
    def __init__(self, data: Path, port: int) -> None:
        self.data, self.port = data, port
        self.stdout: list[str] = []
        self.proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self.base = f"http://127.0.0.1:{port}/api/v1"

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "fetchbox",
            "--data",
            str(self.data),
            "serve",
            "--port",
            str(self.port),
            "--log-format",
            "json",
            "--log-level",
            "warning",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._reader = asyncio.create_task(self._read())
        async with httpx.AsyncClient() as c:
            for _ in range(int(TIMEOUT * 10)):
                with contextlib.suppress(httpx.TransportError):
                    if (await c.get(f"{self.base}/system/ping")).status_code == 200:
                        return
                if self.proc.returncode is not None:
                    raise AssertionError(f"server exited early: {self.stdout}")
                await asyncio.sleep(0.1)
        raise AssertionError("server did not become ready")

    async def _read(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        async for line in self.proc.stdout:
            self.stdout.append(line.decode(errors="replace"))

    async def kill(self) -> None:
        assert self.proc is not None
        self.proc.kill()  # SIGKILL / TerminateProcess: no shutdown hooks run
        await self.proc.wait()
        if self._reader:
            await self._reader

    async def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 30)
            except TimeoutError:
                self.proc.kill()
                await self.proc.wait()
        if self._reader:
            await self._reader

    def printed_keys(self) -> list[str]:
        return KEY_RE.findall("".join(self.stdout))


@contextlib.asynccontextmanager
async def server(data: Path) -> AsyncIterator[Server]:
    s = Server(data, _free_port())
    await s.start()
    try:
        yield s
    finally:
        await s.stop()


async def _ws_until(base: str, key: str, job_id: str) -> list[str]:
    url = base.replace("http://", "ws://") + "/events/ws?topics=job.*"
    seen: list[str] = []
    async with ws_connect(url, additional_headers={"Authorization": f"Bearer {key}"}) as ws:
        async for raw in ws:
            msg: dict[str, Any] = json.loads(raw)
            if (msg.get("subject") or {}).get("id") == job_id:
                seen.append(msg["type"])
                if msg["type"] in ("job.succeeded", "job.failed"):
                    return seen
    return seen


async def _sse_until(base: str, key: str, job_id: str) -> list[str]:
    seen: list[str] = []
    headers = {"Authorization": f"Bearer {key}"}
    async with (
        httpx.AsyncClient(timeout=None) as c,
        c.stream("GET", f"{base}/events/stream", params={"topics": "job.*"}, headers=headers) as r,
    ):
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        event = None
        async for line in r.aiter_lines():
            if line.startswith("event: "):
                event = line[7:]
            elif (
                line.startswith("data: ")
                and event
                and json.loads(line[6:]).get("subject", {}).get("id") == job_id
            ):
                seen.append(event)
                if event in ("job.succeeded", "job.failed"):
                    return seen
    return seen


@pytest.mark.timeout(180)
async def test_phase0_gate(tmp_path: Path) -> None:
    data = tmp_path / "data"

    # 1. First run: admin + default key, printed exactly once.
    first = Server(data, _free_port())
    await first.start()
    keys = first.printed_keys()
    assert len(keys) == 1, first.stdout
    key = keys[0]
    auth = {"Authorization": f"Bearer {key}"}
    async with httpx.AsyncClient(base_url=first.base, headers=auth) as c:
        me = (await c.get("/auth/me")).json()
        assert me["name"] == "default" and me["scopes"] == ["*"]

        # 2. Start a job and kill the server while it runs.
        r = await c.post("/system/selftest", json={"seconds": 8})
        assert r.status_code == 202
        job_id = r.json()["id"]
        for _ in range(int(TIMEOUT * 10)):
            if (await c.get(f"/jobs/{job_id}")).json()["status"] == "running":
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("job never started")
    await first.kill()

    # 3. Restart: no new key; the job resumes and its events stream out.
    async with server(data) as second:
        assert second.printed_keys() == [], "bootstrap must not run twice"
        ws_seen, sse_seen = await asyncio.wait_for(
            asyncio.gather(
                _ws_until(second.base, key, job_id), _sse_until(second.base, key, job_id)
            ),
            TIMEOUT,
        )
        async with httpx.AsyncClient(base_url=second.base, headers=auth) as c:
            job = (await c.get(f"/jobs/{job_id}")).json()
            activity = (await c.get("/activity", params={"filter[category]": "system"})).json()

    assert ws_seen[-1] == "job.succeeded" and "job.progress" in ws_seen, ws_seen
    assert sse_seen[-1] == "job.succeeded", sse_seen
    assert job["status"] == "succeeded"
    assert job["result"]["attempt"] == 2  # attempt 1 died with the first process
    starts = [a for a in activity["items"] if a["action"] == "started"]
    assert len(starts) == 2
