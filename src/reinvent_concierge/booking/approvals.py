"""Single-use approval tokens and approval-gated ad-hoc change sets (SPEC-17)."""

import hashlib
import logging
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from ..models import utc_now
from ..planner.apply_journal import ApplyJournalStore, InvalidStateTransition
from ..planner.models import ApplyOperation, ApplyRunState
from ..planner.snapshots import ScheduleSnapshotService
from ..util.time import as_utc
from .changes import (
    DESTRUCTIVE_KINDS,
    ApplyResult,
    PlanChange,
)
from .reader import ScheduleReader
from .service import ScheduleWriteService

logger = logging.getLogger(__name__)

APPROVAL_TTL = timedelta(minutes=5)
# Returns a refusal reason for the previewed changes, or None to let them run.
ChangeGuard = Callable[[Tuple[PlanChange, ...]], Optional[str]]
# ApplyRun.profile_id is a UUID, so the "ad-hoc" marker is a fixed name-based UUID.
AD_HOC_PROFILE_ID = uuid5(NAMESPACE_URL, "reinvent-concierge:ad-hoc")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DestructiveChangeError(ValueError):
    """Raised when a destructive change is requested without the approval flow."""


class ApprovalRejectedError(ValueError):
    """Raised when an approval is expired, tampered, stale, or bound to another request."""


def new_token() -> str:
    return secrets.token_urlsafe(32)


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_matches(token: str, expected_digest: str) -> bool:
    return secrets.compare_digest(digest(token), expected_digest)


class ChangeSetPreview(_Model):
    apply_run_id: UUID
    event_id: str
    changes: Tuple[PlanChange, ...]
    risk_text: Tuple[str, ...]
    source_fingerprint: str
    expires_at: datetime
    approval_token: str = Field(repr=False)


_NEEDS_REVIEW = frozenset({"PARTIAL", "COMPENSATION_INCOMPLETE", "STALE", "FAILED"})
_UNSUCCESSFUL = frozenset({"REFUSED", "FAILED", "COMPENSATION_FAILED"})
_SWAP_OPERATIONS = frozenset({"CANCEL_FOR_SWAP", "RESERVE_SWAP_TARGET", "RESTORE_SWAP"})
_REVIEW_ACTION = "Review the schedule and re-preview the plan before continuing."


def _subject(operation: ApplyOperation) -> str:
    for key in ("session_id", "personal_time_id", "title"):
        value = operation.request.get(key)
        if isinstance(value, str):
            return value
    return operation.operation_type


def _not_restored(operation: ApplyOperation) -> str:
    return (
        f"{_subject(operation)} was released and could not be restored; "
        "reserve it again if seats remain."
    )


def _unrestored_releases(operations: Sequence[ApplyOperation]) -> List[ApplyOperation]:
    """Confirmed swap releases whose target was not won and that were never restored."""
    found: List[ApplyOperation] = []
    for index, operation in enumerate(operations):
        if operation.operation_type != "CANCEL_FOR_SWAP" or operation.state != "CONFIRMED":
            continue
        later = operations[index + 1 :]
        target = next((op for op in later if op.operation_type == "RESERVE_SWAP_TARGET"), None)
        restored = any(
            op.operation_type == "RESTORE_SWAP" and _subject(op) == _subject(operation)
            for op in later
        )
        if not restored and (target is None or target.state != "CONFIRMED"):
            found.append(operation)
    return found


def compensation_incomplete(operations: Sequence[ApplyOperation]) -> bool:
    """True when a swap may have left a released seat unrestored."""
    return (
        any(op.operation_type in _SWAP_OPERATIONS and op.state == "ATTEMPTING" for op in operations)
        or any(
            op.operation_type == "RESTORE_SWAP" and op.state in _UNSUCCESSFUL for op in operations
        )
        or bool(_unrestored_releases(operations))
    )


def _manual_actions(state: ApplyRunState, operations: Sequence[ApplyOperation]) -> Tuple[str, ...]:
    if state not in _NEEDS_REVIEW:
        return ()
    actions: List[str] = []
    for operation in operations:
        if operation.operation_type == "RESTORE_SWAP" and operation.state in _UNSUCCESSFUL:
            actions.append(_not_restored(operation))
        elif operation.state == "ATTEMPTING":
            actions.append(
                f"{_subject(operation)}: the {operation.operation_type} outcome is unknown; "
                "check your schedule before retrying."
            )
    actions.extend(_not_restored(operation) for operation in _unrestored_releases(operations))
    actions.append(_REVIEW_ACTION)
    return tuple(dict.fromkeys(actions))


