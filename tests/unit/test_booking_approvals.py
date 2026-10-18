"""ChangeSetApprovals: ad-hoc change sets behind single-use approval tokens (SPEC-17)."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest

from reinvent_concierge.booking.approvals import (
    AD_HOC_PROFILE_ID,
    ApprovalRejectedError,
    ChangeSetApprovals,
    DestructiveChangeError,
    digest,
)
from reinvent_concierge.booking.changes import ChangeKind, PlanChange
from reinvent_concierge.booking.service import ScheduleWriteService
from reinvent_concierge.exceptions import ReservationConflictError
from reinvent_concierge.models import BulkFailure, BulkResult, ReservationResult
from reinvent_concierge.planner import (
    ApplyJournalStore,
    PlannerStateStore,
    ScheduleSnapshotService,
)
from tests.factories import make_schedule, make_session


class _Clock:
    def __init__(self) -> None:
        self._now = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, minutes: int) -> None:
        self._now += timedelta(minutes=minutes)


@pytest.fixture
def clock():
    return _Clock()


@pytest.fixture
def reader():
    reader = Mock()
    reader.read = AsyncMock(return_value=make_schedule(make_session("A")))
    return reader


@pytest.fixture
def client():
    client = Mock()
    client.cancel_reservation = AsyncMock()
    client.associate_favorites = AsyncMock()
    return client


@pytest.fixture
async def journal(config):
    store = PlannerStateStore(config.planner_db_path)
    await store.initialize()
    return ApplyJournalStore(store), store


@pytest.fixture
async def approvals(journal, client, reader, clock):
    journal_store, store = journal
    writer = ScheduleWriteService(journal_store, client, reader)
    return ChangeSetApprovals(
        journal_store, ScheduleSnapshotService(store), reader, writer, now=clock.now
    )


def _cancel():
    return PlanChange(kind=ChangeKind.CANCEL, session_id="A", risk="r")


@pytest.mark.asyncio
async def test_apply_immediately_refuses_destructive_changes(approvals):
    with pytest.raises(DestructiveChangeError):
        await approvals.apply_immediately("event", [_cancel()])


@pytest.mark.asyncio
async def test_apply_immediately_executes_non_destructive_changes(approvals, client):
    client.associate_favorites.return_value = BulkResult(successful=["B"])
    result = await approvals.apply_immediately(
        "event", [PlanChange(kind=ChangeKind.FAVORITE, session_id="B", risk="r")]
    )
    client.associate_favorites.assert_awaited_once_with(["B"])
    assert result.state in {"APPLIED", "PARTIAL"}


@pytest.mark.asyncio
async def test_preview_then_apply_executes_once_and_rejects_replay(approvals, client):
    preview = await approvals.preview("event", [_cancel()])
    client.cancel_reservation.assert_not_awaited()

    result = await approvals.apply(preview.apply_run_id, preview.approval_token)
    assert result.state in {"APPLIED", "PARTIAL"}
    client.cancel_reservation.assert_awaited_once_with("A")

    with pytest.raises(ApprovalRejectedError, match="consumed"):
        await approvals.apply(preview.apply_run_id, preview.approval_token)
    client.cancel_reservation.assert_awaited_once()


@pytest.mark.asyncio
async def test_wrong_token_and_expired_token_are_rejected(approvals, clock):
    preview = await approvals.preview("event", [_cancel()])
    with pytest.raises(ApprovalRejectedError, match="invalid"):
        await approvals.apply(preview.apply_run_id, "not-the-token")
    clock.advance(minutes=6)
    with pytest.raises(ApprovalRejectedError, match="expired"):
        await approvals.apply(preview.apply_run_id, preview.approval_token)


@pytest.mark.asyncio
async def test_schedule_change_after_preview_marks_stale(approvals, reader, client):
    preview = await approvals.preview("event", [_cancel()])
    reader.read = AsyncMock(return_value=make_schedule(make_session("A"), make_session("B")))
    with pytest.raises(ApprovalRejectedError, match="schedule changed"):
        await approvals.apply(preview.apply_run_id, preview.approval_token)
    client.cancel_reservation.assert_not_awaited()


@pytest.mark.asyncio
async def test_journal_stores_only_the_token_digest(approvals, journal):
    preview = await approvals.preview("event", [_cancel()])
    payload = await journal[0].preview_payload(preview.apply_run_id)
    assert preview.approval_token not in json.dumps(payload)


async def _runs(store, event_id):
    async with store._connect() as connection:
        cursor = await connection.execute(
            "SELECT state FROM apply_runs WHERE event_id = ?", (event_id,)
        )
        return [row[0] for row in await cursor.fetchall()]


@pytest.mark.asyncio
async def test_apply_immediately_maps_deferred_codes(approvals, client):
    client.reserve_sessions = AsyncMock(side_effect=ReservationConflictError(409, "closed"))
    result = await approvals.apply_immediately(
        "event", [PlanChange(kind=ChangeKind.RESERVE, session_id="B", risk="r")]
    )
    assert result.deferred == ("B",)
    assert result.state == "PARTIAL"


@pytest.mark.asyncio
async def test_apply_immediately_maps_refusal_codes(approvals, client):
    client.associate_favorites.return_value = BulkResult(
        failed=[BulkFailure(session_id="X", code="notFound")]
    )
    result = await approvals.apply_immediately(
        "event", [PlanChange(kind=ChangeKind.FAVORITE, session_id="X", risk="r")]
    )
    assert result.refusal_codes == (("X", "notFound"),)


@pytest.mark.asyncio
async def test_mixed_destructive_set_is_rejected_before_any_run_or_write(
    approvals, client, journal
):
    with pytest.raises(DestructiveChangeError):
        await approvals.apply_immediately(
            "event",
            [PlanChange(kind=ChangeKind.FAVORITE, session_id="B", risk="r"), _cancel()],
        )
    client.associate_favorites.assert_not_awaited()
    client.cancel_reservation.assert_not_awaited()
    assert await _runs(journal[1], "event") == []


@pytest.mark.asyncio
async def test_plan_run_cannot_be_applied_through_ad_hoc_path(approvals, client, journal):
    store = journal[0]
    run = await store.create_run("event", uuid4(), "fp", uuid4())
    await store.save_preview_payload(
        run.run_id,
        {
            "changes": [_cancel().model_dump(mode="json")],
            "approval_digest": digest("tok"),
            "expires_at": (_Clock().now() + timedelta(minutes=5)).isoformat(),
            "source_fingerprint": "fp",
            "event_id": "event",
        },
    )
    with pytest.raises(ApprovalRejectedError, match="another request"):
        await approvals.apply(run.run_id, "tok")
    client.cancel_reservation.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_ad_hoc_profile_run_is_rejected(approvals, client, journal):
    run = await journal[0].create_run("event", uuid4(), "fp")
    assert run.profile_id != AD_HOC_PROFILE_ID
    with pytest.raises(ApprovalRejectedError, match="another request"):
        await approvals.apply(run.run_id, "tok")


@pytest.mark.asyncio
async def test_preview_repr_does_not_leak_the_token(approvals):
    preview = await approvals.preview("event", [_cancel()])
    assert preview.approval_token not in repr(preview)


@pytest.mark.asyncio
async def test_stale_run_retry_says_no_longer_valid(approvals, clock, journal):
    preview = await approvals.preview("event", [_cancel()])
    clock.advance(minutes=6)
    with pytest.raises(ApprovalRejectedError, match="expired"):
        await approvals.apply(preview.apply_run_id, preview.approval_token)
    assert (await journal[0].get_run(preview.apply_run_id)).state == "STALE"
    with pytest.raises(ApprovalRejectedError, match="no longer valid"):
        await approvals.apply(preview.apply_run_id, preview.approval_token)


@pytest.mark.asyncio
async def test_fingerprint_mismatch_leaves_run_stale(approvals, reader, journal):
    preview = await approvals.preview("event", [_cancel()])
    reader.read = AsyncMock(return_value=make_schedule(make_session("A"), make_session("B")))
    with pytest.raises(ApprovalRejectedError, match="schedule changed"):
        await approvals.apply(preview.apply_run_id, preview.approval_token)
    assert (await journal[0].get_run(preview.apply_run_id)).state == "STALE"


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [None, "nope", {"a": 1}])
async def test_payload_without_list_changes_is_rejected_before_transition(
    approvals, client, journal, changes
):
    run = await journal[0].create_run("event", AD_HOC_PROFILE_ID, "fp")
    payload = {
        "approval_digest": digest("tok"),
        "expires_at": (_Clock().now() + timedelta(minutes=5)).isoformat(),
        "source_fingerprint": "fp",
        "event_id": "event",
    }
    if changes is not None:
        payload["changes"] = changes
    await journal[0].save_preview_payload(run.run_id, payload)
    with pytest.raises(ApprovalRejectedError, match="payload is unavailable"):
        await approvals.apply(run.run_id, "tok")
    assert (await journal[0].get_run(run.run_id)).state == "PREVIEWED"


@pytest.mark.asyncio
async def test_malformed_expiry_is_rejected(approvals, journal):
    run = await journal[0].create_run("event", AD_HOC_PROFILE_ID, "fp")
    await journal[0].save_preview_payload(
        run.run_id,
        {
            "changes": [],
            "approval_digest": digest("tok"),
            "expires_at": "not-a-date",
            "source_fingerprint": "fp",
            "event_id": "event",
        },
    )
    with pytest.raises(ApprovalRejectedError, match="payload is unavailable"):
        await approvals.apply(run.run_id, "tok")


@pytest.mark.asyncio
async def test_guard_rejection_does_not_consume_the_token(approvals, client, journal):
    preview = await approvals.preview("event", [_cancel()])
    seen = []

    def refuse(changes):
        seen.append(changes)
        return "approval is for another change"

    with pytest.raises(ApprovalRejectedError, match="another change") as error:
        await approvals.apply(preview.apply_run_id, preview.approval_token, guard=refuse)

    assert seen == [(_cancel(),)]
    assert preview.approval_token not in str(error.value)
    client.cancel_reservation.assert_not_awaited()
    assert (await journal[0].get_run(preview.apply_run_id)).state == "PREVIEWED"
    result = await approvals.apply(
        preview.apply_run_id, preview.approval_token, guard=lambda changes: None
    )
    assert result.state in {"APPLIED", "PARTIAL"}
    client.cancel_reservation.assert_awaited_once_with("A")


@pytest.mark.asyncio
async def test_guard_runs_only_after_the_token_is_verified(approvals, client):
    preview = await approvals.preview("event", [_cancel()])
    guard = Mock(return_value=None)

    with pytest.raises(ApprovalRejectedError, match="invalid"):
        await approvals.apply(preview.apply_run_id, "not-the-token", guard=guard)

    guard.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("run_id", ["not-a-uuid", str(uuid4())])
async def test_unknown_or_malformed_run_is_an_approval_rejection(approvals, run_id):
    with pytest.raises(ApprovalRejectedError, match="approval run was not found"):
        await approvals.apply(run_id, "token")


def _swap():
    return PlanChange(
        kind=ChangeKind.SWAP, session_id="NEW", blocking_session_ids=("OLD",), risk="r"
    )


def _reservation(session_id, status="CONFIRMED", code=None):
    return [ReservationResult(session_id=session_id, status=status, code=code)]


async def _run_state(journal, run_id):
    return (await journal[0].get_run(run_id)).state


@pytest.mark.asyncio
async def test_swap_whose_reads_fail_mid_flight_ends_compensation_incomplete(
    approvals, client, reader, journal
):
    """I1 (3): an exception out of execute never leaves the run APPLYING."""
    old = make_schedule(make_session("OLD"))
    down = httpx.ConnectError("down")
    # preview, apply fingerprint, swap pre-check, then every later read fails
    reader.read = AsyncMock(side_effect=[old, old, old, down, down, down])
    client.cancel_reservation = AsyncMock(side_effect=httpx.ReadTimeout("slow"))
    client.reserve_sessions = AsyncMock(
        return_value=_reservation("OLD", "FAILED", "alreadyScheduled")
    )
    preview = await approvals.preview("event", [_swap()])

    result = await approvals.apply(preview.apply_run_id, preview.approval_token)

    assert result.state == "COMPENSATION_INCOMPLETE"
    assert await _run_state(journal, preview.apply_run_id) == "COMPENSATION_INCOMPLETE"
    assert any(action.startswith("OLD") for action in result.manual_actions)
    assert [c.args for c in client.reserve_sessions.await_args_list] == [(["OLD"],)]


@pytest.mark.asyncio
async def test_unknown_reserve_whose_reconcile_read_fails_ends_failed(
    approvals, client, reader, journal
):
    """I1 (3): no swap in flight, so the run is FAILED with the session named."""
    reader.read = AsyncMock(side_effect=[make_schedule(), httpx.ConnectError("down")])
    client.reserve_sessions = AsyncMock(side_effect=httpx.ReadTimeout("slow"))

    result = await approvals.apply_immediately(
        "event", [PlanChange(kind=ChangeKind.RESERVE, session_id="R", risk="r")]
    )

    assert result.state == "FAILED"
    assert await _run_state(journal, result.apply_run_id) == "FAILED"
    assert any(action.startswith("R:") for action in result.manual_actions)


@pytest.mark.asyncio
async def test_verify_read_failure_ends_failed(approvals, client, reader, journal):
    reader.read = AsyncMock(side_effect=[make_schedule(), httpx.ConnectError("down")])
    client.associate_favorites.return_value = BulkResult(successful=["B"])

    result = await approvals.apply_immediately(
        "event", [PlanChange(kind=ChangeKind.FAVORITE, session_id="B", risk="r")]
    )

    assert result.state == "FAILED"
    assert await _run_state(journal, result.apply_run_id) == "FAILED"
    assert result.manual_actions


@pytest.mark.asyncio
async def test_failed_restore_names_the_released_session(approvals, client, reader):
    """M1: the manual action says which released seat could not be restored."""
    old = make_schedule(make_session("OLD"))
    reader.read = AsyncMock(side_effect=[old, old, old, make_schedule()])
    client.reserve_sessions = AsyncMock(
        side_effect=[_reservation("NEW", "FAILED", "full"), _reservation("OLD", "FAILED", "full")]
    )
    preview = await approvals.preview("event", [_swap()])

    result = await approvals.apply(preview.apply_run_id, preview.approval_token)

    assert result.state == "COMPENSATION_INCOMPLETE"
    assert (
        "OLD was released and could not be restored; reserve it again if seats remain."
        in result.manual_actions
    )
