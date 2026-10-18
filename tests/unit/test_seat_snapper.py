"""SPEC-07 / SPEC-17: seat snapper worker and the shared waitlist store."""

import asyncio
import os
import signal
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest

from reinvent_concierge.booking.changes import ApplyResult, ChangeKind
from reinvent_concierge.exceptions import RateLimitError
from reinvent_concierge.planner.models import ApplyOperation
from reinvent_concierge.schedule import ConflictManager
from reinvent_concierge.snapper import SeatSnapperWorker
from reinvent_concierge.transit import TransitEngine
from tests.factories import at, make_schedule, make_session, read_audit


def applied(session_id):
    return ApplyResult(apply_run_id=uuid4(), state="APPLIED", confirmed=("RESERVE",))


def _operation(run_id, session_id, error):
    return ApplyOperation(
        apply_run_id=run_id,
        order=1,
        operation_type="RESERVE",
        request={"session_id": session_id},
        state="REFUSED",
        error=error,
    )


def deferred(session_id, reason="DEFERRED: Reservations closed"):
    run_id = uuid4()
    return ApplyResult(
        apply_run_id=run_id,
        state="PARTIAL",
        refused=("RESERVE",),
        operations=(_operation(run_id, session_id, reason),),
        deferred=(session_id,),
    )


def refused(session_id, code):
    return ApplyResult(
        apply_run_id=uuid4(),
        state="PARTIAL",
        refused=("RESERVE",),
        refusal_codes=((session_id, code),),
    )


def throttled(session_id):
    run_id = uuid4()
    return ApplyResult(
        apply_run_id=run_id,
        state="PARTIAL",
        refused=("RESERVE",),
        operations=(_operation(run_id, session_id, "THROTTLED: retry after 30s"),),
    )


def unverified(session_id):
    return ApplyResult(apply_run_id=uuid4(), state="PARTIAL")


def mark(waitlist, session_id, status):
    [entry] = [e for e in waitlist.entries() if e.session_id == session_id]
    entry.status = status
    waitlist.apply_poll_results({session_id: entry}, removed=[])


@pytest.fixture
def ingestion():
    service = Mock()

    async def sync_single(session_id):
        return make_session(session_id, status="OPEN")

    service.sync_single = AsyncMock(side_effect=sync_single)
    service.get_session_local = AsyncMock(return_value=None)
    return service


@pytest.fixture
def approvals():
    gateway = Mock()

    async def apply_immediately(event_id, changes):
        return applied(changes[0].session_id)

    gateway.apply_immediately = AsyncMock(side_effect=apply_immediately)
    return gateway


@pytest.fixture
def reader():
    schedule_reader = Mock()
    schedule_reader.read = AsyncMock(return_value=make_schedule())
    return schedule_reader


@pytest.fixture
def notifications():
    return []


def build_worker(config, ingestion, audit, waitlist, approvals, reader, notifications):
    return SeatSnapperWorker(
        config,
        ingestion,
        ConflictManager(TransitEngine()),
        audit,
        waitlist,
        approvals,
        reader,
        notifier=lambda title, message: notifications.append(message),
    )


@pytest.fixture
def worker(write_config, ingestion, audit, waitlist, approvals, reader, notifications):
    return build_worker(write_config, ingestion, audit, waitlist, approvals, reader, notifications)


def audit_rows(worker):
    return [(e["action"], e["session_id"], e["status"]) for e in read_audit(worker.audit.path)]


