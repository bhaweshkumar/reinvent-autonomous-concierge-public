"""SPEC-18 §18.8: the SSE server's local-only DNS-rebinding guard.

FastMCP's own Host/Origin guard only wires into the http/streamable-http transports;
``concierge serve --transport sse`` uses the legacy SSE transport, which has no such
protection, so this project supplies ``LoopbackHostOriginGuard`` for it. These tests
exercise the guard as the ASGI middleware it is wired in as, against a dummy inner app.
"""

import httpx
import pytest

from reinvent_concierge.agent.transport_security import LoopbackHostOriginGuard


async def _ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


@pytest.fixture
def client():
    app = LoopbackHostOriginGuard(_ok_app)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


async def test_loopback_host_and_no_origin_passes(client):
    async with client:
        response = await client.get("/sse", headers={"Host": "127.0.0.1:8000"})
    assert response.status_code == 200


async def test_forged_host_is_rejected_as_misdirected(client):
    """DNS rebinding: a browser-resolved attacker domain sends our loopback Host anyway,
    unless it is rewritten; a forged, non-loopback Host must never reach the app."""
    async with client:
        response = await client.get("/sse", headers={"Host": "attacker.example"})
    assert response.status_code == 421


async def test_cross_origin_post_is_rejected_as_forbidden(client):
    async with client:
        response = await client.post(
            "/messages",
            headers={"Host": "127.0.0.1:8000", "Origin": "https://attacker.example"},
        )
    assert response.status_code == 403


async def test_loopback_origin_is_allowed(client):
    async with client:
        response = await client.post(
            "/messages",
            headers={"Host": "127.0.0.1:8000", "Origin": "http://localhost:8501"},
        )
    assert response.status_code == 200


async def test_non_http_scope_passes_through(client):
    """Non-HTTP scopes (e.g. lifespan) must never be blocked by a Host/Origin check."""
    sent = []

    async def app(scope, receive, send):
        sent.append(scope["type"])

    guard = LoopbackHostOriginGuard(app)
    await guard({"type": "lifespan"}, None, None)
    assert sent == ["lifespan"]
