"""Email notifications, sent to a real SMTP server (aiosmtpd) on localhost: plain, STARTTLS
and TLS-from-the-start, with a login, checked against a test certificate authority."""

from __future__ import annotations

import socket
import ssl
import sys
from collections.abc import Iterator
from email import message_from_bytes
from email.message import Message
from typing import Any

import httpx
import pytest
import trustme
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult, Envelope, LoginPassword

from fetchbox.core.db import utcnow
from fetchbox.core.secrets import MASK
from fetchbox.providers.notifiers import Notification, NotifierError, mail
from fetchbox.providers.notifiers.mail import EmailNotifier, EmailSettings
from tests.conftest import Running
from tests.torrents import until

USER, PASSWORD = "me@example.com", "app-password-1234"


class Inbox:
    def __init__(self) -> None:
        self.got: list[tuple[Envelope, Message]] = []

    async def handle_DATA(self, server: Any, session: Any, envelope: Envelope) -> str:
        self.got.append((envelope, message_from_bytes(envelope.original_content or b"")))
        return "250 OK"


def _auth(server: Any, session: Any, envelope: Any, mechanism: str, data: Any) -> AuthResult:
    ok = isinstance(data, LoginPassword) and (data.login, data.password) == (
        USER.encode(),
        PASSWORD.encode(),
    )
    return AuthResult(success=ok, handled=False)  # a failure answers 535


@pytest.fixture(scope="module")
def ca() -> trustme.CA:
    return trustme.CA()


@pytest.fixture
def client_tls(ca: trustme.CA) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ca.configure_trust(ctx)
    return ctx


def _server_tls(ca: trustme.CA) -> ssl.SSLContext:
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ca.issue_cert("localhost").configure_cert(ctx)
    return ctx


def _start(inbox: Inbox, **kw: Any) -> Controller:
    with socket.socket() as s:  # a free port (aiosmtpd doesn't take 0)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    # server_hostname: its default (socket.getfqdn) can outlast the start-up wait on a Mac
    c = Controller(inbox, hostname="127.0.0.1", port=port, authenticator=_auth,
                   server_hostname="localhost", ready_timeout=30, **kw)  # fmt: skip
    c.start()
    return c


@pytest.fixture
def plain() -> Iterator[tuple[Inbox, int]]:
    inbox = Inbox()
    c = _start(inbox)
    yield inbox, c.port
    c.stop()


@pytest.fixture
def starttls(ca: trustme.CA) -> Iterator[tuple[Inbox, int]]:
    inbox = Inbox()
    c = _start(inbox, tls_context=_server_tls(ca), require_starttls=True)
    yield inbox, c.port
    c.stop()


def _note() -> Notification:
    return Notification(
        event="import.completed",
        title="Downloaded Dune (2021)",
        body="Dune (2021)/Dune (2021).mkv\nWEBDL-1080p",
        occurred_at=utcnow(),
    )


async def test_an_event_arrives_as_an_email(
    c: httpx.AsyncClient, server: Running, plain: tuple[Inbox, int]
) -> None:
    inbox, port = plain
    body = {"kind": "email", "name": "Email",
            "settings": {"host": "localhost", "port": port, "sender": "fetchbox@example.com",
                         "to": "me@example.com, you@example.com"}}  # fmt: skip
    r = await c.post("/notifications/targets", json=body)
    assert r.status_code == 201, r.text
    t = r.json()
    test = (await c.post(f"/notifications/targets/{t['id']}/test")).json()
    assert test == {"ok": True, "message": "Sent a test email to me@example.com, you@example.com",
                    "details": test.get("details")}  # fmt: skip
    bus = server.app.state.fb.bus
    await bus.emit("import.completed", name="Dune", year=2021,
                   path="Dune (2021)/Dune (2021).mkv", quality="WEBDL-1080p")  # fmt: skip

    async def two() -> bool:
        return len(inbox.got) >= 2

    await until(two, within=10)
    env, msg = inbox.got[1]
    assert env.mail_from == "fetchbox@example.com"
    assert env.rcpt_tos == ["me@example.com", "you@example.com"]
    assert msg["Subject"] == "Downloaded Dune (2021)"
    assert msg["From"] == "Fetchbox <fetchbox@example.com>"
    text = msg.get_payload(decode=True).decode().replace("\r\n", "\n")  # type: ignore[union-attr]
    assert text.startswith("Dune (2021)/Dune (2021).mkv\nWEBDL-1080p\n")
    assert "Fetchbox · import.completed" in text


