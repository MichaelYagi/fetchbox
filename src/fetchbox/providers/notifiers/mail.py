"""Email: send each notification as a short plain-text email through a mail server (SMTP).

Gmail: server smtp.gmail.com, port 587, your address as the user name and an app
password (Google Account > Security > App passwords). Port 465 is TLS from the start;
any other port upgrades with STARTTLS when the server offers it. A password is never
sent over a connection that isn't encrypted.
"""

from __future__ import annotations

import asyncio
import smtplib
import socket
import ssl
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid
from typing import Any, ClassVar

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

from fetchbox.core.netguard import BlockedAddress, public_target
from fetchbox.core.providers import TestResult, registry
from fetchbox.providers.notifiers import Notification, NotifierError, NotifierProvider

TIMEOUT = 20.0
TLS_PORTS = {465}  # TLS from the start (SMTPS); other ports: STARTTLS


def _addresses(raw: str) -> list[str]:
    return [a for _, a in getaddresses([raw.replace(";", ",")]) if a]


class EmailSettings(BaseModel):
    host: str = Field(min_length=1, max_length=255, description="Mail server, e.g. smtp.gmail.com")
    port: int = Field(587, ge=1, le=65535, description="587 (STARTTLS) or 465 (TLS)")
    username: str | None = Field(None, max_length=320, description="Usually your email address")
    password: SecretStr | None = Field(
        None, description="For Gmail, an app password (not your normal password)"
    )
    to: str = Field(min_length=3, max_length=2000, description="Send to: one or more addresses")
    sender: str | None = Field(
        None, max_length=320, description="From address; empty: the user name"
    )

    @field_validator("host")
    @classmethod
    def _host(cls, v: str) -> str:
        v = v.strip()
        for prefix in ("smtps://", "smtp://"):
            v = v.removeprefix(prefix)
        if not v or any(c in v for c in " /@"):
            raise ValueError("must be a server name, e.g. smtp.gmail.com")
        return v

    @field_validator("username", "sender")
    @classmethod
    def _blank(cls, v: str | None) -> str | None:
        return (v or "").strip() or None

    @field_validator("to")
    @classmethod
    def _to(cls, v: str) -> str:
        found = _addresses(v)
        if not found or any("@" not in a for a in found):
            raise ValueError("must be one or more email addresses, separated by commas")
        return ", ".join(found)

    @model_validator(mode="after")
    def _from(self) -> EmailSettings:
        if "@" not in (self.sender or self.username or ""):
            raise ValueError("a From address is needed when the user name isn't an email address")
        if self.password and not self.username:
            raise ValueError("a password needs a user name")
        return self

    @property
    def from_address(self) -> str:
        return self.sender or self.username or ""


def message(n: Notification, cfg: EmailSettings) -> EmailMessage:
    m = EmailMessage()
    m["Subject"] = n.title[:200]
    m["From"] = f"Fetchbox <{cfg.from_address}>"
    m["To"] = cfg.to
    m["Date"] = formatdate(n.occurred_at.timestamp(), localtime=True)
    m["Message-ID"] = make_msgid(domain=cfg.from_address.rsplit("@", 1)[-1])
    lines = [n.body] if n.body else []
    lines += [f"{x.label}: {x.url}" for x in n.links]
    lines += ["", f"Fetchbox · {n.event}"]
    m.set_content("\n".join(lines).strip() + "\n")
    return m


class _SMTP(smtplib.SMTP):
    """Connects to a checked address; certificates are still checked against the name."""

    def __init__(self, name: str, **kw: Any) -> None:
        self._name = name
        super().__init__(**kw)

    def connect(self, host: str = "localhost", port: int = 0, source_address: Any = None) -> Any:
        out = super().connect(host, port, source_address)
        self._host = self._name  # STARTTLS checks the certificate against this
        return out


class _SMTPS(smtplib.SMTP_SSL):
    def __init__(self, name: str, context: ssl.SSLContext, **kw: Any) -> None:
        self._name = name
        super().__init__(context=context, **kw)

    def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        sock = socket.create_connection((host, port), timeout, self.source_address)
        return self.context.wrap_socket(sock, server_hostname=self._name)


