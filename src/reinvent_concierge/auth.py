"""AWS Builder ID sign-in: OAuth 2.0 authorization code flow with PKCE (docs/aws-service.md).

The callback listens on 127.0.0.1 at ``/callback`` on the first free reserved port
(8484-8489); any other redirect URI is rejected by the authorization server.
"""

import asyncio
import base64
import hashlib
import hmac
import logging
import secrets
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .config import Config
from .credentials import CredentialManager
from .exceptions import SignInError

logger = logging.getLogger(__name__)

CALLBACK_PORTS = range(8484, 8490)
CALLBACK_PATH = "/callback"
SCOPE = "openid email events/access"
IDENTITY_PROVIDER = "AWSBuilderID"
SIGN_IN_TIMEOUT_SECONDS = 300
BUILDER_ID_PROFILE_URL = "https://profile.aws.amazon.com"

_DONE_PAGE = b"<html><body><h3>%s</h3><p>You can close this window.</p></body></html>"


@dataclass(frozen=True)
class TokenSet:
    """Tokens from the token endpoint; excluded from repr so they never reach logs."""

    access_token: str = field(repr=False)
    refresh_token: Optional[str] = field(default=None, repr=False)
    expires_in: Optional[int] = None


def generate_pkce_pair() -> Tuple[str, str]:
    """Return ``(verifier, S256 challenge)``; fresh for every sign-in attempt."""
    verifier = secrets.token_urlsafe(64)  # 86 characters from the RFC 7636 unreserved set
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_authorize_url(config: Config, redirect_uri: str, challenge: str, state: str) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": config.aws_events_oauth_client_id,
            "redirect_uri": redirect_uri,
            "scope": SCOPE,
            "identity_provider": IDENTITY_PROVIDER,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
    )
    return f"{config.aws_events_oauth_authorize_url}?{query}"


class _CallbackServer(HTTPServer):
    params: Optional[Dict[str, str]] = None


class _CallbackHandler(BaseHTTPRequestHandler):
    server: _CallbackServer

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler naming
        parsed = urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self.send_error(404)
            return
        params = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        self.server.params = params
        message = b"Sign-in failed" if "error" in params else b"Signed in to AWS Events"
        body = _DONE_PAGE % message
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        # The default access log would print the authorization code.
        return


def _bind_callback_server() -> _CallbackServer:
    for port in CALLBACK_PORTS:
        try:
            return _CallbackServer(("127.0.0.1", port), _CallbackHandler)
        except OSError:
            continue
    raise SignInError("Callback ports 8484-8489 are all in use; free one and retry")


async def _wait_for_callback(server: _CallbackServer, timeout: float) -> Dict[str, str]:
    server.timeout = 1
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while server.params is None:
        if loop.time() > deadline:
            raise SignInError("Timed out waiting for the browser sign-in")
        await asyncio.to_thread(server.handle_request)
    return server.params


def _token_set(payload: Dict[str, object]) -> TokenSet:
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise SignInError("Token response did not contain an access token")
    refresh_token = payload.get("refresh_token")
    expires_in = payload.get("expires_in")
    return TokenSet(
        access_token=access_token,
        refresh_token=refresh_token if isinstance(refresh_token, str) else None,
        expires_in=int(expires_in) if isinstance(expires_in, (int, float, str)) else None,
    )


async def exchange_code(config: Config, code: str, verifier: str, redirect_uri: str) -> TokenSet:
    """Exchange the authorization code; ``redirect_uri`` must match the authorize request."""
    async with httpx.AsyncClient(timeout=30.0) as http:
        response = await http.post(
            config.aws_events_oauth_token_url,
            data={
                "grant_type": "authorization_code",
                "client_id": config.aws_events_oauth_client_id,
                "redirect_uri": redirect_uri,
                "code": code,
                "code_verifier": verifier,
            },
        )
    if response.status_code != 200:
        error = _oauth_error(response)
        raise SignInError(f"Token exchange failed (HTTP {response.status_code}: {error})")
    return _token_set(response.json())


def _oauth_error(response: httpx.Response) -> str:
    try:
        return str(response.json().get("error", "unknown_error"))
    except ValueError:
        return "unknown_error"


async def sign_in(
    config: Config,
    credentials: CredentialManager,
    open_browser: Callable[[str], object] = webbrowser.open,
    timeout: float = SIGN_IN_TIMEOUT_SECONDS,
) -> TokenSet:
    """Run the browser sign-in and store the resulting tokens.

    Raises:
        SignInError: On refusal, state mismatch, timeout or a failed code exchange
    """
    verifier, challenge = generate_pkce_pair()
    state = secrets.token_urlsafe(32)
    server = _bind_callback_server()
    try:
        redirect_uri = f"http://127.0.0.1:{server.server_port}{CALLBACK_PATH}"
        open_browser(build_authorize_url(config, redirect_uri, challenge, state))
        params = await _wait_for_callback(server, timeout)
    finally:
        server.server_close()

    if "error" in params:
        raise SignInError(f"Sign-in refused: {params['error']}")
    if not hmac.compare_digest(params.get("state", ""), state):
        raise SignInError("Sign-in state mismatch; the callback was not from this attempt")
    code = params.get("code")
    if not code:
        raise SignInError("Sign-in callback carried no authorization code")

    tokens = await exchange_code(config, code, verifier, redirect_uri)
    credentials.store_token(tokens.access_token, tokens.refresh_token)
    logger.info("Signed in; tokens stored in %s", credentials.credentials_path)
    return tokens


async def sign_out(config: Config, credentials: CredentialManager) -> bool:
    """Revoke the refresh token (if any) and delete stored tokens.

    The local tokens are deleted even when the revocation request fails or cannot connect.

    Returns:
        True if the refresh token was revoked (or there was none to revoke)
    """
    refresh_token = credentials.get_refresh_token()
    revoked = True
    try:
        if refresh_token:
            async with httpx.AsyncClient(timeout=30.0) as http:
                response = await http.post(
                    config.aws_events_oauth_revoke_url,
                    data={"client_id": config.aws_events_oauth_client_id, "token": refresh_token},
                )
            revoked = response.status_code == 200
            if not revoked:
                logger.warning("Refresh token revocation failed with HTTP %s", response.status_code)
    except httpx.HTTPError as e:
        revoked = False
        logger.warning("Refresh token revocation failed: %s", type(e).__name__)
    finally:
        credentials.clear()
    return revoked
