"""Loopback-only Host/Origin guard for the local SSE server (SPEC-18 §18.8).

FastMCP (4.0.10) ships a ``HostOriginGuardMiddleware``, but it is wired into the
``http``/``streamable-http`` transports only: ``create_sse_app`` — the legacy SSE
transport ``concierge serve --transport sse`` uses — takes no such protection and
accepts any ``Host``/``Origin``. This closes that specific gap: a forged ``Host``
header (DNS rebinding) is rejected with 421, and a cross-origin browser request is
rejected with 403, the same outcomes FastMCP's own guard produces for its transports.
"""

from ipaddress import ip_address
from typing import Iterable

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send


def _hostname(value: str) -> str:
    """Strip a port and brackets, so ``127.0.0.1:8000`` and ``[::1]:8000`` compare cleanly."""
    value = value.strip()
    if value.startswith("["):
        return value[1 : value.index("]")] if "]" in value else value
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def _is_loopback(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ip_address(hostname).is_loopback
    except ValueError:
        return False


def _origin_hostname(origin: str) -> str:
    # "scheme://host[:port]" -> "host"
    without_scheme = origin.split("://", 1)[-1]
    return _hostname(without_scheme.split("/", 1)[0])


class LoopbackHostOriginGuard:
    """Rejects a non-loopback ``Host`` header and a cross-origin browser request.

    ``allowed_hosts`` lets ``--allow-remote`` keep working: operators who explicitly
    chose a non-loopback bind address name it so the guard does not lock them out.
    """

    def __init__(self, app: ASGIApp, allowed_hosts: Iterable[str] = ()) -> None:
        self.app = app
        self.allowed_hosts = {_hostname(h).lower() for h in allowed_hosts}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        host = _hostname(headers.get("host", ""))
        if not (_is_loopback(host) or host.lower() in self.allowed_hosts):
            await PlainTextResponse("Misdirected Request", status_code=421)(scope, receive, send)
            return

        origin = headers.get("origin")
        if origin:
            origin_host = _origin_hostname(origin)
            if not (_is_loopback(origin_host) or origin_host.lower() in self.allowed_hosts):
                await PlainTextResponse("Forbidden Origin", status_code=403)(scope, receive, send)
                return

        await self.app(scope, receive, send)
