"""Tier 2 - schedule writes (LIVE_ALLOW_WRITES=1): favorites, personal time and transit
buffers, via our REST client, our agent and the hosted MCP server. Every test restores the
attendee's schedule in ``finally``; leftover personal time is swept at the end of the run.
"""

from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from reinvent_concierge.agent import MCPAgent
from reinvent_concierge.api.hosted_mcp import HostedMCPClient
from reinvent_concierge.api.mapping import format_personal_time, personal_time_from_api
from reinvent_concierge.audit import AuditLogger
from reinvent_concierge.booking import TRANSIT_MARKER, transit_title
from reinvent_concierge.exceptions import ReservationConflictError, UnknownVenueError
from reinvent_concierge.ingestion import IngestionService
from reinvent_concierge.models import AttendeeSchedule
from reinvent_concierge.schedule import ConflictManager
from reinvent_concierge.transit import TransitEngine
from reinvent_concierge.waitlist import WaitlistStore
from tests.factories import build_approvals, read_audit
from tests.live.conftest import LIVE, TEST_TITLE_PREFIX, pick_untouched

pytestmark = [pytest.mark.live, pytest.mark.usefixtures("require_writes")]


def block_by_id(raw_schedule, personal_time_id):
    return next(
        (b for b in raw_schedule["personalTime"] if b["personalTimeId"] == personal_time_id), None
    )


async def test_favorite_round_trip(client, catalog):
    session = pick_untouched(catalog, await client.get_raw_schedule())
    session_id = session.session_id
    try:
        added = await client.associate_favorites([session_id])
        assert added.successful == [session_id] and not added.failed
        assert session_id in (await client.get_raw_schedule())["favorites"]

        repeated = await client.associate_favorites([session_id])
        assert [f.code for f in repeated.failed] == ["alreadyFavorited"]

        await client.disassociate_favorite(session_id)
        assert session_id not in (await client.get_raw_schedule())["favorites"]
        await client.disassociate_favorite(session_id)  # 404 on a repeat is treated as done
    finally:
        await client.disassociate_favorite(session_id)


async def test_personal_time_round_trip(write_client):
    event = await write_client.get_event()
    zone = ZoneInfo(event.timezone or "UTC")
    first_day = datetime.fromisoformat(event.start_date).astimezone(zone)
    start = first_day.replace(hour=5, minute=0, second=0, microsecond=0)
    end = start + timedelta(minutes=15)
    title = f"{TEST_TITLE_PREFIX} {uuid4().hex[:8]}"
    block_id = None
    try:
        try:
            block_id = await write_client.create_personal_time(
                title, start, end, "Created by the concierge live test suite"
            )
        except ReservationConflictError:
            pytest.skip("CreatePersonalTime is closed right now (HTTP 409)")

        created = block_by_id(await write_client.get_raw_schedule(), block_id)
        assert created["title"] == title
        assert created["startDateTime"] == format_personal_time(start)
        assert created["endDateTime"] == format_personal_time(end)

        later = end + timedelta(minutes=5)
        await write_client.update_personal_time(block_id, title, start, later, "Updated by tests")
        updated = block_by_id(await write_client.get_raw_schedule(), block_id)
        assert updated["endDateTime"] == format_personal_time(later)
        assert updated["description"] == "Updated by tests"

        await write_client.delete_personal_time(block_id)
        assert block_by_id(await write_client.get_raw_schedule(), block_id) is None
        await write_client.delete_personal_time(block_id)  # deleting twice is safe
        block_id = None
    finally:
        if block_id:
            await write_client.delete_personal_time(block_id)


def venue_change_pair(catalog, transit):
    """Two same-day sessions at different known venues with enough time to walk."""
    placed = []
    for session in sorted(catalog, key=lambda s: s.start_time):
        try:
            placed.append((session, transit.normalize_venue(session.venue_id)))
        except UnknownVenueError:
            continue
    for index, (a, venue_a) in enumerate(placed):
        for b, venue_b in placed[index + 1 : index + 40]:
            if venue_a == venue_b or a.start_time.date() != b.start_time.date():
                continue
            gap = (b.start_time - a.end_time).total_seconds() / 60
            if gap >= transit.get_transit_minutes(venue_a, venue_b):
                return a, b
    return None