class TestWaitlistStore:
    def test_orders_by_priority_then_age(self, waitlist):
        waitlist.add("LOW", 90)
        waitlist.add("HIGH", 1)
        waitlist.add("MID", 10)

        assert [e.session_id for e in waitlist.entries()] == ["HIGH", "MID", "LOW"]

    def test_add_existing_updates_priority(self, waitlist):
        waitlist.add("A", 50)
        waitlist.update("A", enabled=False)

        waitlist.add("A", 5)

        [entry] = waitlist.entries()
        assert (entry.priority, entry.enabled) == (5, True)

    def test_update_validates_priority_and_remove(self, waitlist):
        waitlist.add("A")
        with pytest.raises(ValueError):
            waitlist.update("A", priority=0)

        assert waitlist.remove("A") is True
        assert waitlist.remove("A") is False

    def test_poll_results_keep_concurrent_edits(self, waitlist):
        waitlist.add("A", 50)
        [polled] = waitlist.entries()
        waitlist.update("A", priority=3)
        waitlist.add("B", 7)
        polled.status = "FULL"

        waitlist.apply_poll_results({"A": polled}, removed=[])

        entries = {e.session_id: e for e in waitlist.entries()}
        assert (entries["A"].priority, entries["A"].status) == (3, "FULL")
        assert "B" in entries

    def test_swap_proposed_status_round_trips(self, waitlist):
        waitlist.add("A")
        [polled] = waitlist.entries()
        polled.status = "SWAP_PROPOSED"

        waitlist.apply_poll_results({"A": polled}, removed=[])

        [entry] = waitlist.entries()
        assert entry.status == "SWAP_PROPOSED"


class TestPollCycle:
    async def test_processes_in_priority_order(self, worker, waitlist, ingestion):
        ingestion.sync_single.side_effect = None
        ingestion.sync_single.return_value = make_session("X", status="FULL")
        for session_id, priority in (("LOW", 90), ("HIGH", 1), ("MID", 10)):
            waitlist.add(session_id, priority)

        await worker.run_cycle()

        order = [call.args[0] for call in ingestion.sync_single.await_args_list]
        assert order == ["HIGH", "MID", "LOW"]
        assert all(e.status == "FULL" and e.last_checked for e in waitlist.entries())

    async def test_entry_added_mid_cycle_is_kept(self, worker, waitlist, ingestion):
        waitlist.add("A")

        async def add_while_polling(session_id):
            waitlist.add("LATE", 5)
            return make_session(session_id, status="FULL")

        ingestion.sync_single.side_effect = add_while_polling

        await worker.run_cycle()

        assert {e.session_id for e in waitlist.entries()} == {"A", "LATE"}

    async def test_disabled_entries_are_skipped(self, worker, waitlist, ingestion):
        waitlist.add("A")
        waitlist.update("A", enabled=False)

        await worker.run_cycle()

        ingestion.sync_single.assert_not_awaited()

    async def test_dry_run_never_reserves(
        self, config, ingestion, audit, waitlist, approvals, reader, notifications
    ):
        dry_worker = build_worker(
            config, ingestion, audit, waitlist, approvals, reader, notifications
        )
        waitlist.add("A")

        await dry_worker.run_cycle()

        approvals.apply_immediately.assert_not_awaited()
        assert [e.session_id for e in waitlist.entries()] == ["A"]

    async def test_snag_removes_entry_audits_and_notifies(self, worker, waitlist, notifications):
        waitlist.add("A", title="Zero trust")

        await worker.run_cycle()

        assert waitlist.entries() == []
        statuses = [(action, status) for action, _, status in audit_rows(worker)]
        assert ("RESERVE_SESSION", "CONFIRMED") in statuses
        assert ("WAITLIST_REMOVE", "CONFIRMED") in statuses
        assert notifications == ["Auto-reserved Zero trust"]

    async def test_canceled_session_leaves_waitlist(self, worker, waitlist, ingestion, approvals):
        ingestion.sync_single.side_effect = None
        ingestion.sync_single.return_value = None
        waitlist.add("GONE")

        await worker.run_cycle()

        assert waitlist.entries() == []
        approvals.apply_immediately.assert_not_awaited()


