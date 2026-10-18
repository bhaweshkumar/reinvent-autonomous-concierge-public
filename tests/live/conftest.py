"""Fixtures for the live suite: real AWS Events API and hosted MCP server.

Opt-in tiers (environment variables):
    (always)                   public catalog checks, no sign-in
    token present              signed-in read-only checks + hosted MCP parity
    LIVE_ALLOW_WRITES=1        favorites / personal-time round trips (always cleaned up)
    LIVE_ALLOW_RESERVATIONS=1  reserve + cancel (409 expected before reservations open)
    LIVE_TEST_REFRESH=1        forces a token refresh (rotates the stored refresh token)
    LIVE_REAL_EMBEDDINGS=1     real sentence-transformers model for search tests
Other knobs: LIVE_EVENT_ID (default reinvent2026), LIVE_PUBLIC_EVENT_ID,
AWS_EVENTS_ACCESS_TOKEN (token without touching ~/.aws-events), AWS_EVENTS_CREDENTIALS_PATH.
"""

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
import pytest
import truststore

from reinvent_concierge.api import AWSEventsRestClient
from reinvent_concierge.config import APP_HOME, Config
from reinvent_concierge.credentials import CredentialManager
from reinvent_concierge.exceptions import AWSEventsAPIError, WriteAccessInactiveError
from reinvent_concierge.models import Event, Session
from tests.factories import FakeEmbedder

# Same as the CLI: trust the OS certificate store (TLS-inspecting proxies).
truststore.inject_into_ssl()

TEST_TITLE_PREFIX = "concierge-live-test"
# BulkFailureCode enum from the OpenAPI description; new values may appear over time.
BULK_FAILURE_CODES = {
    "sessionNotReservable",
    "scheduleConflict",
    "alreadyScheduled",
    "sessionFull",
    "insufficientAccess",
    "timePassed",
    "alreadyFavorited",
    "notFavorited",
    "other",
}


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip() in {"1", "true", "yes"}


def pick_untouched(
    catalog: List[Session],
    raw_schedule: Dict[str, Any],
    predicate: Callable[[Session], bool] = lambda s: True,
) -> Session:
    """A session that is neither reserved nor favorited, so a test can undo what it does."""
    taken = set(raw_schedule.get("reserved", [])) | set(raw_schedule.get("favorites", []))
    for session in catalog:
        if session.session_id not in taken and predicate(session):
            return session
    pytest.skip("No suitable session outside the attendee's schedule")


@dataclass(frozen=True)
class LiveSettings:
    event_id: str
    public_event_id: Optional[str]
    credentials_path: Path
    access_token: Optional[str]
    allow_writes: bool
    allow_reservations: bool
    test_refresh: bool
    real_embeddings: bool

    @classmethod
    def from_env(cls) -> "LiveSettings":
        return cls(
            event_id=os.environ.get("LIVE_EVENT_ID", "reinvent2026"),
            public_event_id=os.environ.get("LIVE_PUBLIC_EVENT_ID") or None,
            credentials_path=Path(
                os.environ.get("AWS_EVENTS_CREDENTIALS_PATH", str(APP_HOME / "credentials"))
            ).expanduser(),
            access_token=os.environ.get("AWS_EVENTS_ACCESS_TOKEN") or None,
            allow_writes=_flag("LIVE_ALLOW_WRITES"),
            allow_reservations=_flag("LIVE_ALLOW_RESERVATIONS"),
            test_refresh=_flag("LIVE_TEST_REFRESH"),
            real_embeddings=_flag("LIVE_REAL_EMBEDDINGS"),
        )


# Read at import: the root conftest's autouse fixture strips AWS_EVENTS_* vars per test.
LIVE = LiveSettings.from_env()


def build_config(root: Path, event_id: str, credentials_path: Path, **overrides: Any) -> Config:
    """Real API endpoints; every local data file lives under ``root``."""
    fields: Dict[str, Any] = {
        "aws_events_event_id": event_id,
        "local_db_path": root / "catalog.db",
        "planner_db_path": root / "planner_state.db",
        "audit_log_path": root / "audit_log.json",
        "waitlist_path": root / "waitlist.json",
        "aws_events_credentials_path": credentials_path,
        "write_access_active": False,
    }
    fields.update(overrides)
    return Config(**fields)


def _credentials_path(root: Path) -> Path:
    if LIVE.access_token:
        path = root / "credentials"
        CredentialManager(path).store_token(LIVE.access_token)
        return path
    # The real file, so a token refresh during the run is persisted for the user.
    return LIVE.credentials_path


def _run(coro):
    return asyncio.run(coro)


async def raw_walk(
    config: Config, event_id: str, headers: Optional[Dict[str, str]] = None
) -> Tuple[List[Dict[str, Any]], int]:
    """Every raw ListSessions item plus ``totalCount``, via plain httpx (not our client)."""
    items: List[Dict[str, Any]] = []
    token: Optional[str] = None
    async with httpx.AsyncClient(
        base_url=config.aws_events_api_base_url, headers=headers or {}, timeout=30
    ) as http:
        while True:
            response = await http.get(
                f"/events/{event_id}/sessions", params={"nextToken": token} if token else {}
            )
            response.raise_for_status()
            page = response.json()
            items.extend(page["items"])
            token = page.get("nextToken")
            if not token:
                return items, int(page["totalCount"])


# ---------------------------------------------------------------------- session-wide facts