async def test_transit_buffer_is_created_read_back_and_idempotent(
    live_write_config, write_client, catalog
):
    transit = TransitEngine()
    pair = venue_change_pair(catalog, transit)
    if pair is None:
        pytest.skip("No same-day venue-change session pair in the catalog")
    a, b = pair
    manager = ConflictManager(transit)
    approvals, _reader = await build_approvals(live_write_config, write_client)
    minutes = transit.get_transit_minutes(a.venue_id, b.venue_id)
    venues = transit.normalize_venue(a.venue_id), transit.normalize_venue(b.venue_id)
    buffer_id = None
    try:
        [change], _ = manager.propose_transit_buffers(AttendeeSchedule(booked=[a, b]))
        result = await approvals.apply_immediately(LIVE.event_id, [change])
        if any((op.error or "").startswith("DEFERRED:") for op in result.operations):
            pytest.skip("CreatePersonalTime is closed right now (HTTP 409)")
        assert result.state == "APPLIED"

        raw = await write_client.get_raw_schedule()
        start = format_personal_time(a.end_time)
        block = next(
            p
            for p in raw["personalTime"]
            if p["title"] == transit_title(*venues) and p["startDateTime"] == start
        )
        buffer_id = block["personalTimeId"]
        assert block["description"].startswith(TRANSIT_MARKER)
        assert block["endDateTime"] == format_personal_time(a.end_time + timedelta(minutes=minutes))

        schedule = AttendeeSchedule(
            booked=[a, b], personal_time=[personal_time_from_api(p) for p in raw["personalTime"]]
        )
        again, _ = manager.propose_transit_buffers(schedule)
        assert again == [], "an existing buffer must not be duplicated"
    finally:
        if buffer_id:
            await write_client.delete_personal_time(buffer_id)


async def test_agent_books_as_favorite_in_dry_run(
    live_config, live_credentials, client, catalog, embedder
):
    session = pick_untouched(catalog, await client.get_raw_schedule())
    ingestion = IngestionService(live_config, client, embedder=embedder)
    await ingestion.initialize()
    transit = TransitEngine()
    approvals, reader = await build_approvals(live_config, client, ingestion.get_session_local)
    agent = MCPAgent(
        live_config,
        client,
        ingestion,
        transit,
        ConflictManager(transit),
        AuditLogger(live_config.planner_db_path, live_credentials.hash_token),
        WaitlistStore(live_config.waitlist_path),
        approvals=approvals,
        reader=reader,
    )
    try:
        result = await agent.optimize_and_book_session(session.session_id)

        assert result.status == "FAVORITED"
        assert session.session_id in (await client.get_raw_schedule())["favorites"]
        [entry] = read_audit(live_config.audit_log_path)
        assert entry["action"] == "[DRY_RUN] optimize_and_book_session"
        assert entry["attendee_token_hash"] and live_credentials.get_token() not in str(entry)
    finally:
        await client.disassociate_favorite(session.session_id)


async def test_hosted_mcp_writes_are_visible_through_rest(
    live_credentials, live_config, client, catalog
):
    session = pick_untouched(catalog, await client.get_raw_schedule())
    arguments = {"eventId": LIVE.event_id}
    try:
        async with HostedMCPClient(
            live_credentials.get_token(), live_config.aws_events_hosted_mcp_url
        ) as hosted:
            await hosted.call(
                "AssociateFavorites", {**arguments, "sessionIds": [session.session_id]}
            )
            assert session.session_id in (await client.get_raw_schedule())["favorites"]
            await hosted.call(
                "DisassociateFavorite", {**arguments, "sessionId": session.session_id}
            )
        assert session.session_id not in (await client.get_raw_schedule())["favorites"]
    finally:
        await client.disassociate_favorite(session.session_id)