class TestReservation:
    async def test_reserves_through_approvals_only(self, worker, waitlist, approvals, reader):
        waitlist.add("A")

        await worker.run_cycle()

        [call] = approvals.apply_immediately.await_args_list
        event_id, [change] = call.args
        assert event_id == worker.config.aws_events_event_id
        assert (change.kind, change.session_id) == (ChangeKind.RESERVE, "A")
        assert reader.read.await_args_list[0].kwargs == {"include_favorites": False}

    async def test_overlapping_seat_is_proposed_not_swapped(
        self, worker, approvals, reader, waitlist, notifications
    ):
        reader.read = AsyncMock(return_value=make_schedule(make_session("OLD", start=at(18))))
        waitlist.add("NEW")  # the open NEW seat is at 18:00 too

        await worker.run_cycle()

        [entry] = waitlist.entries()
        assert entry.session_id == "NEW"
        assert entry.status == "SWAP_PROPOSED"
        approvals.apply_immediately.assert_not_awaited()
        message = "Seat open but overlaps OLD; review the swap in the concierge"
        [proposal] = [e for e in read_audit(worker.audit.path) if e["action"] == "SWAP_PROPOSED"]
        assert (proposal["session_id"], proposal["details"]) == ("NEW", message)
        assert notifications == [message]

    async def test_reserved_session_never_cancels_unreachable_neighbour(
        self, worker, approvals, reader, waitlist, notifications
    ):
        approvals.apply_immediately = AsyncMock(return_value=applied("NEW"))
        new = make_session("NEW", "Venetian", start=at(18))
        later = make_session("LATER", "Mandalay Bay", start=at(19, 10))
        reader.read = AsyncMock(side_effect=[make_schedule(later), make_schedule(new, later)])
        waitlist.add("NEW")

        await worker.run_cycle()

        kinds = [
            c.kind for call in approvals.apply_immediately.await_args_list for c in call.args[1]
        ]
        assert kinds == [ChangeKind.RESERVE]
        assert any(e["action"] == "TRANSIT_CONFLICT" for e in read_audit(worker.audit.path))
        assert waitlist.entries() == []
        assert (
            "Reserved NEW; it leaves no time to reach Session LATER. Review in the concierge."
            in notifications
        )
        assert "Auto-reserved NEW" in notifications

    async def test_throttled_transit_check_still_resolves_the_reserved_entry(
        self, worker, reader, waitlist
    ):
        reader.read = AsyncMock(side_effect=[make_schedule(), RateLimitError(30)])
        waitlist.add("A")

        await worker.run_cycle()

        assert waitlist.entries() == []
        assert ("RESERVE_SESSION", "A", "CONFIRMED") in audit_rows(worker)

    async def test_409_keeps_entries_and_stops_reserving_this_cycle(
        self, worker, approvals, waitlist
    ):
        approvals.apply_immediately = AsyncMock(return_value=deferred("A"))
        waitlist.add("A", 1)
        waitlist.add("B", 2)

        await worker.run_cycle()

        assert approvals.apply_immediately.await_count == 1
        assert {e.session_id for e in waitlist.entries()} == {"A", "B"}
        assert all(e.status == "WATCHING" for e in waitlist.entries())
        [reserve] = [e for e in read_audit(worker.audit.path) if e["action"] == "RESERVE_SESSION"]
        assert reserve["status"] == "DEFERRED" and "Reservations closed" in reserve["details"]
        assert all(action != "WAITLIST_REMOVE" for action, _, _ in audit_rows(worker))

    async def test_deferral_lasts_only_one_cycle(self, worker, approvals, waitlist):
        approvals.apply_immediately = AsyncMock(side_effect=[deferred("A"), applied("A")])
        waitlist.add("A")

        await worker.run_cycle()
        await worker.run_cycle()

        assert approvals.apply_immediately.await_count == 2
        assert waitlist.entries() == []

    async def test_session_full_keeps_watching(self, worker, waitlist, approvals):
        approvals.apply_immediately = AsyncMock(return_value=refused("A", "sessionFull"))
        waitlist.add("A")

        await worker.run_cycle()

        [entry] = waitlist.entries()
        assert entry.status == "FULL"

    async def test_schedule_conflict_refusal_is_swap_proposed(self, worker, waitlist, approvals):
        approvals.apply_immediately = AsyncMock(return_value=refused("A", "scheduleConflict"))
        waitlist.add("A")

        await worker.run_cycle()

        [entry] = waitlist.entries()
        assert entry.status == "SWAP_PROPOSED"

    async def test_other_refusal_fails_and_keeps_entry(self, worker, waitlist, approvals):
        approvals.apply_immediately = AsyncMock(return_value=refused("A", "notReservable"))
        waitlist.add("A")

        await worker.run_cycle()

        [entry] = waitlist.entries()
        assert entry.status == "FAILED"
        [reserve] = [e for e in read_audit(worker.audit.path) if e["action"] == "RESERVE_SESSION"]
        assert reserve["status"] == "FAILED" and "notReservable" in reserve["details"]

    @pytest.mark.parametrize("source", ["ingestion", "reader", "approvals"])
    async def test_rate_limit_keeps_entry_watching(
        self, worker, waitlist, ingestion, reader, approvals, source
    ):
        target = {
            "ingestion": ingestion.sync_single,
            "reader": reader.read,
            "approvals": approvals.apply_immediately,
        }[source]
        target.side_effect = RateLimitError(30)
        waitlist.add("A")

        await worker.run_cycle()

        [entry] = waitlist.entries()
        assert entry.status == "WATCHING"
        assert ("THROTTLED", "A") in [(a, s) for a, s, _ in audit_rows(worker)]

    async def test_throttled_reserve_keeps_entry_watching(self, worker, waitlist, approvals):
        approvals.apply_immediately = AsyncMock(return_value=throttled("A"))
        waitlist.add("A")

        await worker.run_cycle()

        [entry] = waitlist.entries()
        assert entry.status == "WATCHING"
        assert ("THROTTLED", "A") in [(a, s) for a, s, _ in audit_rows(worker)]

    async def test_unverified_outcome_is_not_reported_as_refused(self, worker, waitlist, approvals):
        approvals.apply_immediately = AsyncMock(return_value=unverified("A"))
        waitlist.add("A")

        await worker.run_cycle()

        [entry] = waitlist.entries()
        assert entry.status == "FAILED"
        [reserve] = [e for e in read_audit(worker.audit.path) if e["action"] == "RESERVE_SESSION"]
        assert reserve["details"] == "Reservation outcome unverified; run ended PARTIAL"

    async def test_failed_transit_check_still_resolves_the_reserved_entry(
        self, worker, reader, waitlist, notifications
    ):
        reader.read = AsyncMock(side_effect=[make_schedule(), httpx.ConnectError("down")])
        waitlist.add("A")

        await worker.run_cycle()

        assert waitlist.entries() == []
        rows = audit_rows(worker)
        assert ("RESERVE_SESSION", "A", "CONFIRMED") in rows
        assert ("WAITLIST_REMOVE", "A", "CONFIRMED") in rows
        assert ("TRANSIT_CONFLICT", "A", "SKIPPED") in rows
        assert notifications == ["Auto-reserved A"]

    def test_snapper_has_no_backoff_loop(self):
        assert not hasattr(SeatSnapperWorker, "_reserve_with_backoff")
        assert not hasattr(SeatSnapperWorker, "MAX_RESERVE_ATTEMPTS")