async def test_a_login_goes_over_starttls(
    starttls: tuple[Inbox, int], client_tls: ssl.SSLContext
) -> None:
    inbox, port = starttls
    cfg = EmailSettings(host="localhost", port=port, username=USER, password=PASSWORD,
                        to="me@example.com")  # fmt: skip
    await EmailNotifier(cfg, tls=client_tls).send(_note())
    [(env, msg)] = inbox.got
    assert env.mail_from == USER and msg["Subject"] == "Downloaded Dune (2021)"

    bad = EmailSettings.model_validate({**cfg.model_dump(), "password": "wrong"})
    with pytest.raises(NotifierError, match="refused the user name or password"):
        await EmailNotifier(bad, tls=client_tls).send(_note())
    # the system's certificate authorities don't know the test one
    with pytest.raises(NotifierError, match="certificate isn't valid"):
        await EmailNotifier(cfg).send(_note())


async def test_no_password_over_a_plain_connection(plain: tuple[Inbox, int]) -> None:
    inbox, port = plain
    cfg = EmailSettings(host="localhost", port=port, username=USER, password=PASSWORD,
                        to="me@example.com")  # fmt: skip
    with pytest.raises(NotifierError, match="doesn't offer encryption"):
        await EmailNotifier(cfg).send(_note())
    assert inbox.got == []


async def test_tls_from_the_start(
    ca: trustme.CA, client_tls: ssl.SSLContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    inbox = Inbox()
    srv = _start(inbox, ssl_context=_server_tls(ca), auth_require_tls=False)  # it is TLS
    port = srv.port
    monkeypatch.setattr(mail, "TLS_PORTS", {port})  # as if it were 465
    try:
        cfg = EmailSettings(host="localhost", port=port, username=USER, password=PASSWORD,
                            to="me@example.com")  # fmt: skip
        await EmailNotifier(cfg, tls=client_tls).send(_note())
        assert len(inbox.got) == 1
    finally:
        srv.stop()


async def test_private_addresses_can_be_blocked(plain: tuple[Inbox, int]) -> None:
    _, port = plain
    cfg = EmailSettings(host="127.0.0.1", port=port, sender="f@example.com", to="me@example.com")
    with pytest.raises(NotifierError, match="private or local address"):
        await EmailNotifier(cfg, public_only=True).send(_note())


async def test_settings_are_checked_and_the_password_masked(c: httpx.AsyncClient) -> None:
    def errors(settings: dict[str, Any]) -> str:
        return str(EmailSettings.model_validate(settings))

    ok = {"host": "smtp.gmail.com", "username": USER, "password": PASSWORD, "to": USER}
    assert EmailSettings.model_validate({**ok, "host": " smtp://smtp.gmail.com "}).host == (
        "smtp.gmail.com"
    )
    for bad in ({"host": "me@gmail.com"}, {"to": "nobody"}, {"username": "me", "sender": None},
                {"username": None, "sender": "f@example.com"}):  # fmt: skip
        with pytest.raises(ValueError):
            errors({**ok, **bad})
    r = await c.post(
        "/notifications/targets", json={"kind": "email", "name": "Mail", "settings": ok}
    )
    assert r.status_code == 201, r.text
    t = r.json()
    assert t["settings"]["password"] == MASK and t["settings"]["host"] == "smtp.gmail.com"
    # sending the mask back keeps the password (edits send every field back)
    r = await c.patch(f"/notifications/targets/{t['id']}",
                      json={"settings": {**t["settings"], "to": "other@example.com"}},
                      headers={"If-Match": t["etag"]})  # fmt: skip
    assert r.status_code == 200, r.text
    r = await c.post("/notifications/targets", json={"kind": "email", "name": "Bad",
                     "settings": {**ok, "to": "nobody"}})  # fmt: skip
    assert r.status_code == 422 and "settings" in r.json()["errors"][0]["field"]


def test_certificate_authorities_without_the_systems(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # like the Mac desktop app: OpenSSL's default certificate locations hold nothing
    (tmp_path / "none.pem").write_text("")
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "none.pem"))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path))
    if sys.platform != "win32":  # Windows always adds its own certificate store
        assert ssl.create_default_context().get_ca_certs() == []
    assert len(mail.trusted().get_ca_certs()) > 50
