"""Tier 3 - reservations (LIVE_ALLOW_RESERVATIONS=1). Before reservations open through the
API (October 8, 2026) every reserve/cancel returns 409, and these tests verify how that is
handled. Once open, they take a real seat and give it back straight away.

Only sessions that fit the attendee's current schedule are used. Booking never cancels
anything without an approval token; cancels here use the agent's two-step approval.
"""

from datetime import datetime, timezone

import pytest

from reinvent_concierge.agent import MCPAgent
from reinvent_concierge.audit import AuditLogger
from reinvent_concierge.exceptions import ReservationConflictError
from reinvent_concierge.ingestion import IngestionService
from reinvent_concierge.models import Session
from reinvent_concierge.schedule import ConflictManager
from reinvent_concierge.snapper import SeatSnapperWorker
from reinvent_concierge.transit import TransitEngine
from reinvent_concierge.waitlist import WaitlistStore
from tests.factories import build_approvals, read_audit
from tests.live.conftest import BULK_FAILURE_CODES, pick_untouched

pytestmark = [pytest.mark.live, pytest.mark.usefixtures("require_reservations")]

SEATS_LEFT = {"available", "limited", "veryLimited"}


async def conflict_free_candidate(client, catalog) -> Session:
    """A future, reservable session with seats that clashes with nothing already booked."""
    schedule = await client.get_schedule(include_favorites=False)
    manager = ConflictManager(TransitEngine())
    now = datetime.now(timezone.utc)

    def fits(session: Session) -> bool:
        if not (session.is_reservable and session.seat_availability in SEATS_LEFT):
            return False
        if session.start_time <= now:
            return False
        candidate = schedule.model_copy(update={"booked": [*schedule.booked, session]})
        return not manager.conflicts_involving(candidate, session.session_id)

    return pick_untouched(catalog, await client.get_raw_schedule(), fits)


async def test_reserve_then_cancel(write_client, catalog):
    session = await conflict_free_candidate(write_client, catalog)
    confirmed = False
    try:
        try:
            [result] = await write_client.reserve_sessions([session.session_id])
        except ReservationConflictError as e:
            assert e.status_code == 409  # reservations not open through the API yet
            return

        if result.status != "CONFIRMED":
            assert result.code in BULK_FAILURE_CODES, f"new refusal code {result.code}"
            return
        confirmed = True
        assert session.session_id in (await write_client.get_raw_schedule())["reserved"]

        await write_client.cancel_reservation(session.session_id)
        confirmed = False
        assert session.session_id not in (await write_client.get_raw_schedule())["reserved"]
        await write_client.cancel_reservation(session.session_id)  # 404 on a repeat is done
    finally:
        if confirmed:
            await write_client.cancel_reservation(session.session_id)


async def test_agent_booking_with_write_access(
    live_write_config, live_credentials, write_client, catalog, embedder
):
    session = await conflict_free_candidate(write_client, catalog)
    ingestion = IngestionService(live_write_config, write_client, embedder=embedder)
    await ingestion.initialize()
    transit = TransitEngine()
    approvals, reader = await build_approvals(
        live_write_config, write_client, ingestion.get_session_local
    )
    agent = MCPAgent(
        live_write_config,
        write_client,
        ingestion,
        transit,
        ConflictManager(transit),
        AuditLogger(live_write_config.planner_db_path, live_credentials.hash_token),
        WaitlistStore(live_write_config.waitlist_path),
        approvals=approvals,
        reader=reader,
    )
    result = None
    try:
        result = await agent.optimize_and_book_session(
            session.session_id, auto_resolve_conflicts=False
        )
        assert result.status in {"CONFIRMED", "FAVORITED", "FAILED"}
        raw = await write_client.get_raw_schedule()
        if result.status == "CONFIRMED":
            assert session.session_id in raw["reserved"]
            assert result.canceled_sessions == []

            proposal = await agent.cancel_reservation(session.session_id)
            assert proposal.status == "APPROVAL_REQUIRED"
            assert session.session_id in (await write_client.get_raw_schedule())["reserved"]
            done = await agent.cancel_reservation(
                session.session_id,
                apply_run_id=proposal.apply_run_id,
                approval_token=proposal.approval_token,
            )
            assert done.status == "CANCELED"
            assert proposal.approval_token not in live_write_config.audit_log_path.read_text()
        elif result.status == "FAVORITED":  # reservations closed: HTTP 409 fallback
            assert session.session_id in raw["favorites"]
    finally:
        raw = await write_client.get_raw_schedule()
        if session.session_id in raw["reserved"]:
            await write_client.cancel_reservation(session.session_id)
        await write_client.disassociate_favorite(session.session_id)


async def test_snapper_live_cycle(
    live_write_config, live_credentials, write_client, catalog, embedder
):
    session = await conflict_free_candidate(write_client, catalog)
    ingestion = IngestionService(live_write_config, write_client, embedder=embedder)
    await ingestion.initialize()
    waitlist = WaitlistStore(live_write_config.waitlist_path)
    waitlist.add(session.session_id, priority=1, title=session.title)
    approvals, reader = await build_approvals(
        live_write_config, write_client, ingestion.get_session_local
    )
    worker = SeatSnapperWorker(
        live_write_config,
        ingestion,
        ConflictManager(TransitEngine()),
        AuditLogger(live_write_config.planner_db_path, live_credentials.hash_token),
        waitlist,
        approvals,
        reader,
        notifier=lambda *_: None,
    )
    try:
        await worker.run_cycle()

        reserve = [
            e
            for e in read_audit(live_write_config.audit_log_path)
            if e["action"] == "RESERVE_SESSION"
        ]
        if not reserve:  # the seat filled up, or the attendee's schedule changed meanwhile
            [entry] = waitlist.entries()
            assert entry.status in {"FULL", "SWAP_PROPOSED", "WATCHING"}
            return
        outcome = reserve[-1]["status"]
        assert outcome in {"CONFIRMED", "DEFERRED", "FAILED"}
        if outcome == "CONFIRMED":
            assert waitlist.entries() == [], "resolved entries leave the waitlist"
        else:
            assert [e.session_id for e in waitlist.entries()] == [session.session_id]
    finally:
        if session.session_id in (await write_client.get_raw_schedule())["reserved"]:
            await write_client.cancel_reservation(session.session_id)
