"""Schedule, favorites, transit and cancellation tools (SPEC-18 §18.1 split of
agent/tools.py). The waitlist tools moved to ``tools_waitlist.py`` to keep this file
under the 500-line layering limit (SPEC-18 §18.7 AC1).
"""

import logging
from typing import List, Literal, Optional

import httpx

from ..booking.approvals import ApprovalRejectedError
from ..booking.changes import ChangeKind, PlanChange
from ..booking.transit_blocks import TRANSIT_MARKER
from ..exceptions import ReInventConciergeError
from ..models import ConflictReport, Session
from .audit_policy import (
    CANCEL_RISK,
    FAVORITE_RISK,
    INCOMPLETE_APPROVAL,
    RESERVE_RISK,
    SWAP_RISK,
    UNFAVORITE_RISK,
    approval_guard,
    confirmed_session_ids,
    describe,
    failure_reason,
    favorite_failure,
    held_by_must_attend,
    other_side,
    overlaps,
)
from .contracts import (
    CancelReservationInput,
    CancelReservationOutput,
    CheckTransitInput,
    CheckTransitOutput,
    GetScheduleInput,
    ManageFavoritesInput,
    ManageFavoritesOutput,
    OptimizeAndBookInput,
    OptimizeAndBookOutput,
)
from .runner import _ToolRunner

logger = logging.getLogger(__name__)


