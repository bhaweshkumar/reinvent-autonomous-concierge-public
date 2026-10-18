"""Journaled execution of schedule writes (SPEC-17 §17.3)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Dict, List, Optional, Protocol, Sequence, Tuple, TypeVar, cast
from uuid import UUID

import httpx
from pydantic import JsonValue

from ..exceptions import (
    AWSEventsAPIError,
    AWSEventsServerError,
    RateLimitError,
    ReservationConflictError,
    ScheduleReadBackError,
    WriteAccessInactiveError,
)
from ..models import AttendeeSchedule, BulkResult, ReservationResult
from ..util.time import as_utc as _as_utc
from .changes import DESTRUCTIVE_KINDS, ChangeKind, PlanChange
from .transit_blocks import same_route

if TYPE_CHECKING:  # runtime imports would cycle: api.client -> planner -> application -> booking
    from ..planner.apply_journal import ApplyJournalStore
    from ..planner.models import ApplyOperation, ApplyOperationState
    from .reader import ScheduleReader

RemoteResult = BulkResult | List[ReservationResult] | str | None
RemoteResultT = TypeVar("RemoteResultT", bound=RemoteResult)
RequiredValueT = TypeVar("RequiredValueT")

# Markers returned by ``_remote`` next to the result.
_OK = "ok"  # CONFIRMED
_FAILED = "failed"  # FAILED or REFUSED (including throttling); nothing is deferred
_DEFERRED = "deferred"  # 409 / write inactive: skip the remaining reservation/swap steps
_UNKNOWN = "unknown"  # timeout/transport/5xx: left ATTEMPTING, reconciled from GetSchedule

_BENIGN_CODES = frozenset({"alreadyScheduled", "alreadyFavorited"})
# A 404 on these removals means the item is already gone (aws-service.md: "treat 404 as done").
_ALREADY_DONE_ON_404 = frozenset({"UNFAVORITE", "CANCEL", "CANCEL_FOR_SWAP"})
_RESERVATION_KINDS = frozenset({ChangeKind.RESERVE, ChangeKind.SWAP})


class ScheduleWriter(Protocol):
    async def associate_favorites(self, session_ids: Sequence[str]) -> BulkResult: ...

    async def disassociate_favorite(self, session_id: str) -> None: ...

    async def reserve_sessions(self, session_ids: Sequence[str]) -> List[ReservationResult]: ...

    async def cancel_reservation(self, session_id: str) -> None: ...

    async def create_personal_time(
        self,
        title: str,
        start_time: datetime,
        end_time: datetime,
        description: str,
        location: Optional[str] = None,
    ) -> str: ...

    async def delete_personal_time(self, personal_time_id: str) -> None: ...


@dataclass(frozen=True)
class ExecutionOutcome:
    compensation_incomplete: bool
    deferred: Tuple[str, ...] = ()


@dataclass
class _RunState:
    """Per-execute bookkeeping; never shared between runs."""

    unknown: bool = False
    deferred: bool = False


def _is_unknown_outcome(error: Exception) -> bool:
    if isinstance(
        error,
        (
            httpx.TimeoutException,
            httpx.TransportError,
            AWSEventsServerError,
            ScheduleReadBackError,
        ),
    ):
        return True
    return isinstance(error, AWSEventsAPIError) and error.status_code >= 500


def _is_already_done(operation_type: str, error: Exception) -> bool:
    return (
        operation_type in _ALREADY_DONE_ON_404
        and isinstance(error, AWSEventsAPIError)
        and error.status_code == 404
    )


def _transit_created(
    request: Dict[str, JsonValue], schedule: AttendeeSchedule
) -> ApplyOperationState:
    title, start = request.get("title"), request.get("start_time")
    if not isinstance(title, str) or not isinstance(start, str):
        return "FAILED"
    try:
        start_time = _as_utc(datetime.fromisoformat(start))
    except ValueError:
        return "FAILED"
    found = any(
        same_route(block.title, title) and _as_utc(block.start_time) == start_time
        for block in schedule.personal_time
    )
    return "CONFIRMED" if found else "FAILED"


class ScheduleWriteService:
    """The one place that sends schedule writes and journals each attempt."""

    def __init__(
        self, journal: ApplyJournalStore, client: ScheduleWriter, reader: ScheduleReader
    ) -> None:
        self._journal = journal
        self._client = client
        self._reader = reader

    async def execute(
        self,
        run_id: UUID,
        changes: Sequence[PlanChange],
        *,
        allow_destructive: bool = False,
    ) -> ExecutionOutcome:
        """Send ``changes`` in order, journaling every attempt.

        Callers pass ``allow_destructive=True`` only after an approval token (and any guard)
        has been checked; without it a SWAP, CANCEL or REMOVE_TRANSIT is refused before any
        write is sent.
        """
        if not allow_destructive and any(c.kind in DESTRUCTIVE_KINDS for c in changes):
            raise ValueError("destructive changes require a validated approval")
        compensation_incomplete = False
        state = _RunState()
        deferring = False
        deferred: List[str] = []
        for change in changes:
            if compensation_incomplete and change.kind in DESTRUCTIVE_KINDS:
                break
            if deferring and change.kind in _RESERVATION_KINDS:
                deferred.append(self._required(change.session_id, "session_id"))
                continue
            state.deferred = False
            if change.kind is ChangeKind.SWAP:
                compensation_incomplete = not await self._swap(run_id, change, state)
            else:
                marker = await self._execute_change(run_id, change)
                state.unknown = state.unknown or marker == _UNKNOWN
                state.deferred = marker == _DEFERRED
            if state.deferred and change.kind in _RESERVATION_KINDS:
                deferring = True
                deferred.append(self._required(change.session_id, "session_id"))
        if state.unknown:
            await self.reconcile_unknown(run_id)
        return ExecutionOutcome(compensation_incomplete, tuple(deferred))

    async def verify(
        self, run_id: UUID, changes: Sequence[PlanChange]
    ) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
        return self._reconcile(changes, await self._reader.read())

    async def reconcile_unknown(self, run_id: UUID) -> None:
        await self._journal.reconcile_attempting(
            run_id, await self._reader.read(), self.resolve_attempting
        )

    async def _reconcile_with(self, run_id: UUID, schedule: AttendeeSchedule) -> None:
        await self._journal.reconcile_attempting(run_id, schedule, self.resolve_attempting)

    async def _execute_change(self, run_id: UUID, change: PlanChange) -> Optional[str]:
        if change.kind is ChangeKind.FAVORITE:
            session_id = self._required(change.session_id, "session_id")
            _, marker = await self._remote(
                run_id,
                "FAVORITE",
                {"session_id": session_id},
                lambda: self._client.associate_favorites([session_id]),
            )
        elif change.kind is ChangeKind.UNFAVORITE:
            session_id = self._required(change.session_id, "session_id")
            _, marker = await self._remote(
                run_id,
                "UNFAVORITE",
                {"session_id": session_id},
                lambda: self._client.disassociate_favorite(session_id),
            )
        elif change.kind is ChangeKind.RESERVE:
            session_id = self._required(change.session_id, "session_id")
            _, marker = await self._remote(
                run_id,
                "RESERVE",
                {"session_id": session_id},
                lambda: self._client.reserve_sessions([session_id]),
            )
        elif change.kind is ChangeKind.CANCEL:
            session_id = self._required(change.session_id, "session_id")
            _, marker = await self._remote(
                run_id,
                "CANCEL",
                {"session_id": session_id},
                lambda: self._client.cancel_reservation(session_id),
            )
        elif change.kind is ChangeKind.CREATE_TRANSIT:
            title = self._required(change.title, "title")
            start_time = self._required(change.start_time, "start_time")
            end_time = self._required(change.end_time, "end_time")
            _, marker = await self._remote(
                run_id,
                "CREATE_TRANSIT",
                {"title": title, "start_time": start_time.isoformat()},
                lambda: self._client.create_personal_time(
                    title,
                    start_time,
                    end_time,
                    change.description or "",
                    change.location,
                ),
            )
        elif change.kind is ChangeKind.REMOVE_TRANSIT:
            personal_time_id = self._required(change.personal_time_id, "personal_time_id")
            _, marker = await self._remote(
                run_id,
                "REMOVE_TRANSIT",
                {"personal_time_id": personal_time_id},
                lambda: self._client.delete_personal_time(personal_time_id),
            )
        else:
            return None
        return marker

    @staticmethod
    def _required(value: Optional[RequiredValueT], field_name: str) -> RequiredValueT:
        if value is None:
            raise ValueError(f"{field_name} is required for this plan change")
        return value

    async def _remote(
        self,
        run_id: UUID,
        operation_type: str,
        request: Dict[str, JsonValue],
        call: Callable[[], Awaitable[RemoteResultT]],
    ) -> Tuple[Optional[RemoteResultT], str]:
        order = len(await self._journal.operations(run_id)) + 1
        operation = await self._journal.add_operation(run_id, order, operation_type, request)
        await self._journal.mark_attempting(operation.operation_id)
        try:
            result = await call()
        except (ReservationConflictError, WriteAccessInactiveError) as error:
            await self._journal.record_operation_result(
                operation.operation_id, "REFUSED", error=f"DEFERRED: {error}"
            )
            return None, _DEFERRED
        except RateLimitError as error:
            await self._journal.record_operation_result(
                operation.operation_id,
                "REFUSED",
                error=f"THROTTLED: retry after {error.retry_after}s",
            )
            return None, _FAILED
        except Exception as error:
            if _is_already_done(operation_type, error):
                await self._journal.record_operation_result(operation.operation_id, "CONFIRMED")
                return None, _OK
            if _is_unknown_outcome(error):
                return None, _UNKNOWN  # never re-sent; the journal row stays ATTEMPTING
            await self._journal.record_operation_result(
                operation.operation_id,
                "FAILED",
                error=str(error),
            )
            return None, _FAILED
        state, payload = self._result_state(result, request.get("session_id"))
        await self._journal.record_operation_result(operation.operation_id, state, result=payload)
        return result, _OK if state == "CONFIRMED" else _FAILED

    @staticmethod
    def _result_state(
        result: RemoteResult,
        requested_id: JsonValue = None,
    ) -> Tuple[ApplyOperationState, Dict[str, JsonValue]]:
        if isinstance(result, list):
            if not any(item.session_id == requested_id for item in result):
                missing: List[Dict[str, JsonValue]] = [
                    {"session_id": requested_id, "code": "missingResult"}
                ]
                return "REFUSED", {"failures": cast(JsonValue, missing)}
            failures: List[Dict[str, JsonValue]] = [
                {"session_id": item.session_id, "code": item.code or "refused"}
                for item in result
                if item.status != "CONFIRMED" and item.code not in _BENIGN_CODES
            ]
            return (
                ("REFUSED", {"failures": cast(JsonValue, failures)})
                if failures
                else ("CONFIRMED", {})
            )
        if isinstance(result, BulkResult):
            failures = [
                {"session_id": item.session_id, "code": item.code}
                for item in result.failed
                if item.code not in _BENIGN_CODES
            ]
            return (
                ("REFUSED", {"failures": cast(JsonValue, failures)})
                if failures
                else ("CONFIRMED", {})
            )
        return "CONFIRMED", {}

    async def _swap(self, run_id: UUID, change: PlanChange, state: _RunState) -> bool:
        """Release the blockers, reserve the target, and restore on failure.

        Returns False only when a released seat may not be back (compensation incomplete).
        """
        schedule = await self._reader.read()
        booked_ids = {session.session_id for session in schedule.booked}
        target_id = self._required(change.session_id, "session_id")
        if target_id in booked_ids:  # nothing to release: e.g. an earlier step already won it
            await self._journal_already_booked(run_id, target_id)
            return True
        # A blocker already gone (e.g. released by an earlier swap in this run) is not cancelled.
        blockers = [b for b in change.blocking_session_ids if b in booked_ids]
        released: List[str] = []
        stopped = False
        for blocker_id in blockers:

            async def cancel_released_session(session_id: str = blocker_id) -> None:
                await self._client.cancel_reservation(session_id)

            _, marker = await self._remote(
                run_id,
                "CANCEL_FOR_SWAP",
                {"session_id": blocker_id},
                cancel_released_session,
            )
            if marker == _OK:
                released.append(blocker_id)
                continue
            state.deferred = marker == _DEFERRED
            if marker == _UNKNOWN:  # the cancel may have landed: restore it too if it did
                current = await self._try_read()
                if current is None:
                    # Cannot tell: assume it was released. Restoring a seat that is still
                    # held returns alreadyScheduled, which counts as success.
                    state.unknown = True
                    released.append(blocker_id)
                else:
                    await self._reconcile_with(run_id, current)
                    if blocker_id not in {session.session_id for session in current.booked}:
                        released.append(blocker_id)
            stopped = True
            break
        if not stopped:

            async def reserve_target() -> List[ReservationResult]:
                return await self._client.reserve_sessions([target_id])

            reservation, marker = await self._remote(
                run_id,
                "RESERVE_SWAP_TARGET",
                {"session_id": target_id},
                reserve_target,
            )
            state.deferred = marker == _DEFERRED
            if marker == _UNKNOWN:  # the seat may have landed: trust GetSchedule, not a resend
                current = await self._try_read()
                if current is None:  # the target may hold the slot: do not restore over it
                    state.unknown = True
                    return False
                await self._reconcile_with(run_id, current)
                if target_id in {session.session_id for session in current.booked}:
                    return True
            elif marker == _OK and reservation and reservation[0].status == "CONFIRMED":
                return True
        if not released:
            return True
        for released_id in released:

            async def restore_released_session(
                session_id: str = released_id,
            ) -> List[ReservationResult]:
                return await self._client.reserve_sessions([session_id])

            _, restore_marker = await self._remote(
                run_id,
                "RESTORE_SWAP",
                {"session_id": released_id},
                restore_released_session,
            )
            state.unknown = state.unknown or restore_marker == _UNKNOWN
        reconciled = await self._try_read()
        if reconciled is None:
            return False
        restored_ids = {session.session_id for session in reconciled.booked}
        return set(released) <= restored_ids

    async def _try_read(self) -> Optional[AttendeeSchedule]:
        """GetSchedule, or None when it fails (transport, throttling, server or API error)."""
        try:
            return await self._reader.read()
        except Exception:  # any failed read means "cannot tell"
            return None

    async def _journal_already_booked(self, run_id: UUID, target_id: str) -> None:
        order = len(await self._journal.operations(run_id)) + 1
        operation = await self._journal.add_operation(
            run_id, order, "RESERVE_SWAP_TARGET", {"session_id": target_id}
        )
        await self._journal.mark_attempting(operation.operation_id)
        failures: List[Dict[str, JsonValue]] = [
            {"session_id": target_id, "code": "alreadyScheduled"}
        ]
        await self._journal.record_operation_result(
            operation.operation_id,
            "REFUSED",
            result={"failures": cast(JsonValue, failures)},
            error="alreadyScheduled: the swap target is already booked; nothing was released",
        )

    @staticmethod
    def _reconcile(
        changes: Sequence[PlanChange], schedule: AttendeeSchedule
    ) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
        booked = {session.session_id for session in schedule.booked}
        favorites = {session.session_id for session in schedule.favorites}
        personal = {block.id for block in schedule.personal_time}
        refused: List[str] = []
        unexpected: List[str] = []
        for change in changes:
            if change.kind in _RESERVATION_KINDS and change.session_id not in booked:
                refused.append(change.session_id or change.kind.value)
            elif change.kind is ChangeKind.FAVORITE and change.session_id not in favorites:
                refused.append(change.session_id or change.kind.value)
            elif change.kind is ChangeKind.UNFAVORITE and change.session_id in favorites:
                refused.append(change.session_id or change.kind.value)
            elif change.kind is ChangeKind.CANCEL and change.session_id in booked:
                unexpected.append(change.session_id or change.kind.value)
            elif change.kind is ChangeKind.REMOVE_TRANSIT and change.personal_time_id in personal:
                unexpected.append(change.personal_time_id or change.kind.value)
        return tuple(refused), tuple(unexpected)

    @staticmethod
    def resolve_attempting(
        operation: ApplyOperation,
        schedule: AttendeeSchedule,
    ) -> ApplyOperationState:
        booked = {session.session_id for session in schedule.booked}
        favorites = {session.session_id for session in schedule.favorites}
        if operation.operation_type == "CREATE_TRANSIT":
            return _transit_created(operation.request, schedule)
        if operation.operation_type == "REMOVE_TRANSIT":
            personal_time_id = operation.request.get("personal_time_id")
            absent = personal_time_id not in {block.id for block in schedule.personal_time}
            return "CONFIRMED" if isinstance(personal_time_id, str) and absent else "FAILED"
        session_id = operation.request.get("session_id")
        if not isinstance(session_id, str):
            return "FAILED"
        if (
            operation.operation_type.startswith("RESERVE")
            or operation.operation_type == "RESTORE_SWAP"
        ):
            return "CONFIRMED" if session_id in booked else "FAILED"
        if operation.operation_type == "FAVORITE":
            return "CONFIRMED" if session_id in favorites else "FAILED"
        if operation.operation_type == "UNFAVORITE":
            return "CONFIRMED" if session_id not in favorites else "FAILED"
        if operation.operation_type.startswith("CANCEL"):
            return "CONFIRMED" if session_id not in booked else "FAILED"
        return "FAILED"
