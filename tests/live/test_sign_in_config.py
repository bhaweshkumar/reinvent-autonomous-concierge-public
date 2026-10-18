"""Tier 0 - sign-in configuration: the real authorization server accepts what login sends.

The browser step needs a human, but whether the client ID, scope, identity provider and
callback URLs are accepted can be checked without credentials: a valid request is
redirected to the identity provider, an invalid one to the OAuth error page.
"""

from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from reinvent_concierge.auth import (
    CALLBACK_PATH,
    CALLBACK_PORTS,
    build_authorize_url,
    generate_pkce_pair,
)
from reinvent_concierge.config import Config

pytestmark = pytest.mark.live


async def _authorize_redirect(redirect_uri: str) -> str:
    _, challenge = generate_pkce_pair()
    url = build_authorize_url(Config(), redirect_uri, challenge, "state")
    async with httpx.AsyncClient(timeout=20) as http:
        response = await http.get(url)
    assert response.status_code == 302, f"authorize returned {response.status_code}"
    return response.headers["location"]


@pytest.mark.parametrize("port", [CALLBACK_PORTS[0], CALLBACK_PORTS[-1]])
async def test_authorize_accepts_our_callback(port):
    location = await _authorize_redirect(f"http://127.0.0.1:{port}{CALLBACK_PATH}")

    assert "error" not in parse_qs(urlparse(location).query), location
    assert urlparse(location).path != "/error", location


async def test_authorize_rejects_an_unregistered_callback():
    location = await _authorize_redirect(f"http://127.0.0.1:9999{CALLBACK_PATH}")

    assert parse_qs(urlparse(location).query).get("error") == ["redirect_mismatch"]
