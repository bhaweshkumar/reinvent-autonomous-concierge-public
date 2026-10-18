"""Focused SPEC-12 application safety cases; intentionally mock-only."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from reinvent_concierge.booking.approvals import digest
from reinvent_concierge.booking.changes import ChangeKind
from reinvent_concierge.models import PersonalTimeBlock
from reinvent_concierge.planner.application import (
    ApplicationMode,
    ApprovalRejectedError,
    PlanApplicationService,
)
from reinvent_concierge.planner.itinerary import PlanItem, PlanItemKind
from tests.factories import at, make_schedule, make_session


def _catalog(sessions=(), counts=None):
    by_id = {session.session_id: session for session in sessions}
    catalog = Mock()
    catalog.get_session_local = AsyncMock(side_effect=lambda sid: by_id.get(sid))
    catalog.repeat_group_counts = AsyncMock(return_value=counts or {})
    return catalog


def _service(*, dry_run=False, reader=None, writer=None):
    journal = Mock()
    journal.transition_run = AsyncMock()
    return PlanApplicationService(
        Mock(),
        Mock(),
        journal,
        reader if reader is not None else Mock(),
        writer if writer is not None else Mock(),
        Mock(),
        dry_run=dry_run,
        catalog=_catalog(),
    )


async def test_schedule_is_read_through_the_reader():
    reader = Mock()
    reader.read = AsyncMock(return_value=make_schedule(make_session("A")))
    service = _service(reader=reader)

    schedule = await service._schedule()

    reader.read.assert_awaited_once_with()
    assert schedule is reader.read.return_value


def test_dry_run_forces_favorites_only_for_write_requests():
    service = _service(dry_run=True)

    assert (
        service._effective_mode(ApplicationMode.APPLY_APPROVED_PLAN)
        is ApplicationMode.FAVORITES_ONLY
    )
    assert (
        service._effective_mode(ApplicationMode.RESERVE_NON_CONFLICTING)
        is ApplicationMode.FAVORITES_ONLY
    )


def test_personal_time_ownership_only_matches_concierge_prefix():
    service = _service()
    personal = PersonalTimeBlock(
        id="personal", title="Transit: Venetian to Wynn", start_time=at(10), end_time=at(11)
    )
    owned = PersonalTimeBlock(
        id="owned",
        title="Transit: Venetian to Wynn",
        start_time=at(10),
        end_time=at(11),
        description="concierge-transit:prior-plan",
    )
    item = PlanItem(
        kind=PlanItemKind.TRANSIT,
        title=owned.title,
        start_time=owned.start_time,
        end_time=owned.end_time,
    )

    assert service._same_transit(item, owned)
    assert personal.description is None


@pytest.mark.asyncio
async def test_tampered_approval_token_is_rejected_before_any_write():
    service = _service()
    plan = SimpleNamespace(plan_id=uuid4(), plan_fingerprint="plan")
    run = SimpleNamespace(run_id=uuid4())
    service._journal.latest_preview_for_plan = AsyncMock(return_value=run)
    service._journal.preview_payload = AsyncMock(
        return_value={
            "plan_id": str(plan.plan_id),
            "plan_fingerprint": "plan",
            "requested_mode": "FAVORITES_ONLY",
            "approval_digest": digest("right"),
            "expires_at": (at(0) + timedelta(days=1)).isoformat(),
        }
    )

    with pytest.raises(ApprovalRejectedError, match="invalid"):
        await service._matching_preview(plan, ApplicationMode.FAVORITES_ONLY, "wrong")


@pytest.mark.asyncio
async def test_expired_approval_is_marked_stale_before_any_write():
    service = _service()
    plan = SimpleNamespace(plan_id=uuid4(), plan_fingerprint="plan")
    run = SimpleNamespace(run_id=uuid4())
    service._journal.latest_preview_for_plan = AsyncMock(return_value=run)
    service._journal.preview_payload = AsyncMock(
        return_value={
            "plan_id": str(plan.plan_id),
            "plan_fingerprint": "plan",
            "requested_mode": "FAVORITES_ONLY",
            "approval_digest": digest("token"),
            "expires_at": (at(0) - timedelta(days=1)).isoformat(),
        }
    )

    with pytest.raises(ApprovalRejectedError, match="expired"):
        await service._matching_preview(plan, ApplicationMode.FAVORITES_ONLY, "token")
    service._journal.transition_run.assert_awaited_once_with(run.run_id, "PREVIEWED", "STALE")


def test_legacy_conflict_manager_transit_is_recognized_as_owned():
    from reinvent_concierge.booking.transit_blocks import is_concierge_transit

    legacy = PersonalTimeBlock(
        id="legacy",
        title="Transit: Venetian to Wynn",
        start_time=at(10),
        end_time=at(11),
        description="Travel 10 min from Venetian to Wynn for X",
    )
    item = PlanItem(
        kind=PlanItemKind.TRANSIT,
        title="Transit: Venetian → Wynn",
        start_time=at(10),
        end_time=at(11),
    )

    assert is_concierge_transit(legacy)
    assert PlanApplicationService._same_transit(item, legacy)


def test_changes_treats_owned_legacy_titled_block_as_already_present():
    service = _service()
    owned = PersonalTimeBlock(
        id="owned",
        title="Transit: Venetian to Wynn",
        start_time=at(10),
        end_time=at(11),
        description="concierge-transit:prior-plan:Transit: Venetian to Wynn",
    )
    item = PlanItem(
        kind=PlanItemKind.TRANSIT,
        title="Transit: Venetian → Wynn",
        start_time=at(10),
        end_time=at(11),
    )
    plan = SimpleNamespace(day_items=((item,),), plan_fingerprint="plan")
    profile = SimpleNamespace(must_attend_session_ids=())
    schedule = make_schedule()
    schedule.personal_time.append(owned)

    changes = service._changes(plan, profile, schedule)

    assert [c.kind for c in changes] == []


async def _real_journal_service(config, changes, reads, client, *, must_attend=(), profile=None):
    from reinvent_concierge.booking.service import ScheduleWriteService
    from reinvent_concierge.planner import ApplyJournalStore, PlannerStateStore

    store = PlannerStateStore(config.planner_db_path)
    await store.initialize()
    journal = ApplyJournalStore(store)
    profile_id = uuid4()
    run = await journal.create_run("evt", profile_id, "fp")
    reader = Mock()
    reader.read = AsyncMock(side_effect=list(reads))
    writer = ScheduleWriteService(journal, client, reader)
    snapshots = Mock()
    snapshots.capture = AsyncMock(return_value=SimpleNamespace(fingerprint="fp"))
    profiles = Mock()
    profiles.get = AsyncMock(
        return_value=profile
        or SimpleNamespace(profile_id=profile_id, must_attend_session_ids=tuple(must_attend))
    )
    service = PlanApplicationService(
        Mock(), snapshots, journal, reader, writer, profiles, dry_run=False, catalog=_catalog()
    )
    service._plan = AsyncMock(return_value=SimpleNamespace(event_id="evt", profile_id=profile_id))
    payload = SimpleNamespace(
        source_fingerprint="fp",
        effective_mode=ApplicationMode.APPLY_APPROVED_PLAN.value,
        changes=tuple(change.model_dump(mode="json") for change in changes),
    )
    service._matching_preview = AsyncMock(return_value=(run, payload))
    return service, journal, run


async def _apply_with_real_journal(config, changes, reads, client):
    service, _journal, _run = await _real_journal_service(config, changes, reads, client)
    return await service.apply(uuid4(), ApplicationMode.APPLY_APPROVED_PLAN, "tok")


def _confirmed(session_id):
    return [SimpleNamespace(status="CONFIRMED", session_id=session_id, code=None)]


def _failed(session_id):
    return [SimpleNamespace(status="FAILED", session_id=session_id, code="full")]


async def test_apply_end_states_applied_partial_and_compensation_incomplete(config):
    from reinvent_concierge.booking.changes import ChangeKind, PlanChange
    from reinvent_concierge.models import BulkResult

    def sched(*ids):
        return make_schedule(*[make_session(i, start=at(9 + n)) for n, i in enumerate(ids)])

    client = Mock()
    client.associate_favorites = AsyncMock(return_value=BulkResult(successful=["F"], failed=[]))
    client.reserve_sessions = AsyncMock(return_value=_confirmed("R"))
    after = sched("R").model_copy(update={"favorites": [make_session("F")]})
    applied = await _apply_with_real_journal(
        config,
        [
            PlanChange(kind=ChangeKind.FAVORITE, session_id="F", risk="r"),
            PlanChange(kind=ChangeKind.RESERVE, session_id="R", risk="r"),
        ],
        [sched(), after],
        client,
    )
    assert applied.state == "APPLIED"

    client = Mock()
    client.reserve_sessions = AsyncMock(return_value=_failed("R"))
    partial = await _apply_with_real_journal(
        config,
        [PlanChange(kind=ChangeKind.RESERVE, session_id="R", risk="r")],
        [sched(), sched()],
        client,
    )
    assert partial.state == "PARTIAL"
    assert partial.refused == ("R",)

    client = Mock()
    client.cancel_reservation = AsyncMock()
    client.reserve_sessions = AsyncMock(
        side_effect=[_failed("NEW"), _failed("OLD"), _confirmed("R2")]
    )
    incomplete = await _apply_with_real_journal(
        config,
        [
            PlanChange(
                kind=ChangeKind.SWAP, session_id="NEW", blocking_session_ids=("OLD",), risk="r"
            ),
            PlanChange(kind=ChangeKind.RESERVE, session_id="R2", risk="r"),
            PlanChange(kind=ChangeKind.CANCEL, session_id="X", risk="r"),
        ],
        [sched(), sched("OLD"), sched()],
        client,
    )
    assert incomplete.state == "COMPENSATION_INCOMPLETE"
    client.cancel_reservation.assert_awaited_once_with("OLD")  # later CANCEL skipped
    assert [op.operation_type for op in incomplete.operations][-1] == "RESERVE"


@pytest.mark.asyncio
async def test_apply_maps_execute_deferred_into_result(config):
    from reinvent_concierge.booking.changes import ChangeKind, PlanChange
    from reinvent_concierge.booking.service import ScheduleWriteService
    from reinvent_concierge.exceptions import ReservationConflictError
    from reinvent_concierge.planner import ApplyJournalStore, PlannerStateStore
    from reinvent_concierge.planner.application import PreviewPayload

    store = PlannerStateStore(config.planner_db_path)
    await store.initialize()
    journal = ApplyJournalStore(store)
    client = Mock()
    client.reserve_sessions = AsyncMock(side_effect=ReservationConflictError(409, "closed"))
    reader = Mock()
    reader.read = AsyncMock(return_value=make_schedule())
    writer = ScheduleWriteService(journal, client, reader)
    snapshots = Mock()
    snapshots.capture = AsyncMock(return_value=SimpleNamespace(fingerprint="fp"))
    profile_id = uuid4()
    profiles = Mock()
    profiles.get = AsyncMock(
        return_value=SimpleNamespace(profile_id=profile_id, must_attend_session_ids=())
    )
    service = PlanApplicationService(
        Mock(), snapshots, journal, reader, writer, profiles, dry_run=False, catalog=_catalog()
    )
    run = await journal.create_run("event", profile_id, "fp", uuid4())
    payload = PreviewPayload(
        plan_id=str(uuid4()),
        requested_mode="APPLY_APPROVED_PLAN",
        effective_mode="APPLY_APPROVED_PLAN",
        source_fingerprint="fp",
        plan_fingerprint="plan",
        approval_digest=digest("t"),
        expires_at=at(0),
        changes=(
            PlanChange(kind=ChangeKind.RESERVE, session_id="B", risk="r").model_dump(mode="json"),
        ),
    )
    service._plan = AsyncMock(return_value=SimpleNamespace(event_id="event", profile_id=profile_id))
    service._matching_preview = AsyncMock(return_value=(run, payload))

    result = await service.apply(uuid4(), ApplicationMode.APPLY_APPROVED_PLAN, "t")

    assert result.deferred == ("B",)


def _scarcity_plan_items():
    return tuple(
        PlanItem(
            kind=PlanItemKind.SESSION,
            title=sid,
            session_id=sid,
            start_time=at(9 + index),
            end_time=at(10 + index),
        )
        for index, sid in enumerate(["X", "Y", "Z"])
    )


@pytest.mark.asyncio
async def test_reservations_are_ordered_scarce_and_hands_on_first():
    sessions = [
        make_session("X", repeat_group="GX", session_type="Breakout session"),
        make_session("Y", repeat_group="GY", session_type="Breakout session"),
        make_session("Z", repeat_group="GZ", session_type="Workshop"),
    ]
    catalog = _catalog(sessions, {"GX": 3, "GY": 1, "GZ": 1})
    service = _service()
    service._catalog = catalog
    plan = SimpleNamespace(
        plan_id=uuid4(),
        event_id="evt",
        day_items=(_scarcity_plan_items(),),
        plan_fingerprint="plan",
    )
    profile = SimpleNamespace(must_attend_session_ids=(), profile_id=uuid4())
    schedule = make_schedule()

    scarcity = await service._scarcity(plan)
    changes = service._changes(plan, profile, schedule, scarcity)

    reserves = [c.session_id for c in changes if c.kind is ChangeKind.RESERVE]
    assert reserves == ["Z", "Y", "X"]
    last_favorite = max(i for i, c in enumerate(changes) if c.kind is ChangeKind.FAVORITE)
    first_reserve = min(i for i, c in enumerate(changes) if c.kind is ChangeKind.RESERVE)
    assert last_favorite < first_reserve
    catalog.repeat_group_counts.assert_awaited_once()


def test_changes_order_kinds_favorite_reserve_cancel_create_remove():
    service = _service()
    booked_old = make_session("OLD", start=at(14))
    owned = PersonalTimeBlock(
        id="owned",
        title="Transit: Old to Older",
        start_time=at(16),
        end_time=at(17),
        description="concierge-transit:prior-plan:Transit: Old to Older",
    )
    items = (
        PlanItem(
            kind=PlanItemKind.TRANSIT,
            title="Transit: A to B",
            start_time=at(10),
            end_time=at(11),
        ),
        PlanItem(
            kind=PlanItemKind.SESSION,
            title="N",
            session_id="N",
            start_time=at(11),
            end_time=at(12),
        ),
    )
    plan = SimpleNamespace(day_items=(items,), plan_fingerprint="plan")
    profile = SimpleNamespace(must_attend_session_ids=())
    schedule = make_schedule(booked_old, personal_time=[owned])

    changes = service._changes(plan, profile, schedule, {})

    assert [c.kind for c in changes] == [
        ChangeKind.FAVORITE,
        ChangeKind.RESERVE,
        ChangeKind.CANCEL,
        ChangeKind.CREATE_TRANSIT,
        ChangeKind.REMOVE_TRANSIT,
    ]


def _plan_with_session(session_id, start, end):
    item = PlanItem(
        kind=PlanItemKind.SESSION,
        title=session_id,
        session_id=session_id,
        start_time=start,
        end_time=end,
    )
    return SimpleNamespace(day_items=((item,),), plan_fingerprint="plan")


def test_changes_reserve_instead_of_swap_when_the_blocker_is_must_attend():
    """Regression guard (I2): a must-attend blocker is never proposed for release."""
    service = _service()
    plan = _plan_with_session("NEW", at(9), at(10))
    profile = SimpleNamespace(must_attend_session_ids=("MUST",))
    schedule = make_schedule(make_session("MUST", start=at(9)))

    changes = service._changes(plan, profile, schedule, {})

    [reserve] = [c for c in changes if c.session_id == "NEW" and c.kind is not ChangeKind.FAVORITE]
    assert reserve.kind is ChangeKind.RESERVE
    assert reserve.blocking_session_ids == ()
    assert reserve.risk == (
        "Not reserved automatically because it conflicts with must-attend MUST."
    )


def test_changes_never_cancel_a_booked_must_attend_outside_the_plan():
    """Regression guard (I2)."""
    service = _service()
    plan = _plan_with_session("NEW", at(9), at(10))
    profile = SimpleNamespace(must_attend_session_ids=("MUST",))
    schedule = make_schedule(
        make_session("MUST", start=at(14)), make_session("OTHER", start=at(16))
    )

    changes = service._changes(plan, profile, schedule, {})

    cancels = [c.session_id for c in changes if c.kind is ChangeKind.CANCEL]
    assert cancels == ["OTHER"]


def _swap_and_cancel():
    from reinvent_concierge.booking.changes import PlanChange

    return [
        PlanChange(kind=ChangeKind.SWAP, session_id="NEW", blocking_session_ids=("OLD",), risk="r"),
        PlanChange(kind=ChangeKind.CANCEL, session_id="X", risk="r"),
    ]


@pytest.mark.parametrize("now_must_attend", ["OLD", "X"])
async def test_apply_rejects_when_a_released_session_became_must_attend(config, now_must_attend):
    """I2: must-attend is re-checked at execute time; the run stays PREVIEWED."""
    client = Mock()
    client.cancel_reservation = AsyncMock()
    client.reserve_sessions = AsyncMock()
    reads = [make_schedule(make_session("OLD"), make_session("X", start=at(14)))] * 4
    service, journal, run = await _real_journal_service(
        config, _swap_and_cancel(), reads, client, must_attend=(now_must_attend,)
    )

    with pytest.raises(ApprovalRejectedError, match="must-attend"):
        await service.apply(uuid4(), ApplicationMode.APPLY_APPROVED_PLAN, "tok")

    assert (await journal.get_run(run.run_id)).state == "PREVIEWED"
    client.cancel_reservation.assert_not_awaited()
    client.reserve_sessions.assert_not_awaited()


async def test_apply_rejects_when_the_profile_was_reset(config):
    """I2: a new profile_id (profile reset) invalidates the preview."""
    client = Mock()
    client.cancel_reservation = AsyncMock()
    reads = [make_schedule(make_session("OLD"))] * 4
    reset = SimpleNamespace(profile_id=uuid4(), must_attend_session_ids=())
    service, journal, run = await _real_journal_service(
        config, _swap_and_cancel(), reads, client, profile=reset
    )

    with pytest.raises(ApprovalRejectedError, match="profile"):
        await service.apply(uuid4(), ApplicationMode.APPLY_APPROVED_PLAN, "tok")

    assert (await journal.get_run(run.run_id)).state == "PREVIEWED"
    client.cancel_reservation.assert_not_awaited()


async def test_apply_settles_the_run_when_execute_raises_mid_swap(config):
    """I1 (3): the plan run ends COMPENSATION_INCOMPLETE instead of staying APPLYING."""
    import httpx

    from reinvent_concierge.booking.changes import PlanChange
    from reinvent_concierge.models import ReservationResult

    old = make_schedule(make_session("OLD"))
    down = httpx.ConnectError("down")
    client = Mock()
    client.cancel_reservation = AsyncMock(side_effect=httpx.ReadTimeout("slow"))
    client.reserve_sessions = AsyncMock(
        return_value=[ReservationResult(session_id="OLD", status="FAILED", code="alreadyScheduled")]
    )
    swap = PlanChange(
        kind=ChangeKind.SWAP, session_id="NEW", blocking_session_ids=("OLD",), risk="r"
    )
    service, journal, run = await _real_journal_service(
        config, [swap], [old, old, down, down, down], client
    )

    result = await service.apply(uuid4(), ApplicationMode.APPLY_APPROVED_PLAN, "tok")

    assert result.state == "COMPENSATION_INCOMPLETE"
    assert (await journal.get_run(run.run_id)).state == "COMPENSATION_INCOMPLETE"
    assert any(action.startswith("OLD") for action in result.manual_actions)


async def _applying_plan_run(config, changes, operations):
    """A plan run left APPLYING (e.g. the process died) with journaled operations."""
    from reinvent_concierge.planner import ApplyJournalStore, PlannerStateStore
    from reinvent_concierge.planner.application import PreviewPayload

    store = PlannerStateStore(config.planner_db_path)
    await store.initialize()
    journal = ApplyJournalStore(store)
    run = await journal.create_run("evt", uuid4(), "fp", uuid4())
    payload = PreviewPayload(
        plan_id=str(run.plan_id),
        requested_mode="APPLY_APPROVED_PLAN",
        effective_mode="APPLY_APPROVED_PLAN",
        source_fingerprint="fp",
        plan_fingerprint="plan",
        approval_digest=digest("tok"),
        expires_at=at(0),
        changes=tuple(change.model_dump(mode="json") for change in changes),
    )
    await journal.save_preview_payload(run.run_id, payload.model_dump(mode="json"))
    await journal.transition_run(run.run_id, "PREVIEWED", "APPROVED")
    await journal.transition_run(run.run_id, "APPROVED", "APPLYING")
    for order, (operation_type, session_id, state) in enumerate(operations, start=1):
        op = await journal.add_operation(
            run.run_id, order, operation_type, {"session_id": session_id}
        )
        await journal.mark_attempting(op.operation_id)
        if state != "ATTEMPTING":
            await journal.record_operation_result(op.operation_id, state)
    return journal, run


def _recovery_service(journal, schedule):
    from reinvent_concierge.booking.service import ScheduleWriteService

    reader = Mock()
    reader.read = AsyncMock(return_value=schedule)
    writer = ScheduleWriteService(journal, Mock(), reader)
    return PlanApplicationService(
        Mock(), Mock(), journal, reader, writer, Mock(), dry_run=False, catalog=_catalog()
    )


@pytest.mark.parametrize(("booked", "state"), [(("R",), "APPLIED"), ((), "PARTIAL")])
async def test_resume_recovery_settles_an_applying_plan_run(config, booked, state):
    """M3: after reconciling, the run leaves APPLYING via VERIFYING."""
    from reinvent_concierge.booking.changes import PlanChange

    changes = [PlanChange(kind=ChangeKind.RESERVE, session_id="R", risk="r")]
    journal, run = await _applying_plan_run(config, changes, [("RESERVE", "R", "ATTEMPTING")])
    service = _recovery_service(journal, make_schedule(*[make_session(s) for s in booked]))

    result = await service.resume_recovery(run.run_id, "tok")

    assert result.state == state
    assert (await journal.get_run(run.run_id)).state == state


async def test_resume_recovery_marks_a_failed_restore_compensation_incomplete(config):
    """M3: a failed RESTORE_SWAP ends the recovered run COMPENSATION_INCOMPLETE."""
    from reinvent_concierge.booking.changes import PlanChange

    swap = PlanChange(
        kind=ChangeKind.SWAP, session_id="NEW", blocking_session_ids=("OLD",), risk="r"
    )
    journal, run = await _applying_plan_run(
        config,
        [swap],
        [
            ("CANCEL_FOR_SWAP", "OLD", "CONFIRMED"),
            ("RESERVE_SWAP_TARGET", "NEW", "REFUSED"),
            ("RESTORE_SWAP", "OLD", "ATTEMPTING"),
        ],
    )
    service = _recovery_service(journal, make_schedule())

    result = await service.resume_recovery(run.run_id, "tok")

    assert result.state == "COMPENSATION_INCOMPLETE"
    assert (await journal.get_run(run.run_id)).state == "COMPENSATION_INCOMPLETE"


async def test_resume_recovery_rejects_ad_hoc_runs_without_leaking_the_payload(config):
    """M3: an ad-hoc run is refused as an approval error, not a raw validation error."""
    from reinvent_concierge.booking.approvals import AD_HOC_PROFILE_ID
    from reinvent_concierge.planner import ApplyJournalStore, PlannerStateStore

    store = PlannerStateStore(config.planner_db_path)
    await store.initialize()
    journal = ApplyJournalStore(store)
    run = await journal.create_run("evt", AD_HOC_PROFILE_ID, "fp")
    secret_digest = digest("tok")
    await journal.save_preview_payload(
        run.run_id,
        {"changes": [], "approval_digest": secret_digest, "expires_at": at(0).isoformat()},
    )
    service = _recovery_service(journal, make_schedule())

    with pytest.raises(ApprovalRejectedError) as raised:
        await service.resume_recovery(run.run_id, "tok")

    assert secret_digest not in str(raised.value)
    assert (await journal.get_run(run.run_id)).state == "PREVIEWED"
