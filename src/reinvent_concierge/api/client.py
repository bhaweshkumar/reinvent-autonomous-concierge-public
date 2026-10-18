"""AWS Events REST API client for re:Invent Autonomous Concierge.

Paths and payloads follow docs/aws-service.md and the service's OpenAPI description:
every operation is scoped to ``/events/{eventId}`` under the ``/v1`` base URL.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Awaitable, Callable, Dict, List, Optional, Sequence

import httpx
from pydantic import ValidationError

from ..config import Config
from ..credentials import CredentialManager
from ..exceptions import (
    AuthenticationRequiredError,
    AWSEventsAPIError,
    AWSEventsServerError,
    CredentialsMissingError,
    RateLimitError,
    ReservationConflictError,
    ScheduleReadBackError,
    WriteAccessInactiveError,
)
from ..models import AttendeeSchedule, BulkResult, Event, ReservationResult, Session
from .mapping import (
    build_personal_time_payload,
    bulk_result_from_api,
    event_from_api,
    personal_time_from_api,
    session_from_api,
)
from .ports import OperationQuotaGovernor
from .rate_limiter import TokenBucketRateLimiter

logger = logging.getLogger(__name__)

MAX_BATCH_SIZE = 10
RATE_LIMITER_TIMEOUT_SECONDS = 60.0
MAX_ERROR_BODY_CHARS = 500

SessionResolver = Callable[[str], Awaitable[Optional[Session]]]


class AWSEventsRestClient:
    """Async client for the AWS Events REST API with rate limiting and typed responses."""

    def __init__(
        self,
        config: Config,
        credential_manager: CredentialManager,
        http_client: Optional[httpx.AsyncClient] = None,
        operation_quota_governor: Optional[OperationQuotaGovernor] = None,
    ):
        self.config = config
        self.credential_manager = credential_manager
        self.event_id = config.aws_events_event_id
        self.rate_limiter = TokenBucketRateLimiter(config.api_rate_limit_rps)
        self.operation_quota_governor = operation_quota_governor
        self.default_quota_timeout_seconds = config.quota_default_timeout_seconds
        # Redirects are not followed so the bearer token is never forwarded to another host.
        self._http = http_client or httpx.AsyncClient(
            base_url=config.aws_events_api_base_url.rstrip("/"),
            timeout=30.0,
            follow_redirects=False,
        )
        self._refresh_lock = asyncio.Lock()

    async def __aenter__(self) -> "AWSEventsRestClient":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ transport

    def _auth_header(self, required: bool) -> Dict[str, str]:
        try:
            return self.credential_manager.get_auth_header()
        except CredentialsMissingError:
            if required:
                raise
            return {}

    async def _send(
        self, method: str, path: str, headers: Dict[str, str], **kwargs: Any
    ) -> httpx.Response:
        if not await self.rate_limiter.acquire(timeout=RATE_LIMITER_TIMEOUT_SECONDS):
            raise asyncio.TimeoutError("Rate limiter timeout after 60 seconds")
        return await self._http.request(method, path, headers=headers, **kwargs)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        quota_units: int = 1,
        auth_required: bool = False,
        allow_not_found: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        """Send a rate-limited request and map error statuses to typed exceptions.

        A 401 triggers one token refresh and one retry. A 404 is returned rather than
        raised when ``allow_not_found`` is set (removals whose target is already gone).
        """
        if self.operation_quota_governor is not None:
            acquired = await self.operation_quota_governor.acquire(
                operation, quota_units, timeout_seconds=self.default_quota_timeout_seconds
            )
            if not acquired:
                available_at = await self.operation_quota_governor.available_at(
                    operation, quota_units
                )
                retry_after = max(0.0, (available_at - datetime.now(timezone.utc)).total_seconds())
                raise RateLimitError(retry_after)
        headers = self._auth_header(auth_required)
        response = await self._send(method, path, headers, **kwargs)
        if response.status_code == 401 and await self._refresh_access_token(headers):
            response = await self._send(method, path, self._auth_header(True), **kwargs)
        return await self._check_status(response, allow_not_found, operation)

    async def _check_status(
        self, response: httpx.Response, allow_not_found: bool, operation: str
    ) -> httpx.Response:
        status = response.status_code
        if status < 400:
            return response
        if status == 404 and allow_not_found:
            logger.info(
                "%s %s returned 404; treating as already done",
                response.request.method,
                response.request.url.path,
            )
            return response

        # The edge can answer with HTML or no body, so never assume JSON here.
        body = response.text[:MAX_ERROR_BODY_CHARS]
        if status == 429:
            retry_after = self._parse_retry_after(response)
            if self.operation_quota_governor is not None:
                await self.operation_quota_governor.block_for(operation, retry_after)
            logger.warning("Rate limited by API; retry after %ss", retry_after)
            raise RateLimitError(retry_after)
        if status == 409:
            if not self.config.write_access_active:
                logger.warning("Operation unavailable (write access inactive)")
                raise WriteAccessInactiveError(status, body)
            logger.error("Operation unavailable while write access is active")
            raise ReservationConflictError(status, body)
        if status == 401:
            raise AuthenticationRequiredError(status, body)
        if status >= 500:
            logger.error("AWS Events server error %s: %s", status, body)
            raise AWSEventsServerError(status, body)
        logger.error("AWS Events API error %s: %s", status, body)
        raise AWSEventsAPIError(status, body)

    @staticmethod
    def _parse_retry_after(response: httpx.Response) -> float:
        try:
            return max(0.0, float(response.headers.get("Retry-After", 1)))
        except ValueError:
            return 1.0

    async def _refresh_access_token(self, stale_headers: Dict[str, str]) -> bool:
        """Exchange the refresh token for a new access token. Returns True on success."""
        async with self._refresh_lock:
            # Another request may have refreshed while this one waited on the lock.
            if stale_headers and self._auth_header(False) != stale_headers:
                return True
            refresh_token = self.credential_manager.get_refresh_token()
            if not refresh_token:
                return False
            response = await self._http.post(
                self.config.aws_events_oauth_token_url,
                data={
                    "grant_type": "refresh_token",
                    "client_id": self.config.aws_events_oauth_client_id,
                    "refresh_token": refresh_token,
                },
            )
            if response.status_code != 200:
                logger.warning("Access token refresh failed with HTTP %s", response.status_code)
                return False
            tokens = response.json()
            self.credential_manager.store_token(
                tokens["access_token"], tokens.get("refresh_token") or refresh_token
            )
            logger.info("Access token refreshed")
            return True

    def _require_write_access(self, operation: str) -> None:
        if not self.config.write_access_active:
            raise WriteAccessInactiveError(
                409, f"{operation} skipped: WRITE_ACCESS_ACTIVE=false (dry-run mode)"
            )

    def _event_path(self, suffix: str = "", event_id: Optional[str] = None) -> str:
        return f"/events/{event_id or self.event_id}{suffix}"

    @staticmethod
    def _validate_batch(session_ids: Sequence[str]) -> List[str]:
        unique = list(dict.fromkeys(session_ids))
        if not 1 <= len(unique) <= MAX_BATCH_SIZE:
            raise ValueError(f"A batch must contain 1-{MAX_BATCH_SIZE} distinct session IDs")
        return unique

    # ------------------------------------------------------------------ catalog

    async def list_events(self, include_past: bool = False) -> List[Event]:
        """ListEvents: ongoing and upcoming events, plus ended ones if ``include_past``."""
        params = {"includePast": "true"} if include_past else {}
        response = await self._request("GET", "/events", operation="ListEvents", params=params)
        return [event_from_api(item) for item in response.json().get("items", [])]

    async def get_event(self, event_id: Optional[str] = None) -> Event:
        response = await self._request(
            "GET", self._event_path(event_id=event_id), operation="GetEvent"
        )
        return event_from_api(response.json()["event"])

    async def list_sessions(
        self, event_id: Optional[str] = None, page_token: Optional[str] = None
    ) -> AsyncGenerator[Session, None]:
        """ListSessions, fully paginated from ``page_token``; yields typed sessions.

        Sessions the domain model cannot represent (e.g. unscheduled) are skipped.
        """
        next_token = page_token
        while True:
            params = {"nextToken": next_token} if next_token else {}
            response = await self._request(
                "GET",
                self._event_path("/sessions", event_id),
                operation="ListSessions",
                params=params,
            )
            data = response.json()
            for item in data.get("items", []):
                try:
                    yield session_from_api(item, self.config.event_timezone)
                except (KeyError, ValueError, ValidationError) as e:
                    logger.warning("Skipping session %s: %s", item.get("sessionId"), e)
            # A short page is not the last page; only a missing nextToken is.
            next_token = data.get("nextToken")
            if not next_token:
                return

    async def get_session(self, session_id: str, event_id: Optional[str] = None) -> Session:
        response = await self._request(
            "GET", self._event_path(f"/sessions/{session_id}", event_id), operation="GetSession"
        )
        return session_from_api(response.json()["session"], self.config.event_timezone)

    # ------------------------------------------------------------------ schedule

    async def get_raw_schedule(self) -> Dict[str, Any]:
        """GetSchedule wire payload: ``reserved``/``favorites`` IDs and ``personalTime``.

        Cheaper than ``get_schedule`` when only IDs are needed (no GetSession calls).
        """
        response = await self._request(
            "GET", self._event_path("/schedule"), operation="GetSchedule", auth_required=True
        )
        return response.json()["schedule"]

    async def _resolve_sessions(
        self, session_ids: Sequence[str], resolver: Optional[SessionResolver]
    ) -> List[Session]:
        sessions = []
        for session_id in session_ids:
            session = await resolver(session_id) if resolver else None
            sessions.append(session or await self.get_session(session_id))
        return sessions

    async def get_schedule(
        self,
        include_favorites: bool = True,
        include_personal_time: bool = True,
        session_resolver: Optional[SessionResolver] = None,
    ) -> AttendeeSchedule:
        """GetSchedule, with session IDs expanded into sessions.

        ``session_resolver`` (e.g. the local catalog) is tried before GetSession so
        schedule reads do not spend the per-minute GetSession quota.
        """
        payload = await self.get_raw_schedule()
        return AttendeeSchedule(
            booked=await self._resolve_sessions(payload.get("reserved", []), session_resolver),
            favorites=(
                await self._resolve_sessions(payload.get("favorites", []), session_resolver)
                if include_favorites
                else []
            ),
            personal_time=(
                [personal_time_from_api(p) for p in payload.get("personalTime", [])]
                if include_personal_time
                else []
            ),
        )

    async def associate_favorites(self, session_ids: Sequence[str]) -> BulkResult:
        """AssociateFavorites; read ``failed`` even on success."""
        response = await self._request(
            "POST",
            self._event_path("/favorites"),
            operation="AssociateFavorites",
            quota_units=len(requested := self._validate_batch(session_ids)),
            auth_required=True,
            json={"sessionIds": requested},
        )
        return bulk_result_from_api(response.json())

    async def disassociate_favorite(self, session_id: str) -> None:
        """DisassociateFavorite; a 404 means it was already removed."""
        await self._request(
            "DELETE",
            self._event_path(f"/favorites/{session_id}"),
            operation="DisassociateFavorite",
            auth_required=True,
            allow_not_found=True,
        )

    async def reserve_sessions(self, session_ids: Sequence[str]) -> List[ReservationResult]:
        """ReserveSessions (requires WRITE_ACCESS_ACTIVE=True); one result per session."""
        self._require_write_access("ReserveSessions")
        requested = self._validate_batch(session_ids)
        response = await self._request(
            "POST",
            self._event_path("/reservations"),
            operation="ReserveSessions",
            quota_units=len(requested),
            auth_required=True,
            json={"sessionIds": requested},
        )
        bulk = bulk_result_from_api(response.json())
        failures = {f.session_id: f for f in bulk.failed}
        results = []
        for session_id in requested:
            failure = failures.get(session_id)
            if failure is None and session_id in bulk.successful:
                results.append(ReservationResult(session_id=session_id, status="CONFIRMED"))
                continue
            code = failure.code if failure else "other"
            results.append(
                ReservationResult(
                    session_id=session_id,
                    status="FAILED",
                    code=code,
                    message=f"Reservation refused: {code}",
                    conflicts_with=failure.conflicts_with if failure else [],
                )
            )
        return results

    async def cancel_reservation(self, session_id: str) -> None:
        """CancelReservation; a 404 means it was already canceled."""
        self._require_write_access("CancelReservation")
        await self._request(
            "DELETE",
            self._event_path(f"/reservations/{session_id}"),
            operation="CancelReservation",
            auth_required=True,
            allow_not_found=True,
        )

    async def create_personal_time(
        self,
        title: str,
        start_time: datetime,
        end_time: datetime,
        description: str,
        location: Optional[str] = None,
    ) -> str:
        """CreatePersonalTime and return its ID.

        The API returns no body, so the ID is read back from GetSchedule.

        Raises:
            ValueError: If the block violates the PersonalTime field constraints
            ScheduleReadBackError: If the created block cannot be found afterwards
        """
        self._require_write_access("CreatePersonalTime")
        payload = build_personal_time_payload(title, start_time, end_time, description, location)
        await self._request(
            "POST",
            self._event_path("/personal-time"),
            operation="CreatePersonalTime",
            auth_required=True,
            json=payload,
        )

        schedule = await self.get_raw_schedule()
        matches = [
            block["personalTimeId"]
            for block in schedule.get("personalTime", [])
            if block.get("title") == payload["title"]
            and block.get("startDateTime") == payload["startDateTime"]
            and block.get("endDateTime") == payload["endDateTime"]
        ]
        if not matches:
            raise ScheduleReadBackError(
                f"Personal time '{title}' at {payload['startDateTime']} not found after create"
            )
        return matches[-1]

    async def update_personal_time(
        self,
        personal_time_id: str,
        title: str,
        start_time: datetime,
        end_time: datetime,
        description: str,
        location: Optional[str] = None,
    ) -> None:
        """UpdatePersonalTime: a full replacement, so every field is resent."""
        self._require_write_access("UpdatePersonalTime")
        payload = build_personal_time_payload(title, start_time, end_time, description, location)
        await self._request(
            "PUT",
            self._event_path(f"/personal-time/{personal_time_id}"),
            operation="UpdatePersonalTime",
            auth_required=True,
            json=payload,
        )

    async def delete_personal_time(self, personal_time_id: str) -> None:
        """DeletePersonalTime; removing an entry that is already gone is not an error."""
        self._require_write_access("DeletePersonalTime")
        await self._request(
            "DELETE",
            self._event_path(f"/personal-time/{personal_time_id}"),
            operation="DeletePersonalTime",
            auth_required=True,
            allow_not_found=True,
        )
