"""WebSocket, stream-token and activity endpoints (sync TestClient: it speaks WebSocket)."""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from fetchbox.app import create_app
from fetchbox.config import Settings

# websocket_connect ignores base_url paths, so WebSocket URLs are absolute.
WS = "/api/v1/events/ws"


@pytest.fixture
def tc(settings: Settings) -> Iterator[tuple[TestClient, str]]:
    keys: list[str] = []
    app = create_app(settings, on_bootstrap_key=keys.append)
    with TestClient(app, base_url="http://testserver/api/v1") as client:
        yield client, keys[0]


def _h(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _key(tc: TestClient, root: str, scopes: list[str]) -> dict[str, Any]:
    r = tc.post("/auth/keys", json={"name": "k", "scopes": scopes}, headers=_h(root))
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


def _until_event(
    ws: Any, type_: str, subject_id: str | None = None, limit: int = 200
) -> dict[str, Any]:
    """Next message of `type_` (for `subject_id`, if given). Scheduled jobs such as the
    startup health check emit job events too, so tests match on their own job's id."""
    for _ in range(limit):
        msg: dict[str, Any] = ws.receive_json()
        if msg.get("type") == type_ and (
            subject_id is None or (msg.get("subject") or {}).get("id") == subject_id
        ):
            return msg
    raise AssertionError(f"no {type_} event")


def test_ws_streams_job_events(tc: tuple[TestClient, str]) -> None:
    client, root = tc
    with client.websocket_connect(WS + "?topics=job.*", headers=_h(root)) as ws:
        r = client.post("/system/selftest", json={"seconds": 0.2}, headers=_h(root))
        assert r.status_code == 202
        assert r.headers["location"] == f"/api/v1/jobs/{r.json()['id']}"
        done = _until_event(ws, "job.succeeded", r.json()["id"])
        assert done["subject"] == {"type": "job", "id": r.json()["id"]}
        ws.send_json({"op": "ping"})
        assert _until_event(ws, "pong")


def test_ws_rejects_missing_or_insufficient_credentials(tc: tuple[TestClient, str]) -> None:
    client, root = tc
    with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect(WS):
        pass
    assert exc.value.code == 4401
    weak = _key(client, root, ["system:read"])
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect(WS, headers=_h(weak["key"])),
    ):
        pass
    assert exc.value.code == 4403


def test_stream_token_single_use(tc: tuple[TestClient, str]) -> None:
    client, root = tc
    tok = client.post("/events/token", headers=_h(root)).json()
    assert tok["expires_in"] == 60
    with client.websocket_connect(WS + f"?token={tok['token']}") as ws:
        r = client.post("/system/selftest", json={"seconds": 0}, headers=_h(root))
        _until_event(ws, "job.succeeded", r.json()["id"])
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect(WS + f"?token={tok['token']}"),
    ):
        pass
    assert exc.value.code == 4401


def test_stream_token_not_accepted_on_normal_routes(tc: tuple[TestClient, str]) -> None:
    client, root = tc
    tok = client.post("/events/token", headers=_h(root)).json()["token"]
    assert client.get(f"/system/status?token={tok}").status_code == 401


def test_auth_events_hidden_without_admin(tc: tuple[TestClient, str]) -> None:
    client, root = tc
    reader = _key(client, root, ["events:read"])
    with client.websocket_connect(WS, headers=_h(reader["key"])) as ws:
        _key(client, root, ["media:read"])  # emits auth.key_created
        job_id = client.post("/system/selftest", json={"seconds": 0}, headers=_h(root)).json()["id"]
        seen = []
        while True:
            msg = ws.receive_json()
            seen.append(msg["type"])
            if msg["type"] == "job.succeeded" and msg["subject"]["id"] == job_id:
                break
        assert not any(t.startswith("auth.") for t in seen)


def test_revoking_key_closes_its_stream(tc: tuple[TestClient, str]) -> None:
    client, root = tc
    reader = _key(client, root, ["events:read"])
    with client.websocket_connect(WS, headers=_h(reader["key"])) as ws:
        client.post(f"/auth/keys/{reader['id']}/revoke", headers=_h(root))
        assert _until_event(ws, "stream.revoked")
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4401


def test_cross_origin_cookie_ws_refused(tc: tuple[TestClient, str]) -> None:
    client, _ = tc
    # /auth/setup is localhost-only and TestClient's peer is "testclient"; set it directly.
    assert client.portal is not None
    client.portal.call(client.app.state.auth.set_password, "correct-horse")  # type: ignore[attr-defined]
    r = client.post("/auth/login", json={"username": "admin", "password": "correct-horse"})
    assert r.status_code == 200
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect(WS, headers={"Origin": "https://evil.example"}),
    ):
        pass
    assert exc.value.code == 4403
    with client.websocket_connect(WS, headers={"Origin": "http://testserver"}):
        pass


def test_invalid_topics(tc: tuple[TestClient, str]) -> None:
    client, root = tc
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect(WS + "?topics=Bad;Topic", headers=_h(root)),
    ):
        pass
    assert exc.value.code == 1008


def test_activity_records_audit_with_ip(tc: tuple[TestClient, str]) -> None:
    client, root = tc
    created = _key(client, root, ["media:read"])
    client.post(f"/auth/keys/{created['id']}/revoke", headers=_h(root))
    for _ in range(100):
        body = client.get("/activity?filter[category]=auth", headers=_h(root)).json()
        if len(body["items"]) >= 2:
            break
        time.sleep(0.02)
    revoke, create = body["items"][:2]
    assert create["message"] == f"API key 'k' ({created['prefix']}) created"
    assert revoke["action"] == "key_revoked" and revoke["actor"] == "default"
    assert create["data"]["actor"]["ip"] == "testclient"
    assert "key" not in create["data"]


def test_activity_skips_noise_and_keeps_failures(tc: tuple[TestClient, str]) -> None:
    client, root = tc
    r = client.post("/system/selftest", json={"seconds": 0.1}, headers=_h(root))
    job_id = r.json()["id"]
    for _ in range(100):
        if client.get(f"/jobs/{job_id}", headers=_h(root)).json()["status"] == "succeeded":
            break
        time.sleep(0.02)
    time.sleep(0.1)
    items = client.get("/activity?filter[category]=job", headers=_h(root)).json()["items"]
    assert items == []  # queued/started/progress/succeeded are routine (incl. health_check)
    started = client.get("/activity?filter[category]=system", headers=_h(root)).json()["items"]
    assert any(i["action"] == "started" for i in started)