async def apply_result(journal: ApplyJournalStore, apply_run_id: UUID | str) -> ApplyResult:
    """Summarise a run from its journaled operations."""
    run = await journal.get_run(UUID(str(apply_run_id)))
    if run is None:
        raise ValueError(f"apply run {apply_run_id} was not found")
    operations = tuple(await journal.operations(run.run_id))
    confirmed = tuple(
        op.operation_type for op in operations if op.state in {"CONFIRMED", "COMPENSATED"}
    )
    refused = tuple(
        op.operation_type
        for op in operations
        if op.state in {"REFUSED", "FAILED", "COMPENSATION_FAILED"}
    )
    compensation = tuple(
        op.operation_type
        for op in operations
        if "RESTORE" in op.operation_type or "COMPENS" in op.operation_type
    )
    codes: List[Tuple[str, str]] = []
    for op in operations:
        failures = (op.result or {}).get("failures")
        if not isinstance(failures, list):
            continue
        for failure in failures:
            if isinstance(failure, dict):
                session_id, code = failure.get("session_id"), failure.get("code")
                if isinstance(session_id, str) and isinstance(code, str):
                    codes.append((session_id, code))
    manual = _manual_actions(run.state, operations)
    return ApplyResult(
        apply_run_id=run.run_id,
        state=run.state,
        confirmed=confirmed,
        refused=refused,
        compensation=compensation,
        manual_actions=manual,
        operations=operations,
        refusal_codes=tuple(codes),
    )


async def _settle_after_error(journal: ApplyJournalStore, run_id: UUID) -> None:
    """Move a run that raised mid-apply to a terminal state; never leave it APPLYING."""
    run = await journal.get_run(run_id)
    if run is None:
        return
    state = run.state
    if state in {"APPLYING", "VERIFYING"}:
        if compensation_incomplete(await journal.operations(run_id)):
            await journal.transition_run(run_id, state, "COMPENSATING")
            state = "COMPENSATING"
        else:
            await journal.transition_run(run_id, state, "FAILED")
            return
    if state == "COMPENSATING":
        await journal.transition_run(run_id, "COMPENSATING", "COMPENSATION_INCOMPLETE")


async def execute_and_settle(
    journal: ApplyJournalStore,
    writer: ScheduleWriteService,
    run_id: UUID,
    changes: Tuple[PlanChange, ...],
    *,
    allow_destructive: bool,
) -> ApplyResult:
    """Execute an APPLYING run, verify it, and record its end state.

    An exception from execute or verify ends the run COMPENSATION_INCOMPLETE when a swap was
    mid-flight, else FAILED; the result's manual actions name the affected sessions.
    """
    try:
        outcome = await writer.execute(run_id, changes, allow_destructive=allow_destructive)
        refused: Tuple[str, ...] = ()
        unexpected: Tuple[str, ...] = ()
        if outcome.compensation_incomplete:
            await journal.transition_run(run_id, "APPLYING", "COMPENSATING")
            await journal.transition_run(run_id, "COMPENSATING", "COMPENSATION_INCOMPLETE")
        else:
            await journal.transition_run(run_id, "APPLYING", "VERIFYING")
            refused, unexpected = await writer.verify(run_id, changes)
            target: ApplyRunState = "APPLIED" if not refused and not unexpected else "PARTIAL"
            await journal.transition_run(run_id, "VERIFYING", target)
    except Exception as error:
        # The type only: messages are not guaranteed free of request details.
        logger.warning("apply run %s stopped by %s", run_id, type(error).__name__)
        await _settle_after_error(journal, run_id)
        return await apply_result(journal, run_id)
    result = await apply_result(journal, run_id)
    update: Dict[str, object] = {"deferred": outcome.deferred}
    if not outcome.compensation_incomplete:
        update.update(refused=refused, unexpected=unexpected)
    return result.model_copy(update=update)


