"""Async seat snapper worker (SPEC-07, SPEC-17): polls the waitlist and reserves open seats.

State machine: IDLE -> POLLING_SCHEDULE -> CHECKING_AVAILABILITY -> ATTEMPTING_RESERVATION.

Reservations go through ``ChangeSetApprovals.apply_immediately`` only. The snapper never
cancels: a seat that overlaps the schedule is recorded as ``SWAP_PROPOSED`` for the attendee
to review, and waiting out throttling belongs to the operation quota governor.
"""

import asyncio
import logging
import signal
from typing import Callable, Dict, List, Optional, Protocol, Sequence

from ..audit import AuditLogger
from ..booking.changes import ApplyResult, ChangeKind, PlanChange
from ..config import Config
from ..exceptions import RateLimitError
from ..ingestion import IngestionService
from ..models import AttendeeSchedule, Session, WaitlistEntry, WaitlistStatus, utc_now
from ..schedule import ConflictManager
from ..waitlist import WaitlistStore

logger = logging.getLogger(__name__)

Notifier = Callable[[str, str], None]

_RESERVE_RISK = "Reserves a waitlisted seat without cancelling an existing reservation."


class ReserveGateway(Protocol):
    async def apply_immediately(
        self, event_id: str, changes: Sequence[PlanChange]
    ) -> ApplyResult: ...


class ScheduleReads(Protocol):
    async def read(
        self, include_favorites: bool = True, include_personal_time: bool = True
    ) -> AttendeeSchedule: ...


def desktop_notification(title: str, message: str) -> None:
    """Best-effort desktop notification; failures are logged, never raised."""
    try:
        from plyer import notification

        notification.notify(
            title=title, message=message, app_name="re:Invent Concierge", timeout=10
        )
    except Exception as e:  # plyer raises backend-specific errors per platform
        logger.warning("Desktop notification failed: %s", e)


def _overlaps(a: Session, b: Session) -> bool:
    return a.start_time < b.end_time and b.start_time < a.end_time


def _waiting_status(previous: WaitlistStatus) -> WaitlistStatus:
    """Status for a cycle that did not try to reserve: a pending proposal stays pending."""
    return "SWAP_PROPOSED" if previous == "SWAP_PROPOSED" else "WATCHING"


def _operation_errors(result: ApplyResult, prefix: str) -> List[str]:
    return [op.error for op in result.operations if op.error and op.error.startswith(prefix)]


