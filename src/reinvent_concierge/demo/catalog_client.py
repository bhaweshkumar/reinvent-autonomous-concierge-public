"""Anonymous, hard-allowlisted catalog REST client for the DEMO profile (SPEC-21 §21.1/§21.2).

Only ``GET /events`` and ``GET /events/{id}/sessions*`` ever reach the network; any other
path or method raises ``DemoCatalogAllowlistError`` before a request is built. There is no
field anywhere on this class that could hold a credential or an auth header, so even a bug
elsewhere cannot make it send one.
"""

import re
from typing import TYPE_CHECKING, Any, AsyncIterator, Dict, Optional

import httpx

from ..api.mapping import event_from_api, session_from_api
from ..models import Event, Session

if TYPE_CHECKING:
    from ..config import Config

# Matches /events, /events/{id}, /events/{id}/sessions, /events/{id}/sessions/{sessionId}.
# Deliberately excludes every write path (/reservations, /favorites, /schedule, /personal-time).
_ALLOWED_PATH = re.compile(r"^/events(/[^/]+)?(/sessions(/[^/]+)?)?$")


class DemoCatalogAllowlistError(ValueError):
    """Raised when DEMO-profile code would reach a non-allowlisted path or method."""


class DemoCatalogClient:
    """Implements ``ingestion.sync``'s ``CatalogClient`` Protocol: GET-only, anonymous.

    Takes the full ``Config`` (matching ``AWSEventsRestClient``'s constructor pattern) so
    ``app_context.py`` can correct ``.config`` in place once ``GetEvent`` reports the real
    event timezone, the same way it does for the live client.
    """

    def __init__(self, config: "Config", http_client: Optional[httpx.AsyncClient] = None) -> None:
        self.config = config
        self._http = http_client or httpx.AsyncClient(
            base_url=config.aws_events_api_base_url.rstrip("/"),
            timeout=30.0,
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "DemoCatalogClient":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.aclose()

    async def _get(self, path: str, **kwargs) -> httpx.Response:
        if not _ALLOWED_PATH.match(path):
            raise DemoCatalogAllowlistError(f"DEMO profile refuses GET {path}: not allowlisted")
        response = await self._http.get(path, **kwargs)
        response.raise_for_status()
        return response

    async def get_event(self, event_id: Optional[str] = None) -> Event:
        response = await self._get(f"/events/{event_id or self.config.aws_events_event_id}")
        return event_from_api(response.json()["event"])

    async def list_sessions(self, event_id: Optional[str] = None) -> AsyncIterator[Session]:
        """ListSessions, fully paginated; sessions the domain model can't represent are skipped."""
        path = f"/events/{event_id or self.config.aws_events_event_id}/sessions"
        next_token = None
        while True:
            params: Dict[str, Any] = {"nextToken": next_token} if next_token else {}
            response = await self._get(path, params=params)
            data = response.json()
            for item in data.get("items", []):
                try:
                    yield session_from_api(item, self.config.event_timezone)
                except (KeyError, ValueError):
                    continue
            next_token = data.get("nextToken")
            if not next_token:
                return

    async def get_session(self, session_id: str) -> Session:
        event_id = self.config.aws_events_event_id
        response = await self._get(f"/events/{event_id}/sessions/{session_id}")
        return session_from_api(response.json()["session"], self.config.event_timezone)
