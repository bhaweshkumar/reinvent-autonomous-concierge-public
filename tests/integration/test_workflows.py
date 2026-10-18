"""End-to-end workflows over mocked HTTP (respx), per the spec's integration test table."""

import json
import sqlite3
import time
from typing import Any, Dict, List
from uuid import uuid4

import httpx
import pytest
import respx

from reinvent_concierge.agent import MCPAgent
from reinvent_concierge.api import AWSEventsRestClient
from reinvent_concierge.credentials import CredentialManager
from reinvent_concierge.ingestion import IngestionService
from reinvent_concierge.models import AttendeeSchedule
from reinvent_concierge.planner import (
    ApplyJournalStore,
    OperationQuota,
    PlannerStateStore,
    ScheduleSnapshotService,
    SQLiteOperationQuotaGovernor,
)
from reinvent_concierge.runtime import RuntimeCoordinator, RuntimeLeaseError
from reinvent_concierge.schedule import ConflictManager
from reinvent_concierge.snapper import SeatSnapperWorker
from reinvent_concierge.transit import TransitEngine
from tests.factories import (
    API_BASE,
    EVENT_ID,
    REFRESH_TOKEN,
    TOKEN,
    TOKEN_URL,
    FakeEmbedder,
    audit_dump_text,
    build_approvals,
    make_session,
    read_audit,
    wire_bulk,
    wire_schedule,
    wire_session,
)

EVENT = f"{API_BASE}/events/{EVENT_ID}"
EVENT_PAYLOAD = {
    "event": {
        "eventId": EVENT_ID,
        "name": "AWS re:Invent 2026",
        "eventType": "re:Invent",
        "startDate": "2026-11-30T08:00:00-08:00",
        "endDate": "2026-12-04T17:00:00-08:00",
        "isOnline": False,
        "authenticationRequired": True,
    }
}


class Stack:
    """Real services wired together, as `create_app_context` does, with a fake embedder."""

    def __init__(self, config, credentials, audit, waitlist):
        self.config = config
        self.audit = audit
        self.waitlist = waitlist
        self.client = AWSEventsRestClient(config, credentials)
        self.ingestion = IngestionService(config, self.client, embedder=FakeEmbedder())
        self.transit = TransitEngine()
        self.conflicts = ConflictManager(self.transit)
        self.notifications: List[str] = []

    async def initialize(self) -> None:
        await self.ingestion.initialize()
        self.approvals, self.reader = await build_approvals(
            self.config, self.client, self.ingestion.get_session_local
        )
        self.snapper = SeatSnapperWorker(
            self.config,
            self.ingestion,
            self.conflicts,
            self.audit,
            self.waitlist,
            self.approvals,
            self.reader,
            notifier=lambda _title, message: self.notifications.append(message),
        )
        self.agent = MCPAgent(
            self.config,
            self.client,
            self.ingestion,
            self.transit,
            self.conflicts,
            self.audit,
            self.waitlist,
            approvals=self.approvals,
            reader=self.reader,
        )


class FakeAttendeeApi:
    """respx handlers over one attendee schedule, so writes show up in later GetSchedule reads."""

    def __init__(self, reserved=(), favorites=()):
        self.reserved: List[str] = list(reserved)
        self.favorites: List[str] = list(favorites)
        self.personal_time: List[Dict[str, Any]] = []

    def mount(self, mock) -> None:
        mock.get(f"{EVENT}/schedule").mock(side_effect=self.schedule)
        mock.post(f"{EVENT}/favorites").mock(side_effect=self.favorite)
        mock.post(f"{EVENT}/reservations").mock(side_effect=self.reserve)
        mock.delete(url__regex=rf"{EVENT}/reservations/.+").mock(side_effect=self.cancel)
        mock.post(f"{EVENT}/personal-time").mock(side_effect=self.create_personal_time)

    def schedule(self, request):
        return httpx.Response(
            200, json=wire_schedule(self.reserved, self.favorites, self.personal_time)
        )

    def favorite(self, request):
        ids = json.loads(request.content)["sessionIds"]
        self.favorites.extend(i for i in ids if i not in self.favorites)
        return httpx.Response(200, json=wire_bulk(ids))

    def reserve(self, request):
        ids = json.loads(request.content)["sessionIds"]
        self.reserved.extend(i for i in ids if i not in self.reserved)
        return httpx.Response(200, json=wire_bulk(ids))

    def cancel(self, request):
        session_id = request.url.path.rsplit("/", 1)[-1]
        self.reserved = [i for i in self.reserved if i != session_id]
        return httpx.Response(204)

    def create_personal_time(self, request):
        payload = json.loads(request.content)
        self.personal_time.append(
            {"personalTimeId": f"pt-{len(self.personal_time) + 1}", **payload}
        )
        return httpx.Response(204)


