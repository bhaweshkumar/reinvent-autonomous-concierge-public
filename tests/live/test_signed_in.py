"""Tier 1 - signed in, read-only: schedule, registration catalog, sync/search, local MCP
server, transit, and a dry-run snapper cycle. Nothing is written to the attendee's schedule.
"""

import time
import warnings
from collections import Counter
from itertools import combinations

import pytest
from fastmcp import Client

from reinvent_concierge.agent import MCPAgent
from reinvent_concierge.agent.server import build_server
from reinvent_concierge.api import AWSEventsRestClient
from reinvent_concierge.audit import AuditLogger
from reinvent_concierge.credentials import CredentialManager
from reinvent_concierge.exceptions import UnknownVenueError
from reinvent_concierge.ingestion import IngestionService
from reinvent_concierge.schedule import ConflictManager
from reinvent_concierge.snapper import SeatSnapperWorker
from reinvent_concierge.transit import TransitEngine
from reinvent_concierge.waitlist import WaitlistStore
from tests.factories import build_approvals, read_audit
from tests.live.conftest import LIVE, CachedCatalog, build_config, raw_walk

pytestmark = pytest.mark.live

SEAT_BANDS = {None, "available", "limited", "veryLimited", "unavailable", "walkUp"}
MIN_VENUE_RECOGNITION = 0.9


def recognized(session, transit):
    try:
        return transit.normalize_venue(session.venue_id)
    except UnknownVenueError:
        return None


async def synced_ingestion(config, client, catalog, embedder):
    event = await client.get_event()
    ingestion = IngestionService(config, CachedCatalog(event, catalog, client), embedder=embedder)
    await ingestion.initial_sync()
    return ingestion


async def test_schedule_ids_resolve_to_sessions(client):
    raw = await client.get_raw_schedule()
    schedule = await client.get_schedule()

    assert [s.session_id for s in schedule.booked] == raw["reserved"]
    assert [s.session_id for s in schedule.favorites] == raw["favorites"]
    assert len(schedule.personal_time) == len(raw["personalTime"])


async def test_registration_catalog_is_complete(live_config, live_credentials, catalog):
    raw, total = await raw_walk(live_config, LIVE.event_id, live_credentials.get_auth_header())

    assert len(raw) == total, "raw pagination lost sessions"
    schedulable = [i for i in raw if (i.get("sessionTime") or {}).get("date")]
    assert len(catalog) == len(schedulable), "client skipped or duplicated sessions"
    assert len({s.session_id for s in catalog}) == len(catalog)


def test_seat_availability_uses_known_bands(catalog):
    unknown = Counter(s.seat_availability for s in catalog if s.seat_availability not in SEAT_BANDS)

    assert not unknown, f"new seatAvailability values: {dict(unknown)}"


def test_venues_are_recognized_by_the_transit_engine(catalog):
    transit = TransitEngine()
    with_venue = [s for s in catalog if s.venue_id != "Unknown"]
    if not with_venue:
        pytest.skip("catalog publishes no venues yet")

    unrecognized = Counter(s.venue_id for s in with_venue if recognized(s, transit) is None)
    ratio = 1 - sum(unrecognized.values()) / len(with_venue)

    assert ratio >= MIN_VENUE_RECOGNITION, (
        f"only {ratio:.0%} of venues map to the transit graph; "
        f"add aliases for: {unrecognized.most_common(10)}"
    )


def test_hero_sessions_are_detectable(catalog):
    heroes = [s for s in catalog if s.is_hero_speaker]
    if not heroes:
        warnings.warn(
            "No session is tagged as an AWS Hero session; `hero_only` search returns nothing. "
            "Check which taxonomy field the event uses for heroes.",
            stacklevel=1,
        )


async def test_sync_and_hybrid_search_on_live_catalog(live_config, client, catalog, embedder):
    ingestion = await synced_ingestion(live_config, client, catalog, embedder)
    level = next((s.level for s in catalog if s.level == 300), None) or catalog[0].level

    await ingestion.search("warm up")
    started = time.perf_counter()
    results = await ingestion.search("Amazon Bedrock generative AI agents", level=level, limit=10)
    elapsed_ms = (time.perf_counter() - started) * 1000

    open_sessions = [s for s in catalog if s.status == "OPEN" and s.level == level]
    if not open_sessions:
        pytest.skip(f"no OPEN level-{level} sessions")
    assert results, "hybrid search returned nothing for a populated catalog"
    assert all(r.level == level and r.status == "OPEN" for r in results)
    if LIVE.real_embeddings:
        assert elapsed_ms < 150, f"search took {elapsed_ms:.0f} ms"


