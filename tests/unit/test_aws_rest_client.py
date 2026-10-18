"""SPEC-02: AWS Events REST client (paths, typing, pagination, error contract)."""

import logging
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from reinvent_concierge.api import AWSEventsRestClient
from reinvent_concierge.exceptions import (
    AWSEventsAPIError,
    AWSEventsServerError,
    RateLimitError,
    ReservationConflictError,
    WriteAccessInactiveError,
)
from reinvent_concierge.models import Session
from reinvent_concierge.planner.quota import InMemoryOperationQuotaGovernor
from tests.factories import (
    API_BASE,
    EVENT_ID,
    TOKEN,
    make_session,
    wire_bulk,
    wire_schedule,
    wire_session,
)

EVENT = f"{API_BASE}/events/{EVENT_ID}"


@pytest.fixture
async def client(config, credentials):
    async with AWSEventsRestClient(config, credentials) as rest_client:
        yield rest_client


@pytest.fixture
async def write_client(write_config, credentials):
    async with AWSEventsRestClient(write_config, credentials) as rest_client:
        yield rest_client


@respx.mock
async def test_list_sessions_consumes_every_page(client):
    route = respx.get(f"{EVENT}/sessions")
    route.side_effect = [
        httpx.Response(
            200, json={"items": [wire_session("A")], "totalCount": 2, "nextToken": "p2"}
        ),
        httpx.Response(200, json={"items": [wire_session("B")], "totalCount": 2}),
    ]

    sessions = [s async for s in client.list_sessions()]

    assert [s.session_id for s in sessions] == ["A", "B"]
    assert all(isinstance(s, Session) for s in sessions)
    assert route.calls[1].request.url.params["nextToken"] == "p2"


@respx.mock
async def test_list_sessions_skips_unschedulable_items(client):
    respx.get(f"{EVENT}/sessions").respond(
        200, json={"items": [wire_session("A", sessionTime={}), wire_session("B")], "totalCount": 2}
    )

    assert [s.session_id async for s in client.list_sessions()] == ["B"]


@respx.mock
async def test_get_session_and_list_events_are_typed(client):
    respx.get(f"{EVENT}/sessions/AI301").respond(200, json={"session": wire_session("AI301")})
    respx.get(f"{API_BASE}/events").respond(
        200,
        json={
            "items": [
                {
                    "eventId": EVENT_ID,
                    "name": "re:Invent",
                    "eventType": "x",
                    "startDate": "2026-11-30T08:00:00-08:00",
                    "endDate": "2026-12-04T17:00:00-08:00",
                    "isOnline": False,
                    "authenticationRequired": True,
                }
            ]
        },
    )

    session = await client.get_session("AI301")
    events = await client.list_events()

    assert session.session_id == "AI301"
    assert events[0].event_id == EVENT_ID


@respx.mock
async def test_every_call_goes_through_the_rate_limiter(client):
    client.rate_limiter.acquire = AsyncMock(return_value=True)
    respx.get(f"{EVENT}/sessions/A").respond(200, json={"session": wire_session("A")})
    respx.get(f"{API_BASE}/events").respond(200, json={"items": []})

    await client.get_session("A")
    await client.list_events()

    assert client.rate_limiter.acquire.await_count == 2


@respx.mock
async def test_get_schedule_resolves_ids_locally_then_via_api(client):
    respx.get(f"{EVENT}/schedule").respond(
        200,
        json=wire_schedule(
            reserved=["LOCAL", "REMOTE"],
            personal_time=[
                {
                    "personalTimeId": "pt-1",
                    "title": "Lunch",
                    "description": "food",
                    "startDateTime": "2026-12-01T20:00:00",
                    "endDateTime": "2026-12-01T21:00:00",
                }
            ],
        ),
    )
    remote = respx.get(f"{EVENT}/sessions/REMOTE").respond(
        200, json={"session": wire_session("REMOTE")}
    )

    async def resolver(session_id):
        return make_session(session_id) if session_id == "LOCAL" else None

    schedule = await client.get_schedule(session_resolver=resolver)

    assert [s.session_id for s in schedule.booked] == ["LOCAL", "REMOTE"]
    assert schedule.personal_time[0].id == "pt-1"
    assert remote.call_count == 1


@respx.mock
async def test_bearer_token_sent_and_never_logged(client, caplog):
    caplog.set_level(logging.DEBUG)
    route = respx.get(f"{EVENT}/schedule").respond(500, text="boom")

    with pytest.raises(AWSEventsServerError):
        await client.get_schedule()

    assert route.calls[0].request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in caplog.text


@respx.mock
async def test_409_maps_to_write_access_inactive_before_oct_8(client):
    respx.post(f"{EVENT}/favorites").respond(409, json={"message": "closed"})

    with pytest.raises(WriteAccessInactiveError):
        await client.associate_favorites(["A"])


@respx.mock
async def test_409_maps_to_reservation_conflict_when_write_access_active(write_client):
    respx.post(f"{EVENT}/reservations").respond(409, json={"message": "closed"})

    with pytest.raises(ReservationConflictError):
        await write_client.reserve_sessions(["A"])


async def test_writes_are_gated_in_dry_run_mode_without_http(client):
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(f"{EVENT}/reservations").respond(200, json=wire_bulk(["A"]))
        with pytest.raises(WriteAccessInactiveError):
            await client.reserve_sessions(["A"])
        with pytest.raises(WriteAccessInactiveError):
            await client.cancel_reservation("A")
        assert route.call_count == 0