@pytest.fixture
async def stack(config, credentials, audit, waitlist):
    built = Stack(config, credentials, audit, waitlist)
    await built.initialize()
    yield built
    await built.client.aclose()


@pytest.fixture
async def write_stack(write_config, credentials, audit, waitlist):
    built = Stack(write_config, credentials, audit, waitlist)
    await built.initialize()
    yield built
    await built.client.aclose()


def synthetic_catalog(count=50):
    levels = ("100 - Foundational", "200 - Intermediate", "300 - Advanced", "400 - Expert")
    topics = ("generative AI with Bedrock", "security", "serverless", "databases", "networking")
    return [
        wire_session(
            f"S{i:03d}",
            title=f"{topics[i % 5].title()} deep dive {i}",
            abstract=f"All about {topics[i % 5]}",
            level=levels[i % 4],
            time=f"{8 + i % 9:02d}:00",
        )
        for i in range(count)
    ]


@respx.mock
async def test_two_page_pagination_is_transparent(stack):
    respx.get(f"{EVENT}/sessions").side_effect = [
        httpx.Response(
            200, json={"items": [wire_session("A")], "totalCount": 2, "nextToken": "page-2"}
        ),
        httpx.Response(200, json={"items": [wire_session("B")], "totalCount": 2}),
    ]

    sessions = [s async for s in stack.client.list_sessions()]

    assert [s.session_id for s in sessions] == ["A", "B"]


@respx.mock
async def test_full_ingest_then_search(stack):
    catalog = synthetic_catalog()
    respx.get(EVENT).respond(200, json=EVENT_PAYLOAD)
    respx.get(f"{EVENT}/sessions").side_effect = [
        httpx.Response(200, json={"items": catalog[:25], "totalCount": 50, "nextToken": "p2"}),
        httpx.Response(200, json={"items": catalog[25:], "totalCount": 50}),
    ]

    assert await stack.ingestion.initial_sync() == 50
    started = time.perf_counter()
    results = await stack.ingestion.search("generative AI", level=300, limit=10)
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert results and all(r.level == 300 for r in results)
    assert "Generative Ai" in results[0].title
    assert elapsed_ms < 150


async def test_expired_token_is_refreshed_once_and_retried(stack, config):
    CredentialManager(config.aws_events_credentials_path).store_token(TOKEN, REFRESH_TOKEN)
    with respx.mock:
        schedule = respx.get(f"{EVENT}/schedule")
        schedule.side_effect = [httpx.Response(401), httpx.Response(200, json=wire_schedule())]
        token = respx.post(TOKEN_URL).respond(
            200, json={"access_token": "fresh-token", "expires_in": 3600}
        )

        await stack.client.get_schedule()

    assert token.call_count == 1
    assert schedule.calls[1].request.headers["Authorization"] == "Bearer fresh-token"
    stored = CredentialManager(config.aws_events_credentials_path)
    assert stored.get_token() == "fresh-token"
    assert stored.get_refresh_token() == REFRESH_TOKEN


async def test_reservation_409_falls_back_to_favorites(write_stack):
    api = FakeAttendeeApi()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{EVENT}/sessions/SEC301").respond(200, json={"session": wire_session("SEC301")})
        api.mount(mock)
        mock.post(f"{EVENT}/reservations").respond(409, json={"message": "Reservations closed"})

        result = await write_stack.agent.optimize_and_book_session("SEC301")

        favorites = [c for c in mock.calls if c.request.url.path.endswith("/favorites")]
    assert result.status == "FAVORITED"
    assert len(favorites) == 1 and api.favorites == ["SEC301"]


async def test_dry_run_favorites_without_calling_reserve(stack):
    api = FakeAttendeeApi()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{EVENT}/sessions/SEC301").respond(200, json={"session": wire_session("SEC301")})
        api.mount(mock)

        result = await stack.agent.optimize_and_book_session("SEC301")

        reserves = [c for c in mock.calls if c.request.url.path.endswith("/reservations")]
    assert result.status == "FAVORITED"
    assert reserves == []


