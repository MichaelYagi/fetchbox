"""Security headers on every HTTP response (spec: "Web UI: strict CSP").

The web UI's script policy is a <meta> tag with hashes that SvelteKit writes at build
time; a header can add to it but not loosen it, so HTML responses only get what a meta
tag can't say (no framing). JSON API responses get a policy that allows nothing at all.
Pure ASGI: streaming responses (the event stream) aren't buffered.
"""

from __future__ import annotations

from typing import Any

COMMON = [
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
]
API_CSP = b"default-src 'none'; frame-ancestors 'none'"
HTML_CSP = b"frame-ancestors 'none'; object-src 'none'; base-uri 'self'"


class SecurityHeaders:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def with_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                have = {k.lower() for k, _ in headers}
                ctype = next((v for k, v in headers if k.lower() == b"content-type"), b"")
                csp = API_CSP if ctype.startswith(b"application/json") or ctype.startswith(
                    b"application/problem+json") else HTML_CSP  # fmt: skip
                extra = [*COMMON, (b"content-security-policy", csp)]
                headers += [(k, v) for k, v in extra if k not in have]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, with_headers)