async def test_venue_filter_on_live_catalog(live_config, client, catalog, embedder):
    transit = TransitEngine()
    venues = Counter(recognized(s, transit) for s in catalog if s.status == "OPEN")
    venues.pop(None, None)
    if not venues:
        pytest.skip("no recognized venues in the catalog")
    venue = venues.most_common(1)[0][0]
    ingestion = await synced_ingestion(live_config, client, catalog, embedder)

    results = await ingestion.search("session", venue=venue, limit=20)

    assert results
    assert all(transit.normalize_venue(r.venue_id) == venue for r in results)


def test_transit_checks_on_real_session_pairs(catalog):
    transit = TransitEngine()
    placed = [(s, recognized(s, transit)) for s in catalog]
    placed = [(s, v) for s, v in placed if v][:200]
    pairs = [
        (a, b)
        for (a, va), (b, vb) in combinations(placed, 2)
        if va != vb and a.end_time <= b.start_time and a.start_time.date() == b.start_time.date()
    ][:20]
    if not pairs:
        pytest.skip("no same-day venue-change pairs")

    for a, b in pairs:
        result = transit.check_feasibility(a.end_time, b.start_time, a.venue_id, b.venue_id)
        assert result.transit_needed == transit.get_transit_minutes(a.venue_id, b.venue_id) > 0
        assert result.is_feasible == (result.available_gap >= result.transit_needed)


async def test_local_mcp_server_read_tools_end_to_end(
    live_config, live_credentials, client, catalog, embedder
):
    ingestion = await synced_ingestion(live_config, client, catalog, embedder)
    transit = TransitEngine()
    agent = MCPAgent(
        live_config,
        client,
        ingestion,
        transit,
        ConflictManager(transit),
        AuditLogger(live_config.planner_db_path, live_credentials.hash_token),
        WaitlistStore(live_config.waitlist_path),
    )
    first, second = sorted(catalog, key=lambda s: s.start_time)[:2]

    async with Client(build_server(agent)) as mcp:
        tools = {tool.name for tool in await mcp.list_tools()}
        found = await mcp.call_tool("search_catalog", {"query": first.title, "limit": 5})
        schedule = await mcp.call_tool("get_my_schedule", {})
        transit_result = await mcp.call_tool(
            "check_transit",
            {"from_session_id": first.session_id, "to_session_id": second.session_id},
        )
        queued = await mcp.call_tool(
            "add_to_waitlist", {"session_id": first.session_id, "priority": 7}
        )

    assert len(tools) == 7
    assert found.structured_content["total_found"] >= 0
    assert "booked" in schedule.structured_content
    assert "is_feasible" in transit_result.structured_content
    assert queued.structured_content["priority"] == 7
    audit = read_audit(live_config.audit_log_path)
    assert len(audit) == 4 and all(e["action"].startswith("[DRY_RUN]") for e in audit)
    assert live_credentials.get_token() not in live_config.audit_log_path.read_text()


async def test_snapper_dry_run_cycle_polls_real_sessions(
    live_config, live_credentials, client, catalog, embedder
):
    ingestion = await synced_ingestion(live_config, client, catalog, embedder)
    waitlist = WaitlistStore(live_config.waitlist_path)
    targets = catalog[:2]
    for index, session in enumerate(targets, start=1):
        waitlist.add(session.session_id, priority=index)
    approvals, reader = await build_approvals(live_config, client, ingestion.get_session_local)
    worker = SeatSnapperWorker(
        live_config,
        ingestion,
        ConflictManager(TransitEngine()),
        AuditLogger(live_config.planner_db_path, live_credentials.hash_token),
        waitlist,
        approvals,
        reader,
        notifier=lambda *_: None,
    )

    await worker.run_cycle()

    entries = {e.session_id: e for e in waitlist.entries()}
    assert set(entries) == {s.session_id for s in targets}, "dry-run must never snag"
    assert all(e.last_checked and e.status in {"WATCHING", "FULL"} for e in entries.values())


async def test_expired_access_token_is_refreshed(live_root, credentials_path, registration):
    if not LIVE.test_refresh:
        pytest.skip("Set LIVE_TEST_REFRESH=1 (rotates the stored refresh token)")
    credentials = CredentialManager(credentials_path)
    refresh_token = credentials.get_refresh_token()
    if not refresh_token:
        pytest.skip("No refresh token stored: sign in with `concierge login`")
    credentials.store_token("expired-or-invalid-token", refresh_token)
    config = build_config(live_root / "refresh", LIVE.event_id, credentials_path)

    async with AWSEventsRestClient(config, credentials) as client:
        schedule = await client.get_raw_schedule()

    assert schedule["reserved"] == registration["reserved"]
    assert credentials.get_token() != "expired-or-invalid-token"