class SeatSnapperWorker:
    """Background worker that turns waitlist entries into reservations."""

    STATE_IDLE = "IDLE"
    STATE_POLLING = "POLLING_SCHEDULE"
    STATE_CHECKING = "CHECKING_AVAILABILITY"
    STATE_RESERVING = "ATTEMPTING_RESERVATION"

    def __init__(
        self,
        config: Config,
        ingestion: IngestionService,
        conflict_manager: ConflictManager,
        audit: AuditLogger,
        waitlist: WaitlistStore,
        approvals: ReserveGateway,
        reader: ScheduleReads,
        poll_interval: Optional[float] = None,
        notifier: Notifier = desktop_notification,
    ):
        self.config = config
        self.ingestion = ingestion
        self.conflict_manager = conflict_manager
        self.audit = audit
        self.waitlist = waitlist
        self.approvals = approvals
        self.reader = reader
        self.poll_interval = poll_interval or config.snapper_poll_interval_seconds
        self._notify = notifier
        self.state = self.STATE_IDLE
        self._shutdown = asyncio.Event()
        self._reservations_deferred = False

    # ------------------------------------------------------------------ loop

    async def start(self) -> None:
        """Poll until ``stop()``; a cycle in progress always runs to completion."""
        self._shutdown.clear()
        logger.info("SeatSnapperWorker started (every %ss)", self.poll_interval)
        while not self._shutdown.is_set():
            await self.run_cycle()
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                pass
        logger.info("SeatSnapperWorker stopped")

    def stop(self) -> None:
        self._shutdown.set()

    def install_signal_handlers(self, loop: asyncio.AbstractEventLoop) -> None:
        """Stop gracefully on SIGTERM/SIGINT."""
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stop)

    async def run_cycle(self) -> None:
        """Process every enabled entry once, in priority order (hot-reloaded from disk)."""
        self.state = self.STATE_POLLING
        self._reservations_deferred = False
        try:
            entries = self.waitlist.entries()
        except (OSError, ValueError) as e:
            logger.error("Cannot read waitlist: %s", e)
            self.state = self.STATE_IDLE
            return

        checked: Dict[str, WaitlistEntry] = {}
        removed: List[str] = []
        for entry in entries:
            if not entry.enabled:
                continue
            previous = entry.status
            try:
                if await self._process_entry(entry, previous):
                    removed.append(entry.session_id)
            except RateLimitError as e:
                # The quota governor owns waiting; try again next cycle.
                entry.status = _waiting_status(previous)
                self._throttled(entry.session_id, str(e))
            except Exception:
                logger.exception("Error processing waitlist entry %s", entry.session_id)
                entry.status = "FAILED"
            checked[entry.session_id] = entry

        self.waitlist.apply_poll_results(checked, removed)
        self.state = self.STATE_IDLE
        logger.info("Poll cycle done: %d checked, %d resolved", len(checked), len(removed))

    # ------------------------------------------------------------------ per entry

    async def _process_entry(self, entry: WaitlistEntry, previous: WaitlistStatus) -> bool:
        """Returns True when the entry should leave the waitlist.

        ``previous`` is the status before this cycle: a pending swap proposal survives cycles
        that never get to try a reservation (dry-run, deferral, throttling).
        """
        self.state = self.STATE_CHECKING
        entry.last_checked = utc_now()
        session = await self.ingestion.sync_single(entry.session_id)

        if session is None or session.status == "CANCELLED":
            self.audit.record(
                "WAITLIST_REMOVE",
                session_id=entry.session_id,
                status="CONFIRMED",
                details="Session no longer in the catalog",
            )
            return True
        if not session.has_open_seat:
            entry.status = "FULL"
            return False

        entry.status = _waiting_status(previous)
        if not self.config.write_access_active:
            logger.info("Seat open for %s; not reserving in dry-run mode", entry.session_id)
            return False
        if self._reservations_deferred:
            logger.info("Reservations deferred this cycle; %s waits", entry.session_id)
            return False

        schedule = await self.reader.read(include_favorites=False)
        blockers = sorted(
            s.session_id
            for s in schedule.booked
            if s.session_id != session.session_id and _overlaps(s, session)
        )
        if blockers:
            self._propose_swap(entry, previous, f"overlaps {', '.join(blockers)}")
            return False
        entry.status = "WATCHING"
        return await self._reserve(entry, previous)

    def _propose_swap(self, entry: WaitlistEntry, previous: WaitlistStatus, reason: str) -> None:
        """Mark the entry SWAP_PROPOSED; audit and notify only on entering that state."""
        entry.status = "SWAP_PROPOSED"
        if previous == "SWAP_PROPOSED":
            return
        message = f"Seat open but {reason}; review the swap in the concierge"
        self.audit.record(
            "SWAP_PROPOSED", session_id=entry.session_id, status="PROPOSED", details=message
        )
        self._notify("Seat open: swap needed", message)

    async def _reserve(self, entry: WaitlistEntry, previous: WaitlistStatus) -> bool:
        """Reserve only; nothing is ever cancelled to make room."""
        self.state = self.STATE_RESERVING
        session_id = entry.session_id
        change = PlanChange(kind=ChangeKind.RESERVE, session_id=session_id, risk=_RESERVE_RISK)
        result = await self.approvals.apply_immediately(self.config.aws_events_event_id, [change])

        if result.state == "APPLIED":
            self.audit.record(
                "RESERVE_SESSION",
                session_id=session_id,
                status="CONFIRMED",
                details=f"Auto-reserved from waitlist (priority {entry.priority})",
            )
            self.audit.record("WAITLIST_REMOVE", session_id=session_id, status="CONFIRMED")
            self._notify("Session reserved!", f"Auto-reserved {entry.title or session_id}")
            # The seat is held and the entry resolved; the transit check is advisory only.
            try:
                await self._report_transit_conflicts(entry)
            except Exception as e:
                logger.warning("Transit check after reserving %s skipped: %s", session_id, e)
                self.audit.record(
                    "TRANSIT_CONFLICT",
                    session_id=session_id,
                    status="SKIPPED",
                    details=f"Transit check skipped: {type(e).__name__}",
                )
            return True

        if session_id in result.deferred:
            # HTTP 409 / write inactive: no other reservation can succeed this cycle either.
            self._reservations_deferred = True
            reason = "; ".join(_operation_errors(result, "DEFERRED:")) or "reservations deferred"
            entry.status = "WATCHING"
            self.audit.record(
                "RESERVE_SESSION", session_id=session_id, status="DEFERRED", details=reason
            )
            return False

        throttles = _operation_errors(result, "THROTTLED:")
        if throttles:
            entry.status = _waiting_status(previous)
            self._throttled(session_id, "; ".join(throttles))
            return False

        code = next((c for sid, c in result.refusal_codes if sid == session_id), None)
        if code == "sessionFull":
            entry.status = "FULL"
        elif code == "scheduleConflict":
            self._propose_swap(entry, previous, "the reservation was refused (scheduleConflict)")
        else:
            entry.status = "FAILED"
        details = (
            f"Reservation refused: {code}"
            if code
            else f"Reservation outcome unverified; run ended {result.state}"
        )
        self.audit.record(
            "RESERVE_SESSION", session_id=session_id, status="FAILED", details=details
        )
        return False

    async def _report_transit_conflicts(self, entry: WaitlistEntry) -> None:
        """Tell the attendee about unreachable neighbours; resolving them is their call."""
        session_id = entry.session_id
        schedule = await self.reader.read(include_favorites=False)
        titles = {s.session_id: s.title for s in schedule.booked}
        for conflict in self.conflict_manager.conflicts_involving(schedule, session_id):
            if conflict.suggested_action != "CANCEL_ONE":
                continue
            other = (
                conflict.session_b_id
                if conflict.session_a_id == session_id
                else conflict.session_a_id
            )
            message = (
                f"Reserved {entry.title or session_id}; it leaves no time to reach "
                f"{titles.get(other) or other}. Review in the concierge."
            )
            self.audit.record(
                "TRANSIT_CONFLICT",
                session_id=session_id,
                status="MANUAL_REQUIRED",
                details=message,
            )
            self._notify("Transit conflict", message)

    def _throttled(self, session_id: str, details: str) -> None:
        self.audit.record("THROTTLED", session_id=session_id, status="WATCHING", details=details)