@pytest.fixture(scope="session")
def live_root(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("live")


@pytest.fixture(scope="session")
def credentials_path(live_root) -> Path:
    return _credentials_path(live_root)


@pytest.fixture(scope="session")
def anonymous_config(live_root) -> Config:
    return build_config(live_root / "anon", LIVE.event_id, live_root / "no-credentials")


@pytest.fixture(scope="session")
def public_event(anonymous_config) -> Event:
    """A public (no-registration) event with a non-empty catalog."""

    async def discover() -> Event:
        async with AWSEventsRestClient(anonymous_config, _anonymous(anonymous_config)) as client:
            if LIVE.public_event_id:
                return await client.get_event(LIVE.public_event_id)
            events = await client.list_events(include_past=True)
            public = [e for e in events if e.authentication_required is False]
            for candidate in sorted(public, key=lambda e: e.start_date or "", reverse=True):
                sessions = client.list_sessions(candidate.event_id)
                first = await anext(sessions, None)
                await sessions.aclose()
                if first is not None:
                    return candidate
        pytest.skip("No public event with sessions found")

    return _run(discover())


@pytest.fixture(scope="session")
def registration(live_root, credentials_path) -> Dict[str, Any]:
    """Raw GetSchedule for the signed-in attendee; skips the test when not possible."""
    credentials = CredentialManager(credentials_path)
    if not credentials.token_exists():
        pytest.skip("No token: run `concierge login` or set AWS_EVENTS_ACCESS_TOKEN")
    config = build_config(live_root / "reg", LIVE.event_id, credentials_path)

    async def read() -> Dict[str, Any]:
        async with AWSEventsRestClient(config, credentials) as client:
            return await client.get_raw_schedule()

    try:
        return _run(read())
    except AWSEventsAPIError as e:
        reasons = {401: "token expired: run `concierge login`", 403: "not registered"}
        pytest.skip(f"Signed-in tests skipped for {LIVE.event_id}: {reasons.get(e.status_code, e)}")


@pytest.fixture(scope="session")
def catalog(live_root, credentials_path, registration) -> List[Session]:
    """The signed-in event's full catalog, fetched once per run (ListSessions quota)."""
    config = build_config(live_root / "cat", LIVE.event_id, credentials_path)

    async def walk() -> List[Session]:
        async with AWSEventsRestClient(config, CredentialManager(credentials_path)) as client:
            return [s async for s in client.list_sessions()]

    sessions = _run(walk())
    if not sessions:
        pytest.skip(f"{LIVE.event_id} has no published sessions yet")
    return sessions


@pytest.fixture(scope="session", autouse=True)
def sweep_test_personal_time(live_root, credentials_path):
    """Delete personal time left behind by an interrupted run (titles carry a fixed prefix)."""
    yield
    credentials = CredentialManager(credentials_path)
    if not (LIVE.allow_writes and credentials.token_exists()):
        return
    config = build_config(
        live_root / "sweep", LIVE.event_id, credentials_path, write_access_active=True
    )

    async def sweep() -> None:
        async with AWSEventsRestClient(config, credentials) as client:
            schedule = await client.get_raw_schedule()
            for block in schedule.get("personalTime", []):
                if block.get("title", "").startswith(TEST_TITLE_PREFIX):
                    await client.delete_personal_time(block["personalTimeId"])

    try:
        _run(sweep())
    except (AWSEventsAPIError, WriteAccessInactiveError, httpx.HTTPError):
        pass


# ---------------------------------------------------------------------- per-test objects


def _anonymous(config: Config) -> CredentialManager:
    return CredentialManager(config.aws_events_credentials_path)


@pytest.fixture
def live_config(tmp_path, credentials_path) -> Config:
    return build_config(tmp_path, LIVE.event_id, credentials_path)


@pytest.fixture
def live_write_config(live_config) -> Config:
    return live_config.model_copy(update={"write_access_active": True})


@pytest.fixture
def live_credentials(live_config) -> CredentialManager:
    return CredentialManager(live_config.aws_events_credentials_path)


@pytest.fixture
async def anonymous_client(anonymous_config):
    async with AWSEventsRestClient(anonymous_config, _anonymous(anonymous_config)) as client:
        yield client


@pytest.fixture
async def client(live_config, live_credentials, registration):
    async with AWSEventsRestClient(live_config, live_credentials) as rest_client:
        yield rest_client


@pytest.fixture
async def write_client(live_write_config, live_credentials, registration):
    async with AWSEventsRestClient(live_write_config, live_credentials) as rest_client:
        yield rest_client


@pytest.fixture
def embedder():
    """None selects the configured sentence-transformers model."""
    return None if LIVE.real_embeddings else FakeEmbedder()


@pytest.fixture
def require_writes():
    if not LIVE.allow_writes:
        pytest.skip("Set LIVE_ALLOW_WRITES=1 to run schedule-modifying tests")


@pytest.fixture
def require_reservations():
    if not LIVE.allow_reservations:
        pytest.skip("Set LIVE_ALLOW_RESERVATIONS=1 to run reserve/cancel tests")


class CachedCatalog:
    """CatalogClient serving the session-cached catalog, so syncs don't re-walk the API."""

    def __init__(self, event: Event, sessions: List[Session], rest_client: AWSEventsRestClient):
        self.event = event
        self.sessions = {s.session_id: s for s in sessions}
        self.rest_client = rest_client

    async def get_event(self, event_id: Optional[str] = None) -> Event:
        return self.event

    async def list_sessions(self, event_id: Optional[str] = None):
        for session in self.sessions.values():
            yield session

    async def get_session(self, session_id: str) -> Session:
        return await self.rest_client.get_session(session_id)