async def test_snapper_throttled_reserve_is_not_retried_and_keeps_the_entry(write_stack, waitlist):
    waitlist.add("A")
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{EVENT}/sessions/A").respond(200, json={"session": wire_session("A")})
        mock.get(f"{EVENT}/schedule").respond(200, json=wire_schedule())
        reserve = mock.post(f"{EVENT}/reservations").respond(429, headers={"Retry-After": "30"})

        await write_stack.snapper.run_cycle()

    assert reserve.call_count == 1, "the quota governor owns waiting; the snapper never retries"
    [entry] = waitlist.entries()
    assert entry.status == "WATCHING"
    assert "THROTTLED" in [e["action"] for e in read_audit(write_stack.config.planner_db_path)]


async def test_snapper_swap_scenario_proposes_and_leaves_the_reservation(write_stack, waitlist):
    # A (Venetian, 10:00) opens up but overlaps the booked B (Venetian, 10:30).
    api = FakeAttendeeApi(reserved=["B"])
    waitlist.add("A")
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{EVENT}/sessions/A").respond(200, json={"session": wire_session("A")})
        mock.get(f"{EVENT}/sessions/B").respond(
            200, json={"session": wire_session("B", time="10:30")}
        )
        api.mount(mock)

        await write_stack.snapper.run_cycle()

        writes = [c.request.method for c in mock.calls if c.request.method != "GET"]
    assert writes == [], "the snapper never cancels and never reserves over a clash"
    assert api.reserved == ["B"]
    [entry] = waitlist.entries()
    assert entry.status == "SWAP_PROPOSED"
    audit = read_audit(write_stack.config.planner_db_path)
    assert ("SWAP_PROPOSED", "A") in [(e["action"], e["session_id"]) for e in audit]
    assert write_stack.notifications == [
        "Seat open but overlaps B; review the swap in the concierge"
    ]


async def test_transit_buffer_is_proposed_then_created_through_the_write_service(write_stack):
    api = FakeAttendeeApi(reserved=["A", "B"])
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{EVENT}/sessions/A").respond(
            200,
            json={"session": wire_session("A", venue="Mandalay Bay", time="09:00", length="60")},
        )
        mock.get(f"{EVENT}/sessions/B").respond(
            200, json={"session": wire_session("B", venue="Venetian", time="11:00")}
        )
        api.mount(mock)

        schedule = await write_stack.reader.read()
        buffers, unresolved = write_stack.conflicts.propose_transit_buffers(schedule)
        result = await write_stack.approvals.apply_immediately(EVENT_ID, buffers)
        again, _ = write_stack.conflicts.propose_transit_buffers(await write_stack.reader.read())

        creates = [c for c in mock.calls if c.request.url.path.endswith("/personal-time")]
    assert result.state == "APPLIED" and unresolved == []
    [create] = creates
    body = json.loads(create.request.read())
    assert body["title"] == "Transit: Mandalay Bay → Venetian"
    assert body["description"].startswith("concierge-transit:B:Travel 45 min")
    assert (body["startDateTime"], body["endDateTime"]) == (
        "2026-12-01T18:00:00",
        "2026-12-01T18:45:00",
    )
    assert again == [], "an existing concierge buffer must not be duplicated"


async def test_agent_swap_is_proposed_then_executed_only_after_approval(write_stack):
    api = FakeAttendeeApi(reserved=["B"])
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{EVENT}/sessions/A").respond(
            200, json={"session": wire_session("A", venue="Venetian", time="10:10")}
        )
        mock.get(f"{EVENT}/sessions/B").respond(
            200, json={"session": wire_session("B", venue="Mandalay Bay", time="09:00")}
        )
        api.mount(mock)

        proposal = await write_stack.agent.optimize_and_book_session("A")
        writes_before = [c.request.method for c in mock.calls if c.request.method != "GET"]
        result = await write_stack.agent.optimize_and_book_session(
            "A", apply_run_id=proposal.apply_run_id, approval_token=proposal.approval_token
        )
        writes = [
            (c.request.method, c.request.url.path.rsplit("/", 1)[-1])
            for c in mock.calls
            if c.request.method != "GET"
        ]

    assert proposal.status == "CONFLICT_DETECTED" and proposal.approval_token
    assert writes_before == [], "a proposal never writes"
    assert result.status == "CONFIRMED" and result.canceled_sessions == ["B"]
    assert writes == [("DELETE", "B"), ("POST", "reservations")]
    assert api.reserved == ["A"]
    assert proposal.approval_token not in audit_dump_text(write_stack.config.planner_db_path)