class TestShutdown:
    async def test_stop_ends_loop_after_current_cycle(self, worker):
        worker.poll_interval = 3600
        asyncio.get_running_loop().call_later(0.05, worker.stop)

        await asyncio.wait_for(worker.start(), timeout=2)

        assert worker.state == SeatSnapperWorker.STATE_IDLE

    async def test_sigterm_triggers_graceful_stop(self, worker):
        loop = asyncio.get_running_loop()
        worker.poll_interval = 3600
        worker.install_signal_handlers(loop)
        try:
            loop.call_later(0.05, os.kill, os.getpid(), signal.SIGTERM)
            await asyncio.wait_for(worker.start(), timeout=2)
        finally:
            loop.remove_signal_handler(signal.SIGTERM)
            loop.remove_signal_handler(signal.SIGINT)


class TestSwapProposalTransitions:
    """SWAP_PROPOSED is announced once, when an entry enters that state."""

    @staticmethod
    def overlap(reader):
        reader.read = AsyncMock(return_value=make_schedule(make_session("OLD", start=at(18))))

    @staticmethod
    def proposals(worker):
        return [e for e in read_audit(worker.audit.path) if e["action"] == "SWAP_PROPOSED"]

    async def test_persisting_overlap_is_announced_once(
        self, worker, reader, waitlist, notifications
    ):
        self.overlap(reader)
        waitlist.add("NEW")

        await worker.run_cycle()
        await worker.run_cycle()

        [entry] = waitlist.entries()
        assert entry.status == "SWAP_PROPOSED"
        assert len(self.proposals(worker)) == 1
        assert notifications == ["Seat open but overlaps OLD; review the swap in the concierge"]

    async def test_throttled_cycle_keeps_the_proposal_without_reannouncing(
        self, worker, reader, waitlist, notifications
    ):
        self.overlap(reader)
        waitlist.add("NEW")
        await worker.run_cycle()

        reader.read.side_effect = RateLimitError(30)
        await worker.run_cycle()
        [entry] = waitlist.entries()
        assert entry.status == "SWAP_PROPOSED"

        reader.read.side_effect = None
        await worker.run_cycle()

        assert len(self.proposals(worker)) == 1
        assert len(notifications) == 1

    async def test_schedule_conflict_refusal_is_announced_once(
        self, worker, waitlist, approvals, notifications
    ):
        approvals.apply_immediately = AsyncMock(return_value=refused("A", "scheduleConflict"))
        waitlist.add("A")

        await worker.run_cycle()
        await worker.run_cycle()

        [proposal] = self.proposals(worker)
        assert proposal["session_id"] == "A" and "scheduleConflict" in proposal["details"]
        assert notifications == [proposal["details"]]

    async def test_dry_run_keeps_a_proposal(
        self, config, ingestion, audit, waitlist, approvals, reader, notifications
    ):
        dry_worker = build_worker(
            config, ingestion, audit, waitlist, approvals, reader, notifications
        )
        waitlist.add("NEW")
        mark(waitlist, "NEW", "SWAP_PROPOSED")

        await dry_worker.run_cycle()

        [entry] = waitlist.entries()
        assert entry.status == "SWAP_PROPOSED"

    async def test_deferred_cycle_keeps_a_proposal(self, worker, waitlist, approvals):
        approvals.apply_immediately = AsyncMock(return_value=deferred("A"))
        waitlist.add("A", 1)
        waitlist.add("B", 2)
        mark(waitlist, "B", "SWAP_PROPOSED")

        await worker.run_cycle()

        statuses = {e.session_id: e.status for e in waitlist.entries()}
        assert statuses == {"A": "WATCHING", "B": "SWAP_PROPOSED"}

    async def test_throttled_reserve_keeps_a_proposal(self, worker, waitlist, approvals):
        approvals.apply_immediately = AsyncMock(return_value=throttled("A"))
        waitlist.add("A")
        mark(waitlist, "A", "SWAP_PROPOSED")

        await worker.run_cycle()

        [entry] = waitlist.entries()
        assert entry.status == "SWAP_PROPOSED"

    async def test_reserve_attempt_after_the_overlap_clears_uses_normal_status(
        self, worker, waitlist, approvals
    ):
        approvals.apply_immediately = AsyncMock(return_value=refused("NEW", "sessionFull"))
        waitlist.add("NEW")
        mark(waitlist, "NEW", "SWAP_PROPOSED")

        await worker.run_cycle()

        [entry] = waitlist.entries()
        assert entry.status == "FULL"