class ChangeSetApprovals:
    """Approval-gated execution of ad-hoc (non-plan) change sets."""

    def __init__(
        self,
        journal: ApplyJournalStore,
        snapshots: ScheduleSnapshotService,
        reader: ScheduleReader,
        writer: ScheduleWriteService,
        *,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._journal = journal
        self._snapshots = snapshots
        self._reader = reader
        self._writer = writer
        self._now = now

    async def apply_immediately(self, event_id: str, changes: Sequence[PlanChange]) -> ApplyResult:
        if any(change.kind in DESTRUCTIVE_KINDS for change in changes):
            raise DestructiveChangeError("destructive changes require preview and approval")
        snapshot = await self._snapshots.capture(event_id, await self._reader.read())
        run = await self._journal.create_run(event_id, AD_HOC_PROFILE_ID, snapshot.fingerprint)
        await self._journal.transition_run(run.run_id, "PREVIEWED", "APPROVED")
        await self._journal.transition_run(run.run_id, "APPROVED", "APPLYING")
        return await execute_and_settle(
            self._journal, self._writer, run.run_id, tuple(changes), allow_destructive=False
        )

    async def preview(self, event_id: str, changes: Sequence[PlanChange]) -> ChangeSetPreview:
        snapshot = await self._snapshots.capture(event_id, await self._reader.read())
        run = await self._journal.create_run(event_id, AD_HOC_PROFILE_ID, snapshot.fingerprint)
        token = new_token()
        expires_at = as_utc(self._now()) + APPROVAL_TTL
        payload: Dict[str, JsonValue] = {
            "changes": [cast(JsonValue, change.model_dump(mode="json")) for change in changes],
            "approval_digest": digest(token),
            "expires_at": expires_at.isoformat(),
            "source_fingerprint": snapshot.fingerprint,
            "event_id": event_id,
        }
        await self._journal.save_preview_payload(run.run_id, payload)
        return ChangeSetPreview(
            apply_run_id=run.run_id,
            event_id=event_id,
            changes=tuple(changes),
            risk_text=tuple(sorted({change.risk for change in changes if change.risk})),
            source_fingerprint=snapshot.fingerprint,
            expires_at=expires_at,
            approval_token=token,
        )

    async def apply(
        self,
        apply_run_id: UUID | str,
        approval_token: str,
        guard: Optional[ChangeGuard] = None,
    ) -> ApplyResult:
        """Execute a previewed change set once.

        ``guard`` sees the previewed changes after the token, expiry and staleness checks;
        a reason it returns rejects the approval without consuming the token.
        """
        try:
            run_id = UUID(str(apply_run_id))
        except ValueError as error:
            raise ApprovalRejectedError("approval run was not found") from error
        run = await self._journal.get_run(run_id)
        if run is None:
            raise ApprovalRejectedError("approval run was not found")
        if run.state == "STALE":
            raise ApprovalRejectedError("approval is no longer valid; create a new preview")
        if run.state != "PREVIEWED":
            raise ApprovalRejectedError("approval token was already consumed")
        if run.plan_id is not None or run.profile_id != AD_HOC_PROFILE_ID:
            raise ApprovalRejectedError("approval is bound to another request")
        payload = await self._journal.preview_payload(run_id)
        raw_changes = None if payload is None else payload.get("changes")
        if payload is None or not isinstance(raw_changes, list):
            raise ApprovalRejectedError("preview payload is unavailable")
        expected = payload.get("approval_digest")
        if not isinstance(expected, str) or not token_matches(approval_token, expected):
            raise ApprovalRejectedError("approval token is invalid")
        expires_raw = payload.get("expires_at")
        event_id = payload.get("event_id")
        if not isinstance(expires_raw, str) or not isinstance(event_id, str):
            raise ApprovalRejectedError("preview payload is unavailable")
        try:
            expires_at = as_utc(datetime.fromisoformat(expires_raw))
        except ValueError as error:
            raise ApprovalRejectedError("preview payload is unavailable") from error
        now = as_utc(self._now())
        if expires_at < now or expires_at > now + APPROVAL_TTL:
            await self._mark_stale(run_id)
            raise ApprovalRejectedError("approval token has expired")
        current = await self._snapshots.capture(event_id, await self._reader.read())
        if current.fingerprint != payload.get("source_fingerprint"):
            await self._mark_stale(run_id)
            raise ApprovalRejectedError("schedule changed after preview; create a new preview")
        try:
            changes = tuple(PlanChange.model_validate(item) for item in raw_changes)
        except ValidationError as error:
            raise ApprovalRejectedError("preview payload is unavailable") from error
        reason = guard(changes) if guard is not None else None
        if reason:
            raise ApprovalRejectedError(reason)  # the run stays PREVIEWED
        try:
            await self._journal.transition_run(run_id, "PREVIEWED", "APPROVED")
            await self._journal.transition_run(run_id, "APPROVED", "APPLYING")
        except InvalidStateTransition as error:
            raise ApprovalRejectedError("approval token was already consumed") from error
        return await execute_and_settle(
            self._journal, self._writer, run_id, changes, allow_destructive=True
        )

    async def _mark_stale(self, run_id: UUID) -> None:
        try:
            await self._journal.transition_run(run_id, "PREVIEWED", "STALE")
        except InvalidStateTransition as error:
            raise ApprovalRejectedError("approval token was already consumed") from error