async def test_snapper_reserves_without_canceling_a_transit_neighbour(
    write_stack, waitlist, write_config
):
    # B (Mandalay Bay, 09:00) is unreachable from A (Venetian, 10:10); it stays booked.
    waitlist.add("A")
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{EVENT}/sessions/A").respond(
            200, json={"session": wire_session("A", venue="Venetian", time="10:10")}
        )
        mock.get(f"{EVENT}/sessions/B").respond(
            200, json={"session": wire_session("B", venue="Mandalay Bay", time="09:00")}
        )
        reserve = mock.post(f"{EVENT}/reservations").respond(200, json=wire_bulk(["A"]))
        mock.get(f"{EVENT}/schedule").respond(200, json=wire_schedule(reserved=["A", "B"]))
        cancel = mock.delete(f"{EVENT}/reservations/B").respond(204)

        await write_stack.snapper.run_cycle()

    assert reserve.call_count == 1 and cancel.call_count == 0
    entries = read_audit(write_config.planner_db_path)
    audit = [(e["action"], e["session_id"], e["status"]) for e in entries]
    assert ("RESERVE_SESSION", "A", "CONFIRMED") in audit
    assert ("TRANSIT_CONFLICT", "A", "MANUAL_REQUIRED") in audit
    assert all(action != "CANCEL_RESERVATION" for action, _, _ in audit)


async def test_planner_foundation_captures_snapshots_coordinates_quota_and_reconciles(
    config,
):
    store = PlannerStateStore(config.planner_db_path)
    await store.initialize()
    snapshots = ScheduleSnapshotService(store)
    snapshot = await snapshots.capture(
        config.aws_events_event_id,
        AttendeeSchedule(booked=[make_session("AI301")]),
    )

    assert await snapshots.latest(config.aws_events_event_id) == snapshot
    assert config.planner_db_path.stat().st_mode & 0o777 == 0o600

    quotas = {"GetSchedule": OperationQuota(1, "request")}
    first = SQLiteOperationQuotaGovernor(store, config.aws_events_event_id, quotas=quotas)
    second = SQLiteOperationQuotaGovernor(store, config.aws_events_event_id, quotas=quotas)
    assert await first.acquire("GetSchedule", timeout_seconds=0)
    assert not await second.acquire("GetSchedule", timeout_seconds=0)

    journal = ApplyJournalStore(store)
    run = await journal.create_run(config.aws_events_event_id, uuid4(), snapshot.fingerprint)
    operation = await journal.add_operation(run.run_id, 1, "RESERVE", {"session_ids": ["AI301"]})
    await journal.mark_attempting(operation.operation_id)
    reconciled = await journal.reconcile_attempting(
        run.run_id,
        AttendeeSchedule(),
        lambda _operation, _schedule: "CONFIRMED",
    )

    assert [(item.operation_id, item.state) for item in reconciled] == [
        (operation.operation_id, "CONFIRMED")
    ]


async def test_planner_rejects_tokens_and_runtime_lease_excludes_second_coordinator(
    config, audit, credentials
):
    store = PlannerStateStore(config.planner_db_path)
    await store.initialize()
    journal = ApplyJournalStore(store)
    run = await journal.create_run(config.aws_events_event_id, uuid4(), "snapshot")

    with pytest.raises(ValueError, match="sensitive key"):
        await journal.add_operation(run.run_id, 1, "RESERVE", {"access_token": "do-not-store"})

    audit.record("PLANNER_ACCEPTANCE")
    persisted_state = config.planner_db_path.read_text(errors="ignore")
    assert "do-not-store" not in persisted_state
    assert credentials.get_token() not in audit_dump_text(config.planner_db_path)

    first = RuntimeCoordinator(store, config.aws_events_event_id)
    second = RuntimeCoordinator(store, config.aws_events_event_id)
    await first.start()
    try:
        with pytest.raises(RuntimeLeaseError, match="Runtime already running"):
            await second.start()
    finally:
        await first.aclose()


@respx.mock
async def test_corrupt_vector_row_degrades_to_keyword_search(stack):
    catalog = synthetic_catalog()
    respx.get(EVENT).respond(200, json=EVENT_PAYLOAD)
    respx.get(f"{EVENT}/sessions").respond(200, json={"items": catalog, "totalCount": len(catalog)})
    await stack.ingestion.initial_sync()
    with sqlite3.connect(stack.config.local_db_path) as db:
        db.execute("UPDATE session_vectors SET vector = ? WHERE session_id = 'S000'", (b"short",))
        db.commit()

    restarted = IngestionService(stack.config, stack.client, embedder=FakeEmbedder())
    await restarted.initialize()

    assert restarted.vector_search_state == "available"
    assert (await restarted.search("generative AI"))[0].session_id == "S000"