class ScheduleToolsMixin(_ToolRunner):
    # ------------------------------------------------------------------ tool 2

    async def get_my_schedule(
        self, include_favorites: bool = True, include_personal_time: bool = True
    ):
        """Return booked sessions, favorites and personal time."""
        arguments = {
            "include_favorites": include_favorites,
            "include_personal_time": include_personal_time,
        }
        return await self._run_tool(
            "get_my_schedule", GetScheduleInput, arguments, self._get_my_schedule
        )

    async def _get_my_schedule(self, params: GetScheduleInput):
        schedule = await self._schedule(params.include_favorites, params.include_personal_time)
        zone = self._zone()
        return schedule.model_copy(
            update={
                "booked": [session.localized(zone) for session in schedule.booked],
                "favorites": [session.localized(zone) for session in schedule.favorites],
                "event_timezone": self.config.event_timezone,
            }
        )

    # ------------------------------------------------------------------ tool 3

    async def optimize_and_book_session(
        self,
        session_id: str,
        auto_resolve_conflicts: bool = True,
        apply_run_id: Optional[str] = None,
        approval_token: Optional[str] = None,
    ) -> OptimizeAndBookOutput:
        """Book a session, or propose a swap that needs approval when it clashes.

        Never cancels without an approval token. Falls back to favoriting while
        reservations are closed (dry-run or HTTP 409).
        """
        arguments = {
            "session_id": session_id,
            "auto_resolve_conflicts": auto_resolve_conflicts,
            "apply_run_id": apply_run_id,
            "approval_token": approval_token,
        }
        return await self._run_tool(
            "optimize_and_book_session", OptimizeAndBookInput, arguments, self._optimize_and_book
        )

    async def _optimize_and_book(self, params: OptimizeAndBookInput) -> OptimizeAndBookOutput:
        if params.apply_run_id is not None or params.approval_token is not None:
            return await self._execute_swap(params)
        _approvals, reader = self._write_path()
        session = await self._lookup_session(params.session_id)
        schedule = await reader.read(include_favorites=False)
        others = [s for s in schedule.booked if s.session_id != session.session_id]
        candidate = schedule.model_copy(update={"booked": [*others, session]})
        transit_blocking = [
            c
            for c in self.conflict_manager.conflicts_involving(candidate, session.session_id)
            if c.suggested_action == "CANCEL_ONE"
        ]
        if not self.config.write_access_active:
            return await self._favorite_instead(session, transit_blocking)

        # Already holding the seat: reserving again is a no-op, so there is nothing to swap.
        if len(others) == len(schedule.booked):
            blockers = {s.session_id for s in others if overlaps(s, session)}
            blockers.update(other_side(c, session.session_id) for c in transit_blocking)
            if blockers:
                return await self._propose_swap(session, sorted(blockers), others)

        result = await self._apply_now(
            [PlanChange(kind=ChangeKind.RESERVE, session_id=session.session_id, risk=RESERVE_RISK)]
        )
        if result.deferred:
            # HTTP 409 means reservations are closed server-side; favorites keep working.
            return await self._favorite_instead(session, transit_blocking)
        if result.state != "APPLIED":
            code = next((c for sid, c in result.refusal_codes if sid == session.session_id), None)
            return OptimizeAndBookOutput(
                status="CONFLICT_DETECTED" if code == "scheduleConflict" else "FAILED",
                session_title=session.title,
                message=f"Not booked: {code or failure_reason(result)}",
            )
        return await self._booked(session, [], params.auto_resolve_conflicts)

    async def _propose_swap(
        self, session: Session, blockers: List[str], booked: List[Session]
    ) -> OptimizeAndBookOutput:
        event_id = self.config.aws_events_event_id
        must_attend = sorted(set(blockers) & await self._must_attend())
        if must_attend:
            return OptimizeAndBookOutput(
                status="CONFLICT_DETECTED",
                session_title=session.title,
                message=f"Not booked: conflicts with must-attend {', '.join(must_attend)}",
            )
        approvals, _reader = self._write_path()
        preview = await approvals.preview(
            event_id,
            [
                PlanChange(
                    kind=ChangeKind.SWAP,
                    session_id=session.session_id,
                    blocking_session_ids=tuple(blockers),
                    risk=SWAP_RISK,
                )
            ],
        )
        titles = {s.session_id: s.title for s in booked}
        proposed = [f"Cancel {b} ({titles.get(b, b)})" for b in blockers]
        proposed.append(f"Reserve {session.session_id}")
        return OptimizeAndBookOutput(
            status="CONFLICT_DETECTED",
            session_title=session.title,
            apply_run_id=str(preview.apply_run_id),
            approval_token=preview.approval_token,
            expires_at=preview.expires_at,
            proposed_changes=proposed,
            message=f"Approve to swap: {'; '.join(proposed)}. {SWAP_RISK}",
        )

    async def _execute_swap(self, params: OptimizeAndBookInput) -> OptimizeAndBookOutput:
        approvals, _reader = self._write_path()
        session = await self._lookup_session(params.session_id)
        if params.apply_run_id is None or params.approval_token is None:
            return OptimizeAndBookOutput(
                status="FAILED", session_title=session.title, message=INCOMPLETE_APPROVAL
            )
        guard = approval_guard(ChangeKind.SWAP, params.session_id, await self._must_attend())
        try:
            result = await approvals.apply(params.apply_run_id, params.approval_token, guard)
        except ApprovalRejectedError as error:
            return OptimizeAndBookOutput(
                status="FAILED", session_title=session.title, message=f"Not booked: {error}"
            )
        if result.state != "APPLIED":
            return OptimizeAndBookOutput(
                status="FAILED",
                session_title=session.title,
                message=" ".join([f"Swap not completed ({result.state}).", *result.manual_actions]),
            )
        canceled = confirmed_session_ids(result, "CANCEL_FOR_SWAP")
        return await self._booked(session, canceled, params.auto_resolve_conflicts)

    async def _booked(
        self, session: Session, canceled: List[str], add_buffers: bool
    ) -> OptimizeAndBookOutput:
        message = f"Reserved {session.title}"
        if canceled:
            message += f"; canceled {', '.join(canceled)}"
        buffer_title: Optional[str] = None
        if add_buffers:
            buffer_title, note = await self._add_transit_buffers(session.session_id)
            message += note
        return OptimizeAndBookOutput(
            status="CONFIRMED",
            session_title=session.title,
            transit_buffer_added=buffer_title,
            canceled_sessions=canceled,
            message=message,
        )

    async def _add_transit_buffers(self, session_id: str) -> tuple[Optional[str], str]:
        """Create missing transit buffers; CANCEL_ONE conflicts are reported, never canceled.

        Returns the title of the created block for ``session_id`` (else the first created
        block) and a note for the message.
        """
        _approvals, reader = self._write_path()
        try:
            # GetSchedule is the source of truth after a write.
            schedule = await reader.read(include_favorites=False)
            buffers, unresolved = self.conflict_manager.propose_transit_buffers(schedule)
            created: List[PlanChange] = []
            if buffers:
                result = await self._apply_now(buffers)
                confirmed = {
                    (op.request.get("title"), op.request.get("start_time"))
                    for op in result.operations
                    if op.operation_type == "CREATE_TRANSIT" and op.state == "CONFIRMED"
                }
                created = [
                    change
                    for change in buffers
                    if change.start_time is not None
                    and (change.title, change.start_time.isoformat()) in confirmed
                ]
        except (ReInventConciergeError, httpx.HTTPError) as error:
            logger.error("Transit buffers not added: %s", error)
            return None, "; transit buffers were not added"
        note = "; a transit buffer could not be added" if len(created) < len(buffers) else ""
        if unresolved:
            note += f"; still conflicting: {describe(unresolved)}"
        # A create returns no ID, so the new block is named by its title.
        marker = f"{TRANSIT_MARKER}{session_id}:"
        own = [c for c in created if (c.description or "").startswith(marker)]
        chosen = (own or created)[:1]
        return (chosen[0].title if chosen else None), note

    async def _favorite_instead(
        self, session: Session, conflicts: List[ConflictReport]
    ) -> OptimizeAndBookOutput:
        result = await self._apply_now(
            [
                PlanChange(
                    kind=ChangeKind.FAVORITE, session_id=session.session_id, risk=FAVORITE_RISK
                )
            ]
        )
        failure = favorite_failure(result)
        if failure:
            return OptimizeAndBookOutput(
                status="FAILED",
                session_title=session.title,
                message=f"Reservations are closed and favoriting failed: {failure}",
            )
        message = f"{session.title} added to favorites; reservations are not open yet"
        if conflicts:
            message += f". Transit conflicts to resolve later: {describe(conflicts)}"
        return OptimizeAndBookOutput(
            status="FAVORITED", session_title=session.title, message=message
        )

    # ------------------------------------------------------------------ tool 4

    async def manage_favorites(
        self, action: Literal["add", "remove"], session_id: str
    ) -> ManageFavoritesOutput:
        """Add a session to, or remove it from, favorites."""
        arguments = {"action": action, "session_id": session_id}
        return await self._run_tool(
            "manage_favorites", ManageFavoritesInput, arguments, self._manage_favorites
        )

    async def _manage_favorites(self, params: ManageFavoritesInput) -> ManageFavoritesOutput:
        adding = params.action == "add"
        result = await self._apply_now(
            [
                PlanChange(
                    kind=ChangeKind.FAVORITE if adding else ChangeKind.UNFAVORITE,
                    session_id=params.session_id,
                    risk=FAVORITE_RISK if adding else UNFAVORITE_RISK,
                )
            ]
        )
        failure = favorite_failure(result)
        if adding:
            message = (
                f"Could not favorite {params.session_id}: {failure}"
                if failure
                else f"Session {params.session_id} added to favorites"
            )
        else:
            message = (
                f"Could not remove {params.session_id} from favorites: {failure}"
                if failure
                else f"Session {params.session_id} removed from favorites"
            )
        return ManageFavoritesOutput(
            action=params.action,
            session_id=params.session_id,
            success=failure is None,
            message=message,
        )

    # ------------------------------------------------------------------ tool 5

    async def check_transit(self, from_session_id: str, to_session_id: str) -> CheckTransitOutput:
        """Check whether there is time to travel between two sessions."""
        arguments = {"from_session_id": from_session_id, "to_session_id": to_session_id}
        return await self._run_tool(
            "check_transit", CheckTransitInput, arguments, self._check_transit
        )

    async def _check_transit(self, params: CheckTransitInput) -> CheckTransitOutput:
        from_session = await self._lookup_session(params.from_session_id)
        to_session = await self._lookup_session(params.to_session_id)
        feasibility = self.transit.check_feasibility(
            from_session.end_time,
            to_session.start_time,
            from_session.venue_id,
            to_session.venue_id,
            from_floor=from_session.floor,
            to_floor=to_session.floor,
            from_zone=from_session.zone,
            to_zone=to_session.zone,
            reserved_seat_lead_minutes=(
                self.config.reserved_seat_lead_minutes if to_session.is_reservable else 0
            ),
        )
        return CheckTransitOutput(
            from_venue=feasibility.from_venue,
            to_venue=feasibility.to_venue,
            transit_minutes=feasibility.transit_needed,
            gap_minutes=feasibility.available_gap,
            is_feasible=feasibility.is_feasible,
            deficit_minutes=feasibility.deficit,
            margin_minutes=feasibility.margin_minutes,
            estimate_quality=feasibility.estimate_quality,
            tight=feasibility.tight,
        )

    # ------------------------------------------------------------------ tool 7 (UI drawer)

    async def cancel_reservation(
        self,
        session_id: str,
        apply_run_id: Optional[str] = None,
        approval_token: Optional[str] = None,
    ) -> CancelReservationOutput:
        """Cancel one reservation in two steps: propose (token), then execute with the token."""
        return await self._run_tool(
            "cancel_reservation",
            CancelReservationInput,
            {
                "session_id": session_id,
                "apply_run_id": apply_run_id,
                "approval_token": approval_token,
            },
            self._cancel_reservation,
        )

    async def _cancel_reservation(self, params: CancelReservationInput) -> CancelReservationOutput:
        approvals, _reader = self._write_path()
        session_id = params.session_id
        must_attend = await self._must_attend()
        if params.apply_run_id is None and params.approval_token is None:
            if session_id in must_attend:
                return CancelReservationOutput(
                    session_id=session_id,
                    status="FAILED",
                    message=f"Not canceled: {held_by_must_attend([session_id])}",
                )
            preview = await approvals.preview(
                self.config.aws_events_event_id,
                [PlanChange(kind=ChangeKind.CANCEL, session_id=session_id, risk=CANCEL_RISK)],
            )
            return CancelReservationOutput(
                session_id=session_id,
                status="APPROVAL_REQUIRED",
                apply_run_id=str(preview.apply_run_id),
                approval_token=preview.approval_token,
                expires_at=preview.expires_at,
                message=f"Approve to cancel {session_id}. {CANCEL_RISK}",
            )
        if params.apply_run_id is None or params.approval_token is None:
            return CancelReservationOutput(
                session_id=session_id, status="FAILED", message=INCOMPLETE_APPROVAL
            )
        guard = approval_guard(ChangeKind.CANCEL, session_id, must_attend)
        try:
            result = await approvals.apply(params.apply_run_id, params.approval_token, guard)
        except ApprovalRejectedError as error:
            return CancelReservationOutput(
                session_id=session_id, status="FAILED", message=f"Not canceled: {error}"
            )
        if result.state != "APPLIED":
            reason = (
                "still booked after the cancel" if result.unexpected else failure_reason(result)
            )
            return CancelReservationOutput(
                session_id=session_id,
                status="FAILED",
                message=" ".join(
                    [f"Not canceled ({result.state}): {reason}.", *result.manual_actions]
                ),
            )
        return CancelReservationOutput(
            session_id=session_id, message=f"Reservation for {session_id} canceled"
        )
