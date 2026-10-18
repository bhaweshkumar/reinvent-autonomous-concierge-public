"""Schedule reads that resolve sessions from the local catalog first (SPEC-17 §17.2)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Protocol

from ..models import AttendeeSchedule

if TYPE_CHECKING:  # runtime import would cycle: api.client -> planner -> application -> booking
    from ..api.client import SessionResolver


class ScheduleSource(Protocol):
    async def get_schedule(
        self,
        include_favorites: bool = True,
        include_personal_time: bool = True,
        session_resolver: Optional[SessionResolver] = None,
    ) -> AttendeeSchedule: ...


class ScheduleReader:
    """Reads GetSchedule; a local resolver avoids one GetSession call per scheduled item."""

    def __init__(self, source: ScheduleSource, resolver: Optional[SessionResolver]) -> None:
        self._source = source
        self._resolver = resolver

    async def read(
        self, include_favorites: bool = True, include_personal_time: bool = True
    ) -> AttendeeSchedule:
        return await self._source.get_schedule(
            include_favorites=include_favorites,
            include_personal_time=include_personal_time,
            session_resolver=self._resolver,
        )