def _login(smtp: smtplib.SMTP, user: str, password: str) -> None:
    """One mechanism (PLAIN, else LOGIN), not every one in turn: a wrong password is
    reported as such, not as whatever the last mechanism tried says."""
    offered = (smtp.esmtp_features.get("auth") or "").upper().split()
    for mech in ("PLAIN", "LOGIN"):
        if mech in offered:
            smtp.user, smtp.password = user, password
            method = smtp.auth_plain if mech == "PLAIN" else smtp.auth_login
            smtp.auth(mech, method, initial_response_ok=True)
            return
    smtp.login(user, password)


class EmailNotifier(NotifierProvider):
    kind: ClassVar[str] = "email"
    Settings: ClassVar[type[BaseModel]] = EmailSettings

    def __init__(self, settings: BaseModel, *, tls: ssl.SSLContext | None = None, **kw: Any):
        super().__init__(settings, **kw)
        self._tls = tls or ssl.create_default_context()  # tests: a test CA

    async def send(self, n: Notification) -> None:
        cfg = self.settings
        assert isinstance(cfg, EmailSettings)
        address = cfg.host
        if self._public_only:
            try:
                pinned, _, _ = await public_target(f"http://{cfg.host}:{cfg.port}")
            except BlockedAddress as exc:
                raise NotifierError(exc.message) from None
            address = pinned.host
        await asyncio.to_thread(self._send, message(n, cfg), cfg, address)

    def _send(self, m: EmailMessage, cfg: EmailSettings, address: str) -> None:
        where = f"{cfg.host}:{cfg.port}"
        try:
            # the name said in EHLO: smtplib's default (socket.getfqdn) can take seconds on a Mac
            me = {"timeout": TIMEOUT, "local_hostname": socket.gethostname() or "localhost"}
            if cfg.port in TLS_PORTS:
                smtp: smtplib.SMTP = _SMTPS(cfg.host, self._tls, **me)
            else:
                smtp = _SMTP(cfg.host, **me)
            with smtp:
                smtp.connect(address, cfg.port)
                smtp.ehlo()
                if not isinstance(smtp, smtplib.SMTP_SSL):
                    if smtp.has_extn("starttls"):
                        smtp.starttls(context=self._tls)
                        smtp.ehlo()
                    elif cfg.password:
                        raise NotifierError(
                            f"{where} doesn't offer encryption (STARTTLS), so Fetchbox won't "
                            "send the password; try port 465"
                        )
                if cfg.username and cfg.password:
                    _login(smtp, cfg.username, cfg.password.get_secret_value())
                smtp.send_message(m, from_addr=cfg.from_address, to_addrs=_addresses(cfg.to))
        except NotifierError:
            raise
        except smtplib.SMTPAuthenticationError:
            raise NotifierError(
                "the mail server refused the user name or password (Gmail: use an app password)"
            ) from None
        except smtplib.SMTPRecipientsRefused as exc:
            raise NotifierError(f"the mail server refused {', '.join(exc.recipients)}") from None
        except smtplib.SMTPResponseException as exc:
            text = (
                exc.smtp_error.decode(errors="replace")
                if isinstance(exc.smtp_error, bytes)
                else str(exc.smtp_error)
            )
            raise NotifierError(f"the mail server said {exc.smtp_code}: {text[:200]}") from None
        except ssl.SSLCertVerificationError as exc:
            raise NotifierError(
                f"{where}: its certificate isn't valid ({exc.verify_message})"
            ) from None
        except (OSError, smtplib.SMTPException) as exc:
            detail = (
                "no answer" if isinstance(exc, TimeoutError) else str(exc) or type(exc).__name__
            )
            raise NotifierError(f"couldn't send through {where}: {detail}") from None

    async def test(self) -> TestResult:
        from fetchbox.core.db import utcnow

        cfg = self.settings
        assert isinstance(cfg, EmailSettings)
        try:
            await self.send(Notification(event="test", title="Fetchbox test",
                                         body="Notifications work.", level="success",
                                         occurred_at=utcnow()))  # fmt: skip
        except NotifierError as exc:
            return TestResult(ok=False, message=f"Email: {exc.message}")
        return TestResult(ok=True, message=f"Sent a test email to {cfg.to}")


registry.register(EmailNotifier)
