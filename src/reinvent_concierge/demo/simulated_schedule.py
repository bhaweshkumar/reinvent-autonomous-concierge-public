"""Simulated AWS Events schedule client for the hosted read-only demo (SPEC-21 §21.2).

Implements the same ``ScheduleSource``/``ScheduleWriter`` ports the real
``AWSEventsRestClient`` does, entirely in memory, with deterministic per-session outcomes
(same session id always behaves the same way, across a fresh instance). One instance
belongs to exactly one visitor session; it never calls the network and never holds a
credential of any kind.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import TYPE_CHECKING, Awaitable, Callable, Dict, List, Optional, Sequence
from uuid import uuid4

from ..exceptions import AWSEventsAPIError
from ..models import AttendeeSchedule, BulkResult, PersonalTimeBlock, ReservationResult, Session

if TYPE_CHECKING:
    from ..api.client import SessionResolver
else:
    SessionResolver = Callable[[str], Awaitable[Optional[Session]]]

# Roughly one in ten sessions simulates a full room, deterministically by session id.
_FULL_ROOM_THRESHOLD = 26  # out of 256


def _is_full(session_id: str) -> bool:
    digest = hashlib.sha256(session_id.encode()).digest()
    return digest[0] < _FULL_ROOM_THRESHOLD


class SimulatedScheduleClient:
    """Per-visitor simulated reserve/cancel/favorite/personal-time state.

    ``session_lookup`` resolves a session id to a full ``Session`` (the demo's shared,
    read-only baked catalog) so ``get_schedule`` can return full session objects the way
    the real client does.
    """

    def __init__(self, session_lookup: SessionResolver) -> None:
        self._session_lookup = session_lookup
        self._booked: Dict[str, Session] = {}
        self._favorites: Dict[str, Session] = {}
        self._personal_time: Dict[str, PersonalTimeBlock] = {}

    async def get_session(self, session_id: str) -> Session:
        """Catalog fallback, so this can stand in for the agent's single ``client`` role.

        The demo's baked catalog is comprehensive, so this should rarely trigger; it exists
        to satisfy the same call shape ``MCPAgent``/``ScheduleReader`` use in live mode.
        """
        session = await self._session_lookup(session_id)
        if session is None:
            raise AWSEventsAPIError(404, f"Session '{session_id}' not found in the demo catalog")
        return session

    # ------------------------------------------------------------------ ScheduleSource

    async def get_schedule(
        self,
        include_favorites: bool = True,
        include_personal_time: bool = True,
        session_resolver: Optional[SessionResolver] = None,
    ) -> AttendeeSchedule:
        return AttendeeSchedule(
            booked=list(self._booked.values()),
            favorites=list(self._favorites.values()) if include_favorites else [],
            personal_time=list(self._personal_time.values()) if include_personal_time else [],
        )

    # ------------------------------------------------------------------ ScheduleWriter

    async def associate_favorites(self, session_ids: Sequence[str]) -> BulkResult:
        succeeded = []
        for session_id in session_ids:
            session = await self._session_lookup(session_id)
            if session is not None:
                self._favorites[session_id] = session
                succeeded.append(session_id)
        return BulkResult(successful=succeeded, failed=[])

    async def disassociate_favorite(self, session_id: str) -> None:
        self._favorites.pop(session_id, None)  # idempotent: already-absent is success

    async def reserve_sessions(self, session_ids: Sequence[str]) -> List[ReservationResult]:
        results = []
        for session_id in session_ids:
            session = await self._session_lookup(session_id)
            if session is None:
                results.append(
                    ReservationResult(
                        session_id=session_id,
                        status="FAILED",
                        code="notFound",
                        message="Session not found in the demo catalog",
                    )
                )
            elif _is_full(session_id):
                results.append(
                    ReservationResult(
                        session_id=session_id,
                        status="FAILED",
                        code="scheduleFull",
                        message="Session is full in this simulation",
                    )
                )
            else:
                self._booked[session_id] = session
                results.append(ReservationResult(session_id=session_id, status="CONFIRMED"))
        return results

    async def cancel_reservation(self, session_id: str) -> None:
        self._booked.pop(session_id, None)  # idempotent

    async def create_personal_time(
        self,
        title: str,
        start_time: datetime,
        end_time: datetime,
        description: str,
        location: Optional[str] = None,
    ) -> str:
        block_id = uuid4().hex
        self._personal_time[block_id] = PersonalTimeBlock(
            id=block_id,
            title=title,
            start_time=start_time,
            end_time=end_time,
            description=description,
            location=location,
        )
        return block_id

    async def update_personal_time(
        self,
        personal_time_id: str,
        title: str,
        start_time: datetime,
        end_time: datetime,
        description: str,
        location: Optional[str] = None,
    ) -> None:
        if personal_time_id in self._personal_time:
            self._personal_time[personal_time_id] = PersonalTimeBlock(
                id=personal_time_id,
                title=title,
                start_time=start_time,
                end_time=end_time,
                description=description,
                location=location,
            )

    async def delete_personal_time(self, personal_time_id: str) -> None:
        self._personal_time.pop(personal_time_id, None)  # idempotent

    async def aclose(self) -> None:
        """No-op: nothing here holds a network connection to close."""
        return None
