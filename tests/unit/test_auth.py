"""Builder ID sign-in (authorization code + PKCE) and sign-out, against mocked OAuth."""

import base64
import hashlib
import string
import threading
import urllib.request
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import pytest
import respx

from reinvent_concierge.auth import (
    CALLBACK_PORTS,
    TokenSet,
    _bind_callback_server,
    build_authorize_url,
    generate_pkce_pair,
    sign_in,
    sign_out,
)
from reinvent_concierge.credentials import CredentialManager
from reinvent_concierge.exceptions import SignInError
from tests.factories import REFRESH_TOKEN, TOKEN, TOKEN_URL

AUTHORIZE_URL = "https://oauth.test/oauth2/authorize"
REVOKE_URL = "https://oauth.test/oauth2/revoke"
UNRESERVED = set(string.ascii_letters + string.digits + "-._~")


@pytest.fixture
def oauth_config(config):
    return config.model_copy(
        update={
            "aws_events_oauth_authorize_url": AUTHORIZE_URL,
            "aws_events_oauth_revoke_url": REVOKE_URL,
        }
    )


@pytest.fixture
def empty_credentials(oauth_config):
    return CredentialManager(oauth_config.aws_events_credentials_path)


def fake_browser(callback_params=None, *, tamper_state=False):
    """Stand-in for the user's browser: follows the redirect the IdP would send."""
    seen = {}

    def open_browser(url):
        query = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        seen.update(query)
        params = callback_params or {"code": "auth-code-123"}
        params = {**params, "state": "forged" if tamper_state else query["state"]}
        target = f"{query['redirect_uri']}?{urlencode(params)}"
        thread = threading.Thread(target=lambda: urllib.request.urlopen(target, timeout=5).read())  # noqa: S310
        thread.start()

    return open_browser, seen


def test_pkce_pair_follows_rfc7636():
    verifier, challenge = generate_pkce_pair()

    assert 43 <= len(verifier) <= 128
    assert set(verifier) <= UNRESERVED
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    assert challenge == expected.rstrip(b"=").decode()
    assert generate_pkce_pair()[0] != verifier


def test_authorize_url_carries_required_parameters(oauth_config):
    url = build_authorize_url(oauth_config, "http://127.0.0.1:8484/callback", "chal", "st")

    query = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert url.startswith(AUTHORIZE_URL)
    assert query == {
        "response_type": "code",
        "client_id": oauth_config.aws_events_oauth_client_id,
        "redirect_uri": "http://127.0.0.1:8484/callback",
        "scope": "openid email events/access",
        "identity_provider": "AWSBuilderID",
        "code_challenge": "chal",
        "code_challenge_method": "S256",
        "state": "st",
    }


@respx.mock
async def test_sign_in_exchanges_code_and_stores_tokens(oauth_config, empty_credentials):
    token = respx.post(TOKEN_URL).respond(
        200, json={"access_token": TOKEN, "refresh_token": REFRESH_TOKEN, "expires_in": 3600}
    )
    browser, seen = fake_browser()

    tokens = await sign_in(oauth_config, empty_credentials, open_browser=browser, timeout=10)

    form = parse_qs(token.calls[0].request.read().decode())
    assert form["grant_type"] == ["authorization_code"]
    assert form["code"] == ["auth-code-123"]
    assert form["redirect_uri"] == [seen["redirect_uri"]]
    verifier = form["code_verifier"][0]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    assert seen["code_challenge"] == challenge.rstrip(b"=").decode()
    assert urlparse(seen["redirect_uri"]).port in CALLBACK_PORTS
    assert tokens.expires_in == 3600
    assert empty_credentials.get_token() == TOKEN
    assert empty_credentials.get_refresh_token() == REFRESH_TOKEN


async def test_state_mismatch_is_rejected(oauth_config, empty_credentials):
    browser, _ = fake_browser(tamper_state=True)

    with respx.mock(assert_all_called=False) as mock:
        token = mock.post(TOKEN_URL).respond(200, json={"access_token": TOKEN})
        with pytest.raises(SignInError, match="state mismatch"):
            await sign_in(oauth_config, empty_credentials, open_browser=browser, timeout=10)

    assert token.call_count == 0
    assert not empty_credentials.token_exists()


async def test_refused_sign_in_is_reported(oauth_config, empty_credentials):
    browser, _ = fake_browser({"error": "access_denied"})

    with pytest.raises(SignInError, match="access_denied"):
        await sign_in(oauth_config, empty_credentials, open_browser=browser, timeout=10)


@respx.mock
async def test_failed_exchange_reports_oauth_error(oauth_config, empty_credentials):
    respx.post(TOKEN_URL).respond(400, json={"error": "invalid_grant"})
    browser, _ = fake_browser()

    with pytest.raises(SignInError, match="invalid_grant"):
        await sign_in(oauth_config, empty_credentials, open_browser=browser, timeout=10)
    assert not empty_credentials.token_exists()


async def test_times_out_without_callback(oauth_config, empty_credentials):
    with pytest.raises(SignInError, match="Timed out"):
        await sign_in(oauth_config, empty_credentials, open_browser=lambda url: None, timeout=0.5)


def test_callback_falls_back_to_next_reserved_port():
    first = _bind_callback_server()
    try:
        second = _bind_callback_server()
        try:
            assert second.server_port != first.server_port
            assert second.server_port in CALLBACK_PORTS
        finally:
            second.server_close()
    finally:
        first.server_close()


def test_token_repr_hides_secrets():
    text = repr(TokenSet(access_token=TOKEN, refresh_token=REFRESH_TOKEN, expires_in=60))

    assert TOKEN not in text and REFRESH_TOKEN not in text


@respx.mock
async def test_sign_out_revokes_refresh_token_and_deletes_tokens(oauth_config, empty_credentials):
    empty_credentials.store_token(TOKEN, REFRESH_TOKEN)
    revoke = respx.post(REVOKE_URL).respond(200)

    assert await sign_out(oauth_config, empty_credentials) is True

    assert parse_qs(revoke.calls[0].request.read().decode())["token"] == [REFRESH_TOKEN]
    assert not empty_credentials.credentials_path.exists()


@respx.mock
async def test_sign_out_deletes_tokens_even_if_revocation_fails(oauth_config, empty_credentials):
    empty_credentials.store_token(TOKEN, REFRESH_TOKEN)
    respx.post(REVOKE_URL).respond(400)

    assert await sign_out(oauth_config, empty_credentials) is False
    assert not empty_credentials.credentials_path.exists()


@respx.mock
async def test_sign_out_deletes_tokens_even_if_revocation_cannot_connect(
    oauth_config, empty_credentials
):
    empty_credentials.store_token(TOKEN, REFRESH_TOKEN)
    respx.post(REVOKE_URL).mock(side_effect=httpx.ConnectError("unreachable"))

    assert await sign_out(oauth_config, empty_credentials) is False
    assert not empty_credentials.credentials_path.exists()