@respx.mock
async def test_429_carries_retry_after(client):
    respx.get(f"{EVENT}/sessions/A").respond(429, headers={"Retry-After": "12.5"})

    with pytest.raises(RateLimitError) as error:
        await client.get_session("A")

    assert error.value.retry_after == 12.5


@respx.mock
async def test_operation_quota_uses_documented_metadata_and_blocks_429(config, credentials):
    config.write_access_active = True
    quota = InMemoryOperationQuotaGovernor(config.aws_events_event_id)
    quota.acquire = AsyncMock(return_value=True)
    quota.block_for = AsyncMock()
    async with AWSEventsRestClient(
        config, credentials, operation_quota_governor=quota
    ) as quota_client:
        quota_client.rate_limiter.acquire = AsyncMock(return_value=True)
        respx.post(f"{EVENT}/reservations").respond(429, headers={"Retry-After": "12.5"})

        with pytest.raises(RateLimitError):
            await quota_client.reserve_sessions(["A", "B", "A"])

    quota.acquire.assert_awaited_once_with("ReserveSessions", 2, timeout_seconds=60.0)
    quota.block_for.assert_awaited_once_with("ReserveSessions", 12.5)
    assert quota_client.rate_limiter.acquire.await_count == 1


@respx.mock
async def test_list_sessions_charges_quota_once_per_page(config, credentials):
    quota = InMemoryOperationQuotaGovernor(config.aws_events_event_id)
    quota.acquire = AsyncMock(return_value=True)
    async with AWSEventsRestClient(
        config, credentials, operation_quota_governor=quota
    ) as quota_client:
        respx.get(f"{EVENT}/sessions").side_effect = [
            httpx.Response(200, json={"items": [], "nextToken": "next"}),
            httpx.Response(200, json={"items": []}),
        ]

        assert [session async for session in quota_client.list_sessions()] == []

    assert quota.acquire.await_args_list == [
        (("ListSessions", 1), {"timeout_seconds": 60.0}),
        (("ListSessions", 1), {"timeout_seconds": 60.0}),
    ]


@respx.mock
async def test_refresh_retry_does_not_charge_quota_twice(config, credentials):
    credentials.store_token(TOKEN, refresh_token="refresh-token")
    quota = InMemoryOperationQuotaGovernor(config.aws_events_event_id)
    quota.acquire = AsyncMock(return_value=True)
    async with AWSEventsRestClient(
        config, credentials, operation_quota_governor=quota
    ) as quota_client:
        quota_client.rate_limiter.acquire = AsyncMock(return_value=True)
        respx.get(f"{EVENT}/sessions/A").side_effect = [
            httpx.Response(401),
            httpx.Response(200, json={"session": wire_session("A")}),
        ]
        respx.post(config.aws_events_oauth_token_url).respond(
            200, json={"access_token": "fresh-token"}
        )

        await quota_client.get_session("A")

    quota.acquire.assert_awaited_once_with("GetSession", 1, timeout_seconds=60.0)
    assert quota_client.rate_limiter.acquire.await_count == 2


@respx.mock
async def test_other_4xx_raise_api_error_with_status(client):
    respx.get(f"{EVENT}/sessions/A").respond(403, text="<html>edge</html>")

    with pytest.raises(AWSEventsAPIError) as error:
        await client.get_session("A")

    assert error.value.status_code == 403


@respx.mock
async def test_reserve_reports_each_session(write_client):
    respx.post(f"{EVENT}/reservations").respond(
        200,
        json=wire_bulk(
            successful=["A"],
            failed=[{"sessionId": "B", "code": "scheduleConflict", "conflictsWith": ["C"]}],
        ),
    )

    results = await write_client.reserve_sessions(["A", "B"])

    assert [(r.session_id, r.status) for r in results] == [("A", "CONFIRMED"), ("B", "FAILED")]
    assert results[1].code == "scheduleConflict"
    assert results[1].conflicts_with == ["C"]


async def test_batches_are_limited_to_ten_distinct_ids(write_client):
    with pytest.raises(ValueError):
        await write_client.reserve_sessions([str(i) for i in range(11)])


@respx.mock
async def test_removal_404_is_treated_as_done(write_client):
    respx.delete(f"{EVENT}/reservations/A").respond(404, json={"message": "gone"})
    respx.delete(f"{EVENT}/favorites/A").respond(404, json={"message": "gone"})

    await write_client.cancel_reservation("A")
    await write_client.disassociate_favorite("A")


@respx.mock
async def test_create_personal_time_reads_id_back_from_schedule(write_client):
    create = respx.post(f"{EVENT}/personal-time").respond(204)
    respx.get(f"{EVENT}/schedule").respond(
        200,
        json=wire_schedule(
            personal_time=[
                {
                    "personalTimeId": "pt-9",
                    "title": "Transit: Mandalay Bay → Venetian",
                    "description": "walk",
                    "startDateTime": "2026-12-01T19:00:00",
                    "endDateTime": "2026-12-01T19:35:00",
                }
            ]
        ),
    )
    session = make_session("A", minutes=60)

    personal_time_id = await write_client.create_personal_time(
        "Transit: Mandalay Bay → Venetian",
        session.end_time.replace(minute=0),
        session.end_time.replace(minute=35),
        "walk",
    )

    assert personal_time_id == "pt-9"
    body = create.calls[0].request.read()
    assert b'"startDateTime":"2026-12-01T19:00:00"' in body
    assert b'"description":"walk"' in body
