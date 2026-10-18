"""ScheduleWriteService: journaled execution, swap, and reconciliation (SPEC-17)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest

from reinvent_concierge.booking.changes import ChangeKind, PlanChange
from reinvent_concierge.booking.service import _OK, ScheduleWriteService, _RunState
from reinvent_concierge.exceptions import (
    AWSEventsAPIError,
    AWSEventsServerError,
    RateLimitError,
    ReservationConflictError,
    ScheduleReadBackError,
    WriteAccessInactiveError,
)
from reinvent_concierge.models import BulkResult, PersonalTimeBlock, ReservationResult
from reinvent_concierge.planner import ApplyJournalStore, PlannerStateStore
from tests.factories import at, make_schedule, make_session


def _swap_service(*schedules):
    reader = Mock()
    reader.read = AsyncMock(side_effect=list(schedules))
    client = Mock()
    client.cancel_reservation = AsyncMock()
    service = ScheduleWriteService(Mock(), client, reader)
    calls = []

    async def remote(run_id, operation_type, request, call):
        calls.append((operation_type, request))
        return await call(), _OK

    service._remote = remote
    return service, calls


def _confirmed(session_id):
    return [SimpleNamespace(status="CONFIRMED", session_id=session_id, code=None)]


@pytest.mark.asyncio
async def test_successful_swap_reserves_target_not_released_session():
    service, calls = _swap_service(make_schedule(make_session("OLD", start=at(9))))
    service._client.reserve_sessions = AsyncMock(return_value=_confirmed("NEW"))
    change = SimpleNamespace(session_id="NEW", blocking_session_ids=("OLD",))

    assert await service._swap(uuid4(), change, _RunState()) is True
    assert calls == [
        ("CANCEL_FOR_SWAP", {"session_id": "OLD"}),
        ("RESERVE_SWAP_TARGET", {"session_id": "NEW"}),
    ]
    service._client.cancel_reservation.assert_awaited_once_with("OLD")
    service._client.reserve_sessions.assert_awaited_once_with(["NEW"])


@pytest.mark.asyncio
async def test_failed_swap_restores_released_reservations_and_reconciles():
    schedule = make_schedule(make_session("OLD", start=at(9)))
    service, calls = _swap_service(schedule, schedule)
    service._client.reserve_sessions = AsyncMock(
        side_effect=[
            [SimpleNamespace(status="FAILED", session_id="NEW", code="full")],
            _confirmed("OLD"),
        ]
    )
    change = SimpleNamespace(session_id="NEW", blocking_session_ids=("OLD",))

    assert await service._swap(uuid4(), change, _RunState()) is True
    assert calls == [
        ("CANCEL_FOR_SWAP", {"session_id": "OLD"}),
        ("RESERVE_SWAP_TARGET", {"session_id": "NEW"}),
        ("RESTORE_SWAP", {"session_id": "OLD"}),
    ]
    assert service._client.reserve_sessions.await_args_list[0].args == (["NEW"],)
    assert service._client.reserve_sessions.await_args_list[1].args == (["OLD"],)


@pytest.mark.asyncio
async def test_multi_blocker_swap_cancels_in_order_and_restores_all_in_order_on_failure():
    schedule = make_schedule(
        make_session("OLD1", start=at(9)), make_session("OLD2", start=at(9, 30))
    )
    service, calls = _swap_service(schedule, schedule)
    service._client.reserve_sessions = AsyncMock(
        side_effect=[
            [SimpleNamespace(status="FAILED", session_id="NEW", code="full")],
            _confirmed("OLD1"),
            _confirmed("OLD2"),
        ]
    )
    change = SimpleNamespace(session_id="NEW", blocking_session_ids=("OLD1", "OLD2"))

    assert await service._swap(uuid4(), change, _RunState()) is True
    assert calls == [
        ("CANCEL_FOR_SWAP", {"session_id": "OLD1"}),
        ("CANCEL_FOR_SWAP", {"session_id": "OLD2"}),
        ("RESERVE_SWAP_TARGET", {"session_id": "NEW"}),
        ("RESTORE_SWAP", {"session_id": "OLD1"}),
        ("RESTORE_SWAP", {"session_id": "OLD2"}),
    ]
    assert [c.args for c in service._client.cancel_reservation.await_args_list] == [
        ("OLD1",),
        ("OLD2",),
    ]
    assert [c.args for c in service._client.reserve_sessions.await_args_list] == [
        (["NEW"],),
        (["OLD1"],),
        (["OLD2"],),
    ]


@pytest.mark.asyncio
async def test_execute_runs_favorites_reserves_and_transit_in_order(config):
    store = PlannerStateStore(config.planner_db_path)
    await store.initialize()
    journal = ApplyJournalStore(store)
    run = await journal.create_run("evt", uuid4(), "fingerprint")
    client = Mock()
    client.associate_favorites = AsyncMock(return_value=BulkResult(succeeded=["S1"], failed=[]))
    client.reserve_sessions = AsyncMock(return_value=_confirmed("S1"))
    client.create_personal_time = AsyncMock(return_value="PT1")
    service = ScheduleWriteService(journal, client, Mock())
    changes = [
        PlanChange(kind=ChangeKind.FAVORITE, session_id="S1", risk="r"),
        PlanChange(kind=ChangeKind.RESERVE, session_id="S1", risk="r"),
        PlanChange(
            kind=ChangeKind.CREATE_TRANSIT,
            title="Transit",
            start_time=at(10),
            end_time=at(10, 30),
            risk="r",
        ),
    ]

    outcome = await service.execute(run.run_id, changes, allow_destructive=True)

    assert outcome.compensation_incomplete is False
    operations = await journal.operations(run.run_id)
    assert [(op.operation_type, op.state) for op in operations] == [
        ("FAVORITE", "CONFIRMED"),
        ("RESERVE", "CONFIRMED"),
        ("CREATE_TRANSIT", "CONFIRMED"),
    ]


def confirmed(session_id):
    return [ReservationResult(session_id=session_id, status="CONFIRMED")]


@pytest.fixture
async def service_parts(config):
    store = PlannerStateStore(config.planner_db_path)
    await store.initialize()
    journal = ApplyJournalStore(store)
    client = Mock()
    for name in (
        "associate_favorites",
        "disassociate_favorite",
        "reserve_sessions",
        "cancel_reservation",
        "create_personal_time",
        "delete_personal_time",
    ):
        setattr(client, name, AsyncMock())
    reader = Mock()
    reader.read = AsyncMock(return_value=make_schedule())
    return journal, client, reader, ScheduleWriteService(journal, client, reader)


async def test_timeout_is_not_retried_and_is_reconciled_from_schedule(service_parts):
    journal, client, reader, service = service_parts
    client.reserve_sessions = AsyncMock(side_effect=httpx.ReadTimeout("slow"))
    reader.read = AsyncMock(return_value=make_schedule(make_session("A")))  # seat landed
    run = await journal.create_run("event", uuid4(), "fp")
    change = PlanChange(kind=ChangeKind.RESERVE, session_id="A", risk="r")

    await service.execute(run.run_id, [change], allow_destructive=True)

    assert client.reserve_sessions.await_count == 1
    [operation] = await journal.operations(run.run_id)
    assert operation.state == "CONFIRMED"


@pytest.mark.parametrize(
    "error",
    [httpx.ConnectError("down"), AWSEventsServerError(503, "x"), AWSEventsAPIError(502, "x")],
)
async def test_other_unknown_outcomes_stay_attempting_until_reconciled(service_parts, error):
    journal, client, reader, service = service_parts
    client.reserve_sessions = AsyncMock(side_effect=error)
    reader.read = AsyncMock(return_value=make_schedule(make_session("A")))  # seat landed
    run = await journal.create_run("event", uuid4(), "fp")

    await service.execute(
        run.run_id, [PlanChange(kind=ChangeKind.RESERVE, session_id="A", risk="r")]
    )

    assert client.reserve_sessions.await_count == 1
    [operation] = await journal.operations(run.run_id)
    assert operation.state == "CONFIRMED"  # misclassified as FAILED would fail here


async def test_unknown_outcome_is_left_attempting_when_reconcile_is_skipped(service_parts):
    journal, client, reader, service = service_parts
    client.reserve_sessions = AsyncMock(side_effect=httpx.ReadTimeout("slow"))
    service.reconcile_unknown = AsyncMock()
    run = await journal.create_run("event", uuid4(), "fp")

    await service.execute(
        run.run_id, [PlanChange(kind=ChangeKind.RESERVE, session_id="A", risk="r")]
    )

    service.reconcile_unknown.assert_awaited_once_with(run.run_id)
    [operation] = await journal.operations(run.run_id)
    assert operation.state == "ATTEMPTING"


async def test_rate_limit_is_refused_as_throttled(service_parts):
    journal, client, reader, service = service_parts
    client.reserve_sessions = AsyncMock(side_effect=RateLimitError(7))
    run = await journal.create_run("event", uuid4(), "fp")

    await service.execute(
        run.run_id, [PlanChange(kind=ChangeKind.RESERVE, session_id="A", risk="r")]
    )

    [operation] = await journal.operations(run.run_id)
    assert operation.state == "REFUSED"
    assert operation.error.startswith("THROTTLED:")


async def test_409_defers_later_reservations_but_keeps_favorites(service_parts):
    journal, client, reader, service = service_parts
    client.reserve_sessions = AsyncMock(side_effect=ReservationConflictError(409, "closed"))
    client.associate_favorites = AsyncMock(return_value=BulkResult(successful=["C"]))
    run = await journal.create_run("event", uuid4(), "fp")
    changes = [
        PlanChange(kind=ChangeKind.RESERVE, session_id="A", risk="r"),
        PlanChange(kind=ChangeKind.RESERVE, session_id="B", risk="r"),
        PlanChange(kind=ChangeKind.FAVORITE, session_id="C", risk="r"),
    ]

    outcome = await service.execute(run.run_id, changes, allow_destructive=True)

    assert client.reserve_sessions.await_count == 1
    assert set(outcome.deferred) == {"A", "B"}
    ops = {op.operation_type: op for op in await journal.operations(run.run_id)}
    assert ops["RESERVE"].state == "REFUSED" and ops["RESERVE"].error.startswith("DEFERRED:")
    assert ops["FAVORITE"].state == "CONFIRMED"


async def test_write_inactive_defers_swap_and_reserve(service_parts):
    journal, client, reader, service = service_parts
    reader.read = AsyncMock(return_value=make_schedule(make_session("OLD")))
    client.cancel_reservation = AsyncMock(side_effect=WriteAccessInactiveError(409, "off"))
    run = await journal.create_run("event", uuid4(), "fp")
    changes = [
        PlanChange(kind=ChangeKind.SWAP, session_id="NEW", blocking_session_ids=("OLD",), risk="r"),
        PlanChange(kind=ChangeKind.RESERVE, session_id="B", risk="r"),
    ]

    outcome = await service.execute(run.run_id, changes, allow_destructive=True)

    assert set(outcome.deferred) == {"NEW", "B"}
    client.reserve_sessions.assert_not_awaited()


async def test_swap_stops_when_a_blocker_cancel_fails(service_parts):
    journal, client, reader, service = service_parts
    reader.read = AsyncMock(return_value=make_schedule(make_session("OLD1"), make_session("OLD2")))
    client.cancel_reservation = AsyncMock(side_effect=[None, AWSEventsAPIError(400, "bad")])
    client.reserve_sessions = AsyncMock(return_value=confirmed("OLD1"))
    run = await journal.create_run("event", uuid4(), "fp")
    change = PlanChange(
        kind=ChangeKind.SWAP, session_id="NEW", blocking_session_ids=("OLD1", "OLD2"), risk="r"
    )

    await service.execute(run.run_id, [change], allow_destructive=True)

    reserved = [call.args[0] for call in client.reserve_sessions.await_args_list]
    assert ["NEW"] not in reserved  # never reserve the target after a failed release
    assert ["OLD1"] in reserved  # restore what was released


async def test_empty_reservation_result_is_not_success(service_parts):
    journal, client, reader, service = service_parts
    reader.read = AsyncMock(return_value=make_schedule(make_session("OLD")))
    client.cancel_reservation = AsyncMock()
    client.reserve_sessions = AsyncMock(side_effect=[[], confirmed("OLD")])
    run = await journal.create_run("event", uuid4(), "fp")
    change = PlanChange(
        kind=ChangeKind.SWAP, session_id="NEW", blocking_session_ids=("OLD",), risk="r"
    )
    await service.execute(run.run_id, [change], allow_destructive=True)
    assert client.reserve_sessions.await_args_list[-1].args == (["OLD"],)


async def test_already_scheduled_and_favorited_codes_count_as_success(service_parts):
    journal, client, reader, service = service_parts
    client.reserve_sessions = AsyncMock(
        return_value=[ReservationResult(session_id="A", status="FAILED", code="alreadyScheduled")]
    )
    run = await journal.create_run("event", uuid4(), "fp")

    await service.execute(
        run.run_id, [PlanChange(kind=ChangeKind.RESERVE, session_id="A", risk="r")]
    )

    [operation] = await journal.operations(run.run_id)
    assert operation.state == "CONFIRMED"


def _op(operation_type, request):
    return SimpleNamespace(operation_type=operation_type, request=request)


def test_resolve_create_transit_confirmed_when_matching_block_exists():
    title = "Transit: Venetian → Wynn"
    block = PersonalTimeBlock(id="P1", title=title, start_time=at(10), end_time=at(10, 30))
    schedule = make_schedule(personal_time=[block])
    request = {"title": title, "start_time": at(10).isoformat()}

    resolve = ScheduleWriteService.resolve_attempting
    assert resolve(_op("CREATE_TRANSIT", request), schedule) == "CONFIRMED"
    assert resolve(_op("CREATE_TRANSIT", request), make_schedule()) == "FAILED"
    other_time = {"title": title, "start_time": at(11).isoformat()}
    assert resolve(_op("CREATE_TRANSIT", other_time), schedule) == "FAILED"


def test_resolve_remove_transit_confirmed_when_block_is_gone():
    block = PersonalTimeBlock(id="P1", title="T", start_time=at(10), end_time=at(10, 30))
    request = {"personal_time_id": "P1"}

    resolve = ScheduleWriteService.resolve_attempting
    assert resolve(_op("REMOVE_TRANSIT", request), make_schedule()) == "CONFIRMED"
    assert resolve(_op("REMOVE_TRANSIT", request), make_schedule(personal_time=[block])) == "FAILED"


def test_verify_reports_unfavorite_still_favorited_as_refused():
    schedule = make_schedule().model_copy(update={"favorites": [make_session("F")]})
    change = PlanChange(kind=ChangeKind.UNFAVORITE, session_id="F", risk="r")

    refused, unexpected = ScheduleWriteService._reconcile([change], schedule)

    assert refused == ("F",)
    assert unexpected == ()
    assert ScheduleWriteService._reconcile([change], make_schedule()) == ((), ())


def _swap_change():
    return PlanChange(
        kind=ChangeKind.SWAP, session_id="NEW", blocking_session_ids=("OLD",), risk="r"
    )


async def _ops(journal, run):
    return {op.operation_type: op.state for op in await journal.operations(run.run_id)}


async def test_unknown_cancel_that_landed_is_confirmed_and_blocker_restored(service_parts):
    journal, client, reader, service = service_parts
    old, empty = make_schedule(make_session("OLD")), make_schedule()
    reader.read = AsyncMock(side_effect=[old, empty, old])  # pre-check, read-back, final
    client.cancel_reservation = AsyncMock(side_effect=httpx.ReadTimeout("slow"))
    client.reserve_sessions = AsyncMock(return_value=confirmed("OLD"))
    run = await journal.create_run("event", uuid4(), "fp")

    outcome = await service.execute(run.run_id, [_swap_change()], allow_destructive=True)

    assert await _ops(journal, run) == {"CANCEL_FOR_SWAP": "CONFIRMED", "RESTORE_SWAP": "CONFIRMED"}
    assert [c.args for c in client.reserve_sessions.await_args_list] == [(["OLD"],)]
    assert outcome.compensation_incomplete is False


async def test_unknown_cancel_that_did_not_land_is_failed_and_nothing_restored(service_parts):
    journal, client, reader, service = service_parts
    old = make_schedule(make_session("OLD"))
    reader.read = AsyncMock(side_effect=[old, old, old])
    client.cancel_reservation = AsyncMock(side_effect=httpx.ReadTimeout("slow"))
    run = await journal.create_run("event", uuid4(), "fp")

    await service.execute(run.run_id, [_swap_change()], allow_destructive=True)

    assert await _ops(journal, run) == {"CANCEL_FOR_SWAP": "FAILED"}
    client.reserve_sessions.assert_not_awaited()


async def test_unknown_target_reserve_that_landed_is_confirmed_without_restore(service_parts):
    journal, client, reader, service = service_parts
    old, landed = make_schedule(make_session("OLD")), make_schedule(make_session("NEW"))
    reader.read = AsyncMock(side_effect=[old, landed, landed])
    client.reserve_sessions = AsyncMock(side_effect=httpx.ReadTimeout("slow"))
    run = await journal.create_run("event", uuid4(), "fp")

    outcome = await service.execute(run.run_id, [_swap_change()], allow_destructive=True)

    assert await _ops(journal, run) == {
        "CANCEL_FOR_SWAP": "CONFIRMED",
        "RESERVE_SWAP_TARGET": "CONFIRMED",
    }
    assert client.reserve_sessions.await_count == 1
    assert outcome.compensation_incomplete is False


async def test_unknown_target_reserve_that_did_not_land_is_failed_and_blockers_restored(
    service_parts,
):
    journal, client, reader, service = service_parts
    old, empty = make_schedule(make_session("OLD")), make_schedule()
    reader.read = AsyncMock(side_effect=[old, empty, old])
    client.reserve_sessions = AsyncMock(side_effect=[httpx.ReadTimeout("slow"), confirmed("OLD")])
    run = await journal.create_run("event", uuid4(), "fp")

    await service.execute(run.run_id, [_swap_change()], allow_destructive=True)

    assert await _ops(journal, run) == {
        "CANCEL_FOR_SWAP": "CONFIRMED",
        "RESERVE_SWAP_TARGET": "FAILED",
        "RESTORE_SWAP": "CONFIRMED",
    }


async def test_missing_reservation_result_is_refused(service_parts):
    journal, client, reader, service = service_parts
    client.reserve_sessions = AsyncMock(side_effect=[[], confirmed("OTHER")])
    run = await journal.create_run("event", uuid4(), "fp")
    changes = [
        PlanChange(kind=ChangeKind.RESERVE, session_id="A", risk="r"),
        PlanChange(kind=ChangeKind.RESERVE, session_id="B", risk="r"),
    ]

    await service.execute(run.run_id, changes, allow_destructive=True)

    states = [op.state for op in await journal.operations(run.run_id)]
    assert states == ["REFUSED", "REFUSED"]


async def test_cancel_continues_after_a_deferred_reservation(service_parts):
    journal, client, reader, service = service_parts
    client.reserve_sessions = AsyncMock(side_effect=ReservationConflictError(409, "closed"))
    run = await journal.create_run("event", uuid4(), "fp")
    changes = [
        PlanChange(kind=ChangeKind.RESERVE, session_id="A", risk="r"),
        PlanChange(kind=ChangeKind.CANCEL, session_id="X", risk="r"),
    ]

    await service.execute(run.run_id, changes, allow_destructive=True)

    client.cancel_reservation.assert_awaited_once_with("X")
    assert await _ops(journal, run) == {"RESERVE": "REFUSED", "CANCEL": "CONFIRMED"}


async def test_read_back_error_after_create_is_reconciled_not_failed(service_parts):
    journal, client, reader, service = service_parts
    title = "Transit: Venetian → Wynn"
    block = PersonalTimeBlock(id="P1", title=title, start_time=at(10), end_time=at(10, 30))
    reader.read = AsyncMock(return_value=make_schedule(personal_time=[block]))
    client.create_personal_time = AsyncMock(side_effect=ScheduleReadBackError("not found"))
    run = await journal.create_run("event", uuid4(), "fp")
    change = PlanChange(
        kind=ChangeKind.CREATE_TRANSIT,
        title=title,
        start_time=at(10),
        end_time=at(10, 30),
        risk="r",
    )

    await service.execute(run.run_id, [change], allow_destructive=True)

    assert client.create_personal_time.await_count == 1
    assert await _ops(journal, run) == {"CREATE_TRANSIT": "CONFIRMED"}


@pytest.mark.parametrize(
    ("kind", "method", "operation_type"),
    [
        (ChangeKind.UNFAVORITE, "disassociate_favorite", "UNFAVORITE"),
        (ChangeKind.CANCEL, "cancel_reservation", "CANCEL"),
    ],
)
async def test_404_on_unfavorite_or_cancel_is_already_done(
    service_parts, kind, method, operation_type
):
    journal, client, reader, service = service_parts
    setattr(client, method, AsyncMock(side_effect=AWSEventsAPIError(404, "not found")))
    run = await journal.create_run("event", uuid4(), "fp")

    await service.execute(
        run.run_id, [PlanChange(kind=kind, session_id="A", risk="r")], allow_destructive=True
    )

    [operation] = await journal.operations(run.run_id)
    assert (operation.operation_type, operation.state) == (operation_type, "CONFIRMED")


async def test_404_on_a_swap_release_is_already_done(service_parts):
    journal, client, reader, service = service_parts
    reader.read = AsyncMock(return_value=make_schedule(make_session("OLD")))
    client.cancel_reservation = AsyncMock(side_effect=AWSEventsAPIError(404, "not found"))
    client.reserve_sessions = AsyncMock(return_value=confirmed("NEW"))
    run = await journal.create_run("event", uuid4(), "fp")
    change = PlanChange(
        kind=ChangeKind.SWAP, session_id="NEW", blocking_session_ids=("OLD",), risk="r"
    )

    await service.execute(run.run_id, [change], allow_destructive=True)

    states = [(op.operation_type, op.state) for op in await journal.operations(run.run_id)]
    assert states == [("CANCEL_FOR_SWAP", "CONFIRMED"), ("RESERVE_SWAP_TARGET", "CONFIRMED")]


@pytest.mark.parametrize(
    ("kind", "method"),
    [(ChangeKind.FAVORITE, "associate_favorites"), (ChangeKind.RESERVE, "reserve_sessions")],
)
async def test_404_on_other_writes_still_fails(service_parts, kind, method):
    journal, client, reader, service = service_parts
    setattr(client, method, AsyncMock(side_effect=AWSEventsAPIError(404, "not found")))
    run = await journal.create_run("event", uuid4(), "fp")

    await service.execute(
        run.run_id, [PlanChange(kind=kind, session_id="A", risk="r")], allow_destructive=True
    )

    [operation] = await journal.operations(run.run_id)
    assert operation.state == "FAILED"


def _already_scheduled(session_id):
    return [ReservationResult(session_id=session_id, status="FAILED", code="alreadyScheduled")]


@pytest.mark.parametrize("read_error", [httpx.ConnectError("down"), RateLimitError(3)])
async def test_unknown_cancel_with_failed_read_back_restores_blocker_and_skips_target(
    service_parts, read_error
):
    """I1 (1): the read-back fails, so the blocker is treated as released and restored."""
    journal, client, reader, service = service_parts
    old = make_schedule(make_session("OLD"))
    # pre-check, read-back (fails), final restore check, end-of-run reconcile
    reader.read = AsyncMock(side_effect=[old, read_error, old, old])
    client.cancel_reservation = AsyncMock(side_effect=httpx.ReadTimeout("slow"))
    client.reserve_sessions = AsyncMock(return_value=_already_scheduled("OLD"))
    run = await journal.create_run("event", uuid4(), "fp")

    outcome = await service.execute(run.run_id, [_swap_change()], allow_destructive=True)

    assert [c.args for c in client.reserve_sessions.await_args_list] == [(["OLD"],)]
    assert await _ops(journal, run) == {"CANCEL_FOR_SWAP": "FAILED", "RESTORE_SWAP": "CONFIRMED"}
    assert outcome.compensation_incomplete is False


async def test_unknown_cancel_with_failed_read_back_and_final_read_is_incomplete(service_parts):
    """I1 (2): when the restore cannot be checked either, compensation is incomplete."""
    journal, client, reader, service = service_parts
    old = make_schedule(make_session("OLD"))
    down = httpx.ConnectError("down")
    reader.read = AsyncMock(side_effect=[old, down, down, old])
    client.cancel_reservation = AsyncMock(side_effect=httpx.ReadTimeout("slow"))
    client.reserve_sessions = AsyncMock(return_value=_already_scheduled("OLD"))
    run = await journal.create_run("event", uuid4(), "fp")

    outcome = await service.execute(run.run_id, [_swap_change()], allow_destructive=True)

    assert outcome.compensation_incomplete is True
    assert [c.args for c in client.reserve_sessions.await_args_list] == [(["OLD"],)]


async def test_two_swaps_sharing_a_blocker_release_it_once_and_reserve_both(service_parts):
    """I3: X overlaps targets A and B; the second swap finds X already released."""
    journal, client, reader, service = service_parts
    x = make_session("X", start=at(9), minutes=180)
    before = make_schedule(x, make_session("Y", start=at(15)))
    after_a = make_schedule(make_session("A", start=at(9)), make_session("Y", start=at(15)))
    reader.read = AsyncMock(side_effect=[before, after_a])
    client.reserve_sessions = AsyncMock(side_effect=[confirmed("A"), confirmed("B")])
    run = await journal.create_run("event", uuid4(), "fp")
    changes = [
        PlanChange(kind=ChangeKind.SWAP, session_id="A", blocking_session_ids=("X",), risk="r"),
        PlanChange(kind=ChangeKind.SWAP, session_id="B", blocking_session_ids=("X",), risk="r"),
        PlanChange(kind=ChangeKind.CANCEL, session_id="Y", risk="r"),
    ]

    outcome = await service.execute(run.run_id, changes, allow_destructive=True)

    assert outcome.compensation_incomplete is False
    assert [c.args for c in client.reserve_sessions.await_args_list] == [(["A"],), (["B"],)]
    assert [c.args for c in client.cancel_reservation.await_args_list] == [("X",), ("Y",)]
    states = [(op.operation_type, op.state) for op in await journal.operations(run.run_id)]
    assert states == [
        ("CANCEL_FOR_SWAP", "CONFIRMED"),
        ("RESERVE_SWAP_TARGET", "CONFIRMED"),
        ("RESERVE_SWAP_TARGET", "CONFIRMED"),
        ("CANCEL", "CONFIRMED"),
    ]


async def test_swap_releases_only_the_blockers_still_booked(service_parts):
    journal, client, reader, service = service_parts
    reader.read = AsyncMock(return_value=make_schedule(make_session("OLD2")))
    client.reserve_sessions = AsyncMock(return_value=confirmed("NEW"))
    run = await journal.create_run("event", uuid4(), "fp")
    change = PlanChange(
        kind=ChangeKind.SWAP, session_id="NEW", blocking_session_ids=("OLD1", "OLD2"), risk="r"
    )

    outcome = await service.execute(run.run_id, [change], allow_destructive=True)

    assert outcome.compensation_incomplete is False
    client.cancel_reservation.assert_awaited_once_with("OLD2")
    client.reserve_sessions.assert_awaited_once_with(["NEW"])


async def test_swap_whose_target_is_already_booked_is_refused_without_release(service_parts):
    """I3: nothing is released when the target is already held."""
    journal, client, reader, service = service_parts
    reader.read = AsyncMock(return_value=make_schedule(make_session("NEW"), make_session("OLD")))
    run = await journal.create_run("event", uuid4(), "fp")

    outcome = await service.execute(run.run_id, [_swap_change()], allow_destructive=True)

    assert outcome.compensation_incomplete is False
    client.cancel_reservation.assert_not_awaited()
    client.reserve_sessions.assert_not_awaited()
    [operation] = await journal.operations(run.run_id)
    assert (operation.operation_type, operation.state) == ("RESERVE_SWAP_TARGET", "REFUSED")
    assert operation.result == {"failures": [{"session_id": "NEW", "code": "alreadyScheduled"}]}


@pytest.mark.parametrize(
    "change",
    [
        _swap_change(),
        PlanChange(kind=ChangeKind.CANCEL, session_id="A", risk="r"),
        PlanChange(kind=ChangeKind.REMOVE_TRANSIT, personal_time_id="PT", risk="r"),
    ],
)
async def test_execute_refuses_destructive_changes_unless_allowed(service_parts, change):
    """M5: defense in depth — destructive kinds need an explicit allow_destructive."""
    journal, client, reader, service = service_parts
    run = await journal.create_run("event", uuid4(), "fp")
    favorite = PlanChange(kind=ChangeKind.FAVORITE, session_id="F", risk="r")

    with pytest.raises(ValueError, match="destructive"):
        await service.execute(run.run_id, [favorite, change])

    assert await journal.operations(run.run_id) == []
    client.associate_favorites.assert_not_awaited()
    client.cancel_reservation.assert_not_awaited()
    client.delete_personal_time.assert_not_awaited()
