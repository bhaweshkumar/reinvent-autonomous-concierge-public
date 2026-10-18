"""SPEC-21: SimulatedScheduleClient behaves like ScheduleSource + ScheduleWriter, in memory."""

from datetime import datetime, timezone

import pytest

from reinvent_concierge.demo import SimulatedScheduleClient
from reinvent_concierge.exceptions import AWSEventsAPIError
from tests.factories import make_session


@pytest.fixture
def catalog():
    sessions = {
        "OPEN1": make_session("OPEN1", title="Open session"),
        "OPEN2": make_session("OPEN2", title="Another open session"),
    }

    async def lookup(session_id):
        return sessions.get(session_id)

    return lookup


@pytest.fixture
def client(catalog):
    return SimulatedScheduleClient(catalog)


async def test_get_session_falls_back_to_the_catalog_lookup(client):
    session = await client.get_session("OPEN1")
    assert session.session_id == "OPEN1"


async def test_get_session_raises_for_an_unknown_session(client):
    with pytest.raises(AWSEventsAPIError) as exc_info:
        await client.get_session("GHOST")
    assert exc_info.value.status_code == 404


async def test_empty_schedule_by_default(client):
    schedule = await client.get_schedule()

    assert schedule.booked == [] and schedule.favorites == [] and schedule.personal_time == []


async def test_reserve_then_cancel_round_trips(client):
    [result] = await client.reserve_sessions(["OPEN1"])
    assert result.status == "CONFIRMED"

    schedule = await client.get_schedule()
    assert [s.session_id for s in schedule.booked] == ["OPEN1"]

    await client.cancel_reservation("OPEN1")
    schedule = await client.get_schedule()
    assert schedule.booked == []


async def test_cancel_of_an_absent_reservation_is_idempotent(client):
    await client.cancel_reservation("never-booked")  # must not raise


async def test_reserve_unknown_session_fails_without_booking_it(client):
    [result] = await client.reserve_sessions(["GHOST"])

    assert result.status == "FAILED"
    assert result.code == "notFound"
    assert (await client.get_schedule()).booked == []


async def test_reserve_outcome_is_deterministic_across_fresh_instances():
    async def lookup(session_id):
        return make_session(session_id)

    first = SimulatedScheduleClient(lookup)
    second = SimulatedScheduleClient(lookup)

    [r1] = await first.reserve_sessions(["A100"])
    [r2] = await second.reserve_sessions(["A100"])

    assert r1.status == r2.status
    assert r1.code == r2.code


async def test_reserve_outcome_varies_by_session_id():
    async def lookup(session_id):
        return make_session(session_id)

    client = SimulatedScheduleClient(lookup)
    outcomes = {(await client.reserve_sessions([f"SESSION-{i}"]))[0].status for i in range(50)}

    # Not every session should be full, and not every session should be open.
    assert outcomes == {"CONFIRMED", "FAILED"}


async def test_favorite_then_remove_round_trips(client):
    result = await client.associate_favorites(["OPEN1", "OPEN2"])
    assert set(result.successful) == {"OPEN1", "OPEN2"}
    assert result.failed == []

    schedule = await client.get_schedule()
    assert {s.session_id for s in schedule.favorites} == {"OPEN1", "OPEN2"}

    await client.disassociate_favorite("OPEN1")
    schedule = await client.get_schedule()
    assert [s.session_id for s in schedule.favorites] == ["OPEN2"]


async def test_disassociate_absent_favorite_is_idempotent(client):
    await client.disassociate_favorite("never-favorited")  # must not raise


async def test_favoriting_an_unknown_session_is_silently_skipped(client):
    result = await client.associate_favorites(["GHOST"])

    assert result.successful == [] and result.failed == []


async def test_personal_time_create_update_delete_round_trips(client):
    start = datetime(2026, 12, 1, 9, 0, tzinfo=timezone.utc)
    end = datetime(2026, 12, 1, 9, 30, tzinfo=timezone.utc)

    block_id = await client.create_personal_time("Transit: A to B", start, end, "Travel buffer")
    schedule = await client.get_schedule()
    assert [b.id for b in schedule.personal_time] == [block_id]
    assert schedule.personal_time[0].title == "Transit: A to B"

    await client.update_personal_time(block_id, "Renamed", start, end, "Updated")
    schedule = await client.get_schedule()
    assert schedule.personal_time[0].title == "Renamed"

    await client.delete_personal_time(block_id)
    schedule = await client.get_schedule()
    assert schedule.personal_time == []


async def test_delete_absent_personal_time_is_idempotent(client):
    await client.delete_personal_time("never-created")  # must not raise


async def test_get_schedule_can_exclude_favorites_and_personal_time(client):
    start = datetime(2026, 12, 1, tzinfo=timezone.utc)
    end = datetime(2026, 12, 1, 1, tzinfo=timezone.utc)
    await client.associate_favorites(["OPEN1"])
    await client.create_personal_time("Block", start, end, "desc")

    schedule = await client.get_schedule(include_favorites=False, include_personal_time=False)

    assert schedule.favorites == [] and schedule.personal_time == []
