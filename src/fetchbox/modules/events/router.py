from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from typing import Annotated

import anyio
from anyio.abc import TaskGroup
from fastapi import APIRouter, Depends, Header, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from fetchbox.core.errors import ProblemError, problem_responses
from fetchbox.core.state import State
from fetchbox.modules.auth.deps import (
    WS_UNAUTHORIZED,
    CurrentPrincipal,
    get_auth,
    require,
    ws_require,
)
from fetchbox.modules.auth.service import Principal
from fetchbox.modules.events.service import (
    EventStream,
    TopicError,
    envelope_json,
    parse_topics,
    sse_format,
)

router = APIRouter(prefix="/events", tags=["events"])

TOPICS_DOC = "Comma-separated topic patterns, e.g. `download.*,job.failed`. Default `*`."


class StreamToken(BaseModel):
    token: str
    expires_in: int


def _topics(raw: str | None) -> tuple[str, ...]:
    try:
        return parse_topics(raw)
    except TopicError as exc:
        raise ProblemError(422, "Validation Failed", str(exc), slug="validation") from None


@router.post(
    "/token",
    response_model=StreamToken,
    dependencies=[Depends(require("events:read"))],
)
async def create_stream_token(request: Request, p: CurrentPrincipal) -> StreamToken:
    """Single-use, 60-second token for `?token=` on `/events/ws` and `/events/stream`.

    Browsers can't attach headers to WebSocket or EventSource connections, and API keys
    must never appear in URLs, so clients exchange their real credential for one of these.
    """
    token, ttl = get_auth(request).issue_stream_token(p)
    return StreamToken(token=token, expires_in=ttl)


@router.get(
    "/stream",
    dependencies=[Depends(require("events:read", allow_stream_token=True))],
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}, **problem_responses(401, 403, 422)},
)
async def stream_sse(
    request: Request,
    state: State,
    p: CurrentPrincipal,
    topics: Annotated[str | None, Query(description=TOPICS_DOC)] = None,
    last_event_id_q: Annotated[str | None, Query(alias="last_event_id")] = None,
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    """Server-Sent Events. Reconnects resume via the `Last-Event-ID` header (up to 1,000
    events back); a `stream.gap` event means some were missed and state should be refetched.
    """
    stream = EventStream(state.bus, p, _topics(topics), last_event_id or last_event_id_q)

    async def body() -> AsyncIterator[str]:
        try:
            async for item in stream.items():
                if item.kind == "keepalive" and await request.is_disconnected():
                    return
                yield sse_format(item)
        finally:
            stream.close()

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.websocket("/ws", dependencies=[Depends(ws_require("events:read"))])
async def stream_ws(
    websocket: WebSocket,
    topics: Annotated[str | None, Query(description=TOPICS_DOC)] = None,
    last_event_id: Annotated[str | None, Query()] = None,
) -> None:
    """WebSocket stream of event envelopes (JSON text frames).

    Control frames from the server: `{"type": "stream.gap"}`, `{"type": "stream.revoked"}`.
    Clients may send `{"op": "ping"}` and get `{"type": "pong"}`.
    """
    principal: Principal = websocket.state.principal
    try:
        parsed = parse_topics(topics)
    except TopicError as exc:
        await websocket.close(code=1008, reason=str(exc))
        return
    await websocket.accept()
    stream = EventStream(websocket.app.state.fb.bus, principal, parsed, last_event_id)
    send_lock = anyio.Lock()

    async def send(text: str) -> None:
        async with send_lock:
            await websocket.send_text(text)

    async def pump(tg: TaskGroup) -> None:
        async for item in stream.items():
            if item.kind == "event" and item.event is not None:
                await send(envelope_json(item.event))
            elif item.kind == "gap":
                await send(json.dumps({"type": "stream.gap"}))
            elif item.kind == "revoked":
                await send(json.dumps({"type": "stream.revoked"}))
                await websocket.close(code=WS_UNAUTHORIZED, reason="credential revoked")
                break
        tg.cancel_scope.cancel()

    async def receive(tg: TaskGroup) -> None:
        with contextlib.suppress(WebSocketDisconnect):
            while True:
                msg = await websocket.receive_text()
                with contextlib.suppress(ValueError, TypeError, AttributeError):
                    if json.loads(msg).get("op") == "ping":
                        await send(json.dumps({"type": "pong"}))
        tg.cancel_scope.cancel()

    # Structured concurrency: whichever side ends first cancels the other, and outer
    # cancellation (server shutdown, test teardown) propagates cleanly through the group.
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(pump, tg)
            tg.start_soon(receive, tg)
    finally:
        stream.close()
