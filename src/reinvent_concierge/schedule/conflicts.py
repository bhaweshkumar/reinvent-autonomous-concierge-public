"""Schedule conflict detection and transit-buffer proposals (pure: never calls the API)."""

import logging
from datetime import timedelta
from typing import Dict, List, Literal, Optional, Tuple

from ..booking.changes import ChangeKind, PlanChange
from ..booking.transit_blocks import (
    is_concierge_transit,
    same_route,
    transit_description,
    transit_title,
)
from ..models import AttendeeSchedule, ConflictReport, Session
from ..transit import TransitEngine

logger = logging.getLogger(__name__)

_TRANSIT_RISK = "Creates a concierge-owned transit block."


class ConflictManager:
    """Detects venue-transit conflicts and proposes transit buffers; writes go elsewhere."""

    def __init__(self, transit_engine: TransitEngine, reserved_seat_lead_minutes: int = 0):
        self.transit_engine = transit_engine
        self.reserved_seat_lead_minutes = reserved_seat_lead_minutes

    def _lead_for(self, session: Session) -> int:
        return self.reserved_seat_lead_minutes if session.is_reservable else 0

    def detect_conflicts(self, schedule: AttendeeSchedule) -> List[ConflictReport]:
        """Assess each consecutive pair of booked sessions (sorted by start time).

        Venue changes with enough gap yield ``ADD_BUFFER``; gaps shorter than the
        transit time (including overlaps) yield ``CANCEL_ONE``.
        """
        booked = sorted(schedule.booked, key=lambda s: s.start_time)
        reports = [self._assess_pair(a, b) for a, b in zip(booked, booked[1:], strict=False)]
        return [report for report in reports if report is not None]

    def conflicts_involving(
        self, schedule: AttendeeSchedule, session_id: str
    ) -> List[ConflictReport]:
        return [
            c
            for c in self.detect_conflicts(schedule)
            if session_id in (c.session_a_id, c.session_b_id)
        ]

    def propose_transit_buffers(
        self, schedule: AttendeeSchedule
    ) -> Tuple[List[PlanChange], List[ConflictReport]]:
        """CREATE_TRANSIT changes for ``ADD_BUFFER`` pairs that have no concierge-owned buffer
        yet, plus the ``CANCEL_ONE`` conflicts left for the attendee. Safe to call repeatedly."""
        sessions: Dict[str, Session] = {s.session_id: s for s in schedule.booked}
        owned = [block for block in schedule.personal_time if is_concierge_transit(block)]
        changes: List[PlanChange] = []
        unresolved: List[ConflictReport] = []
        for conflict in self.detect_conflicts(schedule):
            if conflict.suggested_action == "CANCEL_ONE":
                unresolved.append(conflict)
                continue
            from_session = sessions[conflict.session_a_id]
            to_session = sessions[conflict.session_b_id]
            title = transit_title(conflict.from_venue, conflict.to_venue)
            start = from_session.end_time
            if any(same_route(b.title, title) and b.start_time == start for b in owned):
                continue
            minutes = self.transit_engine.check_feasibility(
                from_session.end_time,
                to_session.start_time,
                from_session.venue_id,
                to_session.venue_id,
                from_floor=from_session.floor,
                to_floor=to_session.floor,
                from_zone=from_session.zone,
                to_zone=to_session.zone,
                reserved_seat_lead_minutes=self._lead_for(to_session),
            ).transit_needed
            changes.append(
                PlanChange(
                    kind=ChangeKind.CREATE_TRANSIT,
                    title=title,
                    start_time=start,
                    end_time=start + timedelta(minutes=minutes),
                    location=conflict.from_venue,
                    description=transit_description(
                        to_session.session_id,
                        f"Travel {minutes} min from {conflict.from_venue} to "
                        f"{conflict.to_venue} for {to_session.title}",
                    ),
                    risk=_TRANSIT_RISK,
                )
            )
        return changes, unresolved

    def _assess_pair(self, a: Session, b: Session) -> Optional[ConflictReport]:
        # Never raises: an unrecognized venue estimates rather than blocking the check
        # (SPEC-19 §19.1); margin_minutes/estimate_quality surface the uncertainty instead.
        result = self.transit_engine.check_feasibility(
            a.end_time,
            b.start_time,
            a.venue_id,
            b.venue_id,
            from_floor=a.floor,
            to_floor=b.floor,
            from_zone=a.zone,
            to_zone=b.zone,
            reserved_seat_lead_minutes=self._lead_for(b),
        )

        action: Literal["ADD_BUFFER", "CANCEL_ONE"]
        if not result.is_feasible:
            # A real time conflict is flagged even on a low-confidence estimate.
            action = "CANCEL_ONE"
        elif result.estimate_quality == "NORMAL" and result.transit_needed > 0:
            # A buffer is only auto-proposed from a trustworthy (registry-resolved) estimate.
            action = "ADD_BUFFER"
        else:
            return None
        return ConflictReport(
            session_a_id=a.session_id,
            session_b_id=b.session_id,
            from_venue=result.from_venue,
            to_venue=result.to_venue,
            deficit_minutes=result.deficit,
            suggested_action=action,
            estimate_quality=result.estimate_quality,
        )
