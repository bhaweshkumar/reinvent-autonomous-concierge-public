"""Shared operation quota governor behavior."""

from datetime import datetime, timedelta, timezone

import pytest

from reinvent_concierge.planner import OperationQuota, SQLiteOperationQuotaGovernor
from reinvent_concierge.planner.quota import InMemoryOperationQuotaGovernor
from reinvent_concierge.planner.state_store import PlannerStateStore


class FakeClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 9, 30, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.current

    async def sleep(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


async def test_in_memory_governor_counts_session_units_and_releases_at_rolling_boundary():
    clock = FakeClock()
    governor = InMemoryOperationQuotaGovernor(
        "event",
        quotas={"ReserveSessions": OperationQuota(10, "session ID")},
        clock=clock,
    )

    assert await governor.acquire("ReserveSessions", units=10, timeout_seconds=0)
    assert not await governor.acquire("ReserveSessions", timeout_seconds=0)
    assert await governor.available_at("ReserveSessions") == clock.now() + timedelta(minutes=1)

    clock.current += timedelta(minutes=1)

    assert await governor.acquire("ReserveSessions", timeout_seconds=0)


async def test_background_calls_preserve_interactive_reserve_and_unknown_is_conservative():
    clock = FakeClock()
    governor = InMemoryOperationQuotaGovernor(
        "event",
        quotas={"ReserveSessions": OperationQuota(10, "session ID")},
        interactive_reserve_units=2,
        clock=clock,
    )

    assert await governor.acquire("ReserveSessions", units=8, background=True, timeout_seconds=0)
    assert not await governor.acquire("ReserveSessions", background=True, timeout_seconds=0)
    assert await governor.acquire("ReserveSessions", units=2, timeout_seconds=0)
    assert await governor.acquire("UnpublishedOperation", units=30, timeout_seconds=0)
    assert not await governor.acquire("UnpublishedOperation", timeout_seconds=0)


async def test_block_for_applies_only_to_named_operation_without_mutating_available_query():
    clock = FakeClock()
    governor = InMemoryOperationQuotaGovernor("event", clock=clock)

    await governor.block_for("GetSchedule", 12)
    before = await governor.available_at("GetSchedule")
    after = await governor.available_at("GetSchedule")

    assert before == after == clock.now() + timedelta(seconds=12)
    assert not await governor.acquire("GetSchedule", timeout_seconds=0)
    assert await governor.acquire("GetSession", timeout_seconds=0)


@pytest.mark.parametrize("units", [0, -1, True, "1"])
async def test_governor_rejects_non_positive_or_non_integer_units(units):
    governor = InMemoryOperationQuotaGovernor("event", clock=FakeClock())

    with pytest.raises(ValueError, match="positive integer"):
        await governor.acquire("GetSession", units=units, timeout_seconds=0)


async def test_sqlite_governor_coordinates_event_scoped_quota(config):
    clock = FakeClock()
    store = PlannerStateStore(config.planner_db_path)
    await store.initialize()
    quotas = {"GetSchedule": OperationQuota(1, "request")}
    first = SQLiteOperationQuotaGovernor(store, "event", quotas=quotas, clock=clock)
    second = SQLiteOperationQuotaGovernor(store, "event", quotas=quotas, clock=clock)

    assert await first.acquire("GetSchedule", timeout_seconds=0)
    assert not await second.acquire("GetSchedule", timeout_seconds=0)
