"""Outbound requests that must only reach the public internet (webhooks on an install
reachable from the internet; Settings > Network > "Only send webhooks to public
addresses"): otherwise a webhook could be pointed at the router or other machines on
the private network (SSRF).

The name is resolved once and every address checked; the request then goes to that
exact address (the Host header and the TLS certificate check still use the name), so a
second DNS answer can't swap in a private address (DNS rebinding). Redirects are never
followed by the callers.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket

import httpx


class BlockedAddress(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


async def _resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


async def public_target(url: str) -> tuple[httpx.URL, dict[str, str], dict[str, object]]:
    """(url to request, extra headers, request extensions) for ``url``, pinned to a
    checked public address; BlockedAddress when it isn't one."""
    u = httpx.URL(url)
    host = u.host
    if not host:
        raise BlockedAddress("the address has no host name")
    port = u.port or (443 if u.scheme == "https" else 80)
    try:
        addrs = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            addrs = await _resolve(host, port)
        except OSError:
            raise BlockedAddress(f"couldn't look up {host}") from None
    if not addrs:
        raise BlockedAddress(f"couldn't look up {host}")
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global:
            raise BlockedAddress(
                f"{host} is a private or local address ({a}); only public addresses are "
                "allowed (Settings > Network)"
            )
    pinned = u.copy_with(host=addrs[0])
    headers = {"Host": u.netloc.decode("ascii")}
    ext: dict[str, object] = {"sni_hostname": host} if u.scheme == "https" else {}
    return pinned, headers, ext
