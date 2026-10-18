"""SPEC-06: MCP agent tool behavior and audit logging."""

import hashlib
from typing import Dict, List
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from reinvent_concierge.agent import MCPAgent
from reinvent_concierge.audit import AuditLogger
from reinvent_concierge.exceptions import AWSEventsAPIError, ReservationConflictError
from reinvent_concierge.models import (
    AttendeeSchedule,
    BulkFailure,
    BulkResult,
    PersonalTimeBlock,
    ReservationResult,
    Session,
    SessionSummary,
)
from reinvent_concierge.planner import (
    AttendeePreferenceProfile,
    PreferenceProfileStore,
    WeightedInterestQuery,
)
from reinvent_concierge.planner.state_store import PlannerStateStore
from reinvent_concierge.schedule import ConflictManager
from reinvent_concierge.transit import TransitEngine
from tests.factories import TOKEN, at, audit_dump_text, build_approvals, make_session, read_audit


def confirmed(session_id):
    return [ReservationResult(session_id=session_id, status="CONFIRMED")]


class AttendeeState:
    """Stateful stand-in for the attendee's AWS schedule: writes change what reads return."""

    def __init__(self) -> None:
        self.sessions: Dict[str, Session] = {}
        self.booked: Dict[str, Session] = {}
        self.favorites: Dict[str, Session] = {}
        self.personal_time: List[PersonalTimeBlock] = []

    def known(self, *sessions: Session) -> None:
        self.sessions.update({session.session_id: session for session in sessions})

    def book(self, *sessions: Session) -> None:
        self.known(*sessions)
        self.booked.update({session.session_id: session for session in sessions})

    async def get_session(self, session_id):
        return self.sessions[session_id]

    async def get_schedule(
        self, include_favorites=True, include_personal_time=True, session_resolver=None
    ):
        return AttendeeSchedule(
            booked=list(self.booked.values()),
            favorites=list(self.favorites.values()) if include_favorites else [],
            personal_time=list(self.personal_time) if include_personal_time else [],
        )

    async def reserve_sessions(self, session_ids):
        results = []
        for session_id in session_ids:
            session = self.sessions[session_id]
            clashes = [
                other.session_id
                for other in self.booked.values()
                if other.session_id != session_id
                and other.start_time < session.end_time
                and session.start_time < other.end_time
            ]
            if clashes:
                results.append(
                    ReservationResult(
                        session_id=session_id,
                        status="FAILED",
                        code="scheduleConflict",
                        message="Reservation refused: scheduleConflict",
                        conflicts_with=clashes,
                    )
                )
                continue
            self.booked[session_id] = session
            results.append(ReservationResult(session_id=session_id, status="CONFIRMED"))
        return results

    async def cancel_reservation(self, session_id):
        self.booked.pop(session_id, None)

    async def associate_favorites(self, session_ids):
        result = BulkResult()
        for session_id in session_ids:
            if session_id in self.favorites:
                result.failed.append(BulkFailure(session_id=session_id, code="alreadyFavorited"))
                continue
            self.favorites[session_id] = self.sessions.get(session_id) or make_session(session_id)
            result.successful.append(session_id)
        return result

    async def disassociate_favorite(self, session_id):
        self.favorites.pop(session_id, None)

    async def create_personal_time(self, title, start_time, end_time, description, location=None):
        block_id = f"pt-{len(self.personal_time) + 1}"
        self.personal_time.append(
            PersonalTimeBlock(
                id=block_id,
                title=title,
                start_time=start_time,
                end_time=end_time,
                description=description,
                location=location,
            )
        )
        return block_id


_CLIENT_METHODS = (
    "get_session",
    "get_schedule",
    "reserve_sessions",
    "cancel_reservation",
    "associate_favorites",
    "disassociate_favorite",
    "create_personal_time",
)


@pytest.fixture
def state():
    return AttendeeState()


@pytest.fixture
def client(state):
    rest_client = Mock()
    for name in _CLIENT_METHODS:
        setattr(rest_client, name, AsyncMock(side_effect=getattr(state, name)))
    return rest_client


@pytest.fixture
def ingestion():
    service = Mock()
    service.search = AsyncMock(return_value=[])
    service.get_session_local = AsyncMock(return_value=None)
    return service


async def build_agent(config, client, ingestion, credentials, waitlist, profiles=None):
    transit = TransitEngine()
    approvals, reader = await build_approvals(config, client)
    return MCPAgent(
        config,
        client,
        ingestion,
        transit,
        ConflictManager(transit),
        AuditLogger(config.planner_db_path, credentials.hash_token),
        waitlist,
        profiles,
        approvals=approvals,
        reader=reader,
    )


@pytest.fixture
async def profiles(config):
    store = PlannerStateStore(config.planner_db_path)
    await store.initialize()
    return PreferenceProfileStore(store)


@pytest.fixture
async def agent(config, client, ingestion, credentials, waitlist):
    return await build_agent(config, client, ingestion, credentials, waitlist)


@pytest.fixture
async def write_agent(write_config, client, ingestion, credentials, waitlist, profiles):
    return await build_agent(write_config, client, ingestion, credentials, waitlist, profiles)


class TestSearchCatalog:
    async def test_delegates_with_normalized_venue(self, agent, ingestion):
        result = await agent.search_catalog("Bedrock", level=300, venue="the venetian")

        assert result.total_found == 0
        assert ingestion.search.await_args.kwargs["venue"] == "Venetian"
        assert ingestion.search.await_args.kwargs["level"] == 300

    async def test_normalizes_venue_alias(self, agent, ingestion):
        # Regression guard: the registry maps the alias to its canonical venue.
        await agent.search_catalog("security", venue="Palazzo")

        assert ingestion.search.await_args.kwargs["venue"] == "Venetian"

    async def test_forwards_session_type_and_topic(self, agent, ingestion):
        await agent.search_catalog("security", session_type="Workshop", topic="Security")

        kwargs = ingestion.search.await_args.kwargs
        assert (kwargs["session_type"], kwargs["topic"]) == ("Workshop", "Security")

    async def test_invalid_input_is_rejected_and_audited(self, agent, ingestion, config):
        with pytest.raises(ValidationError):
            await agent.search_catalog("Bedrock", limit=50)

        ingestion.search.assert_not_awaited()
        assert read_audit(config.planner_db_path)[0]["status"] == "FAILED"


class TestEventLocalOutputs:
    async def test_search_and_schedule_outputs_carry_event_local_times(
        self, agent, ingestion, state
    ):
        agent.config = agent.config.model_copy(update={"event_timezone": "America/Los_Angeles"})
        ingestion.search.return_value = [
            SessionSummary(
                **make_session("SEC301").model_dump(include=set(SessionSummary.model_fields))
            )
        ]
        booked = make_session("BOOKED")
        favorite = make_session("FAV", start=at(20))
        state.book(booked)
        state.favorites[favorite.session_id] = favorite

        result = await agent.search_catalog("security")
        assert result.sessions and result.sessions[0].start_local is not None
        assert result.sessions[0].start_local.endswith("-08:00")

        schedule = await agent.get_my_schedule()
        assert schedule.event_timezone == "America/Los_Angeles"
        assert schedule.booked and all(session.start_local for session in schedule.booked)
        assert schedule.favorites and all(session.start_local for session in schedule.favorites)
        # Storage and transport stay UTC; only the *_local display fields are added.
        assert schedule.booked[0].start_time == booked.start_time
        assert schedule.favorites[0].start_time == favorite.start_time


class TestOptimizeAndBook:
    async def test_favorites_when_write_access_inactive(self, agent, client, config, state):
        state.known(make_session("SEC301", title="Security Session"))

        result = await agent.optimize_and_book_session("SEC301")

        assert result.status == "FAVORITED"
        client.associate_favorites.assert_awaited_once_with(["SEC301"])
        client.reserve_sessions.assert_not_awaited()
        [entry] = read_audit(config.planner_db_path)
        assert entry["action"] == "[DRY_RUN] optimize_and_book_session"
        assert entry["attendee_token_hash"] == hashlib.sha256(TOKEN.encode()).hexdigest()

    async def test_confirms_and_adds_buffer_on_venue_change(self, write_agent, client, state):
        earlier = make_session("EARLY", "Mandalay Bay", start=at(18))
        state.book(earlier)
        state.known(make_session("SEC301", "Venetian", start=at(20)))

        result = await write_agent.optimize_and_book_session("SEC301")

        assert result.status == "CONFIRMED"
        assert result.transit_buffer_added == "Transit: Mandalay Bay → Venetian"
        title, start, end, description, location = client.create_personal_time.await_args.args
        assert (start, end, location) == (at(19), at(19, 45), "Mandalay Bay")
        assert description.startswith("concierge-transit:SEC301:Travel 45 min")
        client.cancel_reservation.assert_not_awaited()

    async def test_reports_conflict_without_booking_when_not_auto_resolving(
        self, write_agent, client, state
    ):
        state.book(make_session("OLD", "Mandalay Bay", start=at(18)))
        state.known(make_session("NEW", "Venetian", start=at(19, 10)))

        result = await write_agent.optimize_and_book_session("NEW", auto_resolve_conflicts=False)

        assert result.status == "CONFLICT_DETECTED"
        client.reserve_sessions.assert_not_awaited()

    async def test_conflicting_booking_proposes_swap_and_cancels_nothing(
        self, write_agent, client, state
    ):
        # OLD (booked) overlaps NEW in time.
        state.book(make_session("OLD", "Venetian", start=at(18)))
        state.known(make_session("NEW", "Venetian", start=at(18, 30)))

        result = await write_agent.optimize_and_book_session("NEW")

        assert result.status == "CONFLICT_DETECTED"
        assert result.approval_token and result.apply_run_id
        assert any("OLD" in change for change in result.proposed_changes)
        client.cancel_reservation.assert_not_awaited()
        client.reserve_sessions.assert_not_awaited()
        assert result.proposed_changes == ["Cancel OLD (Session OLD)", "Reserve NEW"]
        assert result.expires_at is not None
        assert result.message.startswith("Approve to swap: ")

    async def test_approved_swap_executes_once(self, write_agent, client, state):
        state.book(make_session("OLD", "Venetian", start=at(18)))
        state.known(make_session("NEW", "Venetian", start=at(18, 30)))

        proposal = await write_agent.optimize_and_book_session("NEW")
        result = await write_agent.optimize_and_book_session(
            "NEW", apply_run_id=proposal.apply_run_id, approval_token=proposal.approval_token
        )

        assert result.status == "CONFIRMED" and result.canceled_sessions == ["OLD"]
        client.cancel_reservation.assert_awaited_once_with("OLD")
        assert set(state.booked) == {"NEW"}

        replay = await write_agent.optimize_and_book_session(
            "NEW", apply_run_id=proposal.apply_run_id, approval_token=proposal.approval_token
        )
        assert replay.status == "FAILED" and "consumed" in replay.message
        client.cancel_reservation.assert_awaited_once()

    async def test_must_attend_blocker_gets_no_token(
        self, write_agent, client, state, profiles, write_config
    ):
        # Save a profile whose must_attend_session_ids includes OLD.
        await profiles.save(
            AttendeePreferenceProfile(
                event_id=write_config.aws_events_event_id,
                name="Plan",
                interest_queries=[WeightedInterestQuery(query="serverless", weight=100)],
                must_attend_session_ids=frozenset({"OLD"}),
            ),
            None,
        )
        state.book(make_session("OLD", "Venetian", start=at(18)))
        state.known(make_session("NEW", "Venetian", start=at(18, 30)))

        result = await write_agent.optimize_and_book_session("NEW")

        assert result.status == "CONFLICT_DETECTED" and result.approval_token is None
        assert "must-attend" in result.message
        assert result.message == "Not booked: conflicts with must-attend OLD"
        assert result.proposed_changes == [] and result.apply_run_id is None
        client.cancel_reservation.assert_not_awaited()

    async def test_cancel_reservation_requires_approval(self, write_agent, client, state):
        state.book(make_session("OLD"))

        proposal = await write_agent.cancel_reservation("OLD")
        assert proposal.status == "APPROVAL_REQUIRED"
        client.cancel_reservation.assert_not_awaited()
        done = await write_agent.cancel_reservation(
            "OLD", apply_run_id=proposal.apply_run_id, approval_token=proposal.approval_token
        )
        assert done.status == "CANCELED"
        client.cancel_reservation.assert_awaited_once_with("OLD")

    async def test_cancel_with_a_wrong_token_fails_and_cancels_nothing(
        self, write_agent, client, state
    ):
        state.book(make_session("OLD"))

        proposal = await write_agent.cancel_reservation("OLD")
        done = await write_agent.cancel_reservation(
            "OLD", apply_run_id=proposal.apply_run_id, approval_token="not-the-token"
        )

        assert done.status == "FAILED" and "invalid" in done.message
        client.cancel_reservation.assert_not_awaited()

    async def test_a_token_without_its_run_id_is_refused(self, write_agent, client, state):
        state.book(make_session("OLD"))
        proposal = await write_agent.cancel_reservation("OLD")

        done = await write_agent.cancel_reservation("OLD", approval_token=proposal.approval_token)

        assert done.status == "FAILED" and "together" in done.message
        client.cancel_reservation.assert_not_awaited()

    async def test_approval_tokens_are_never_audited(self, write_agent, config):
        proposal = await write_agent.cancel_reservation("OLD")
        await write_agent.cancel_reservation(
            "OLD", apply_run_id=proposal.apply_run_id, approval_token=proposal.approval_token
        )
        assert proposal.approval_token not in audit_dump_text(config.planner_db_path)

    async def test_a_rejected_token_argument_is_redacted_from_failure_details(
        self, write_agent, config
    ):
        with pytest.raises(ValidationError):
            await write_agent.cancel_reservation("OLD", apply_run_id="r", approval_token=987654321)

        [entry] = read_audit(config.planner_db_path)
        assert entry["status"] == "FAILED" and "[REDACTED]" in entry["details"]
        assert "987654321" not in audit_dump_text(config.planner_db_path)

    async def test_swap_tokens_are_never_audited(self, write_agent, config, state):
        state.book(make_session("OLD", "Venetian", start=at(18)))
        state.known(make_session("NEW", "Venetian", start=at(18, 30)))

        proposal = await write_agent.optimize_and_book_session("NEW")
        await write_agent.optimize_and_book_session(
            "NEW", apply_run_id=proposal.apply_run_id, approval_token=proposal.approval_token
        )

        assert proposal.approval_token
        assert proposal.approval_token not in audit_dump_text(config.planner_db_path)

    async def test_transit_neighbour_is_reported_not_canceled(self, write_agent, client, state):
        # NEW confirms; a later booked session at another venue becomes unreachable.
        state.book(make_session("OLD", "Mandalay Bay", start=at(19, 10)))
        state.known(make_session("NEW", "Venetian", start=at(18)))

        result = await write_agent.optimize_and_book_session("NEW")
        assert result.status == "CONFLICT_DETECTED"  # proposed as a swap, not auto-canceled
        client.cancel_reservation.assert_not_awaited()
        assert result.approval_token and result.proposed_changes[0].startswith("Cancel OLD")

    async def test_unresolvable_neighbour_after_booking_is_listed_not_canceled(
        self, write_agent, client, state
    ):
        # The neighbour is unreachable only once a seat is held elsewhere: the API books NEW,
        # then the post-booking read shows a CANCEL_ONE pair that is reported, never canceled.
        a = make_session("A", "Mandalay Bay", start=at(13), minutes=30)
        b = make_session("B", "Venetian", start=at(13, 50))
        state.book(a, b)
        state.known(make_session("NEW", "Wynn", start=at(20)))

        result = await write_agent.optimize_and_book_session("NEW")

        assert result.status == "CONFIRMED"
        assert "still conflicting: A -> B" in result.message
        client.cancel_reservation.assert_not_awaited()

    async def test_api_time_clash_without_known_blocker_is_conflict_detected(
        self, write_agent, client, state
    ):
        state.known(make_session("NEW", "Venetian", start=at(18)))
        client.reserve_sessions.side_effect = [
            [
                ReservationResult(
                    session_id="NEW",
                    status="FAILED",
                    code="scheduleConflict",
                    conflicts_with=["OLD"],
                )
            ]
        ]

        result = await write_agent.optimize_and_book_session("NEW")

        assert result.status == "CONFLICT_DETECTED"
        assert "scheduleConflict" in result.message and result.approval_token is None
        client.cancel_reservation.assert_not_awaited()

    async def test_full_session_fails(self, write_agent, client, state):
        state.known(make_session("NEW"))
        client.reserve_sessions.side_effect = [
            [
                ReservationResult(
                    session_id="NEW",
                    status="FAILED",
                    code="sessionFull",
                    message="Reservation refused: sessionFull",
                )
            ]
        ]

        result = await write_agent.optimize_and_book_session("NEW")

        assert result.status == "FAILED"
        assert "sessionFull" in result.message

    async def test_http_409_falls_back_to_favorites(self, write_agent, client, state):
        state.known(make_session("SEC301"))
        client.reserve_sessions.side_effect = ReservationConflictError(409, "closed")

        result = await write_agent.optimize_and_book_session("SEC301")

        assert result.status == "FAVORITED"
        client.associate_favorites.assert_awaited_once_with(["SEC301"])


async def mark_must_attend(profiles, config, *session_ids):
    await profiles.save(
        AttendeePreferenceProfile(
            event_id=config.aws_events_event_id,
            name="Plan",
            interest_queries=[WeightedInterestQuery(query="serverless", weight=100)],
            must_attend_session_ids=frozenset(session_ids),
        ),
        None,
    )


def book_overlapping_old_and_new(state):
    state.book(make_session("OLD", "Venetian", start=at(18)))
    state.known(make_session("NEW", "Venetian", start=at(18, 30)))


class TestApprovalBinding:
    """A token executes only the change, tool and session it was issued for."""

    async def test_cancel_token_cannot_book_through_optimize(self, write_agent, client, state):
        state.book(make_session("OLD", "Venetian", start=at(10)))
        state.known(make_session("NEW", "Venetian", start=at(14)))
        proposal = await write_agent.cancel_reservation("OLD")

        result = await write_agent.optimize_and_book_session(
            "NEW", apply_run_id=proposal.apply_run_id, approval_token=proposal.approval_token
        )

        assert result.status == "FAILED" and "another change" in result.message
        client.cancel_reservation.assert_not_awaited()
        client.reserve_sessions.assert_not_awaited()
        done = await write_agent.cancel_reservation(
            "OLD", apply_run_id=proposal.apply_run_id, approval_token=proposal.approval_token
        )
        assert done.status == "CANCELED"  # still PREVIEWED: the token was not consumed

    async def test_cancel_token_is_bound_to_its_session(self, write_agent, client, state):
        state.book(
            make_session("OLD", "Venetian", start=at(10)),
            make_session("KEEP", "Venetian", start=at(14)),
        )
        proposal = await write_agent.cancel_reservation("OLD")

        result = await write_agent.cancel_reservation(
            "KEEP", apply_run_id=proposal.apply_run_id, approval_token=proposal.approval_token
        )

        assert result.status == "FAILED" and "another change" in result.message
        client.cancel_reservation.assert_not_awaited()
        assert set(state.booked) == {"OLD", "KEEP"}
        done = await write_agent.cancel_reservation(
            "OLD", apply_run_id=proposal.apply_run_id, approval_token=proposal.approval_token
        )
        assert done.status == "CANCELED" and set(state.booked) == {"KEEP"}

    async def test_swap_token_cannot_be_used_to_cancel(self, write_agent, client, state):
        book_overlapping_old_and_new(state)
        proposal = await write_agent.optimize_and_book_session("NEW")

        result = await write_agent.cancel_reservation(
            "ZZZ", apply_run_id=proposal.apply_run_id, approval_token=proposal.approval_token
        )

        assert result.status == "FAILED" and "another change" in result.message
        client.cancel_reservation.assert_not_awaited()
        client.reserve_sessions.assert_not_awaited()
        done = await write_agent.optimize_and_book_session(
            "NEW", apply_run_id=proposal.apply_run_id, approval_token=proposal.approval_token
        )
        assert done.status == "CONFIRMED" and done.canceled_sessions == ["OLD"]

    async def test_swap_token_is_bound_to_its_target(self, write_agent, client, state):
        book_overlapping_old_and_new(state)
        state.known(make_session("OTHER", "Venetian", start=at(18, 30)))
        proposal = await write_agent.optimize_and_book_session("NEW")

        result = await write_agent.optimize_and_book_session(
            "OTHER", apply_run_id=proposal.apply_run_id, approval_token=proposal.approval_token
        )

        assert result.status == "FAILED" and "another change" in result.message
        client.cancel_reservation.assert_not_awaited()

    async def test_must_attend_cancel_is_refused_at_preview(
        self, write_agent, client, state, profiles, write_config
    ):
        await mark_must_attend(profiles, write_config, "OLD")
        state.book(make_session("OLD"))

        proposal = await write_agent.cancel_reservation("OLD")

        assert proposal.status == "FAILED" and proposal.approval_token is None
        assert proposal.apply_run_id is None
        assert "OLD is must-attend" in proposal.message
        client.cancel_reservation.assert_not_awaited()

    async def test_must_attend_cancel_is_refused_at_execute(
        self, write_agent, client, state, profiles, write_config
    ):
        state.book(make_session("OLD"))
        proposal = await write_agent.cancel_reservation("OLD")
        await mark_must_attend(profiles, write_config, "OLD")

        result = await write_agent.cancel_reservation(
            "OLD", apply_run_id=proposal.apply_run_id, approval_token=proposal.approval_token
        )

        assert result.status == "FAILED" and "OLD is must-attend" in result.message
        client.cancel_reservation.assert_not_awaited()
        await profiles.reset(write_config.aws_events_event_id)
        done = await write_agent.cancel_reservation(
            "OLD", apply_run_id=proposal.apply_run_id, approval_token=proposal.approval_token
        )
        assert done.status == "CANCELED"

    async def test_must_attend_marked_after_a_swap_preview_is_not_released(
        self, write_agent, client, state, profiles, write_config
    ):
        book_overlapping_old_and_new(state)
        proposal = await write_agent.optimize_and_book_session("NEW")
        await mark_must_attend(profiles, write_config, "OLD")

        result = await write_agent.optimize_and_book_session(
            "NEW", apply_run_id=proposal.apply_run_id, approval_token=proposal.approval_token
        )

        assert result.status == "FAILED" and "OLD is must-attend" in result.message
        client.cancel_reservation.assert_not_awaited()
        assert set(state.booked) == {"OLD"}
        await profiles.reset(write_config.aws_events_event_id)
        done = await write_agent.optimize_and_book_session(
            "NEW", apply_run_id=proposal.apply_run_id, approval_token=proposal.approval_token
        )
        assert done.status == "CONFIRMED"

    @pytest.mark.parametrize("run_id", ["not-a-uuid", "6ddd726e-6d25-4cf4-9c04-92bdec912839"])
    async def test_unknown_or_malformed_run_fails_both_tools(
        self, write_agent, client, state, run_id
    ):
        book_overlapping_old_and_new(state)

        booked = await write_agent.optimize_and_book_session(
            "NEW", apply_run_id=run_id, approval_token="token"
        )
        canceled = await write_agent.cancel_reservation(
            "OLD", apply_run_id=run_id, approval_token="token"
        )

        assert booked.status == "FAILED" and "approval run was not found" in booked.message
        assert canceled.status == "FAILED" and "approval run was not found" in canceled.message
        client.cancel_reservation.assert_not_awaited()
        client.reserve_sessions.assert_not_awaited()


class TestTransitBuffersAfterBooking:
    async def test_buffer_that_cannot_be_added_is_reported(self, write_agent, client, state):
        state.book(make_session("EARLY", "Mandalay Bay", start=at(18)))
        state.known(make_session("SEC301", "Venetian", start=at(20)))
        client.create_personal_time.side_effect = AWSEventsAPIError(400, "bad block")

        result = await write_agent.optimize_and_book_session("SEC301")

        assert result.status == "CONFIRMED" and result.transit_buffer_added is None
        assert "a transit buffer could not be added" in result.message

    async def test_buffer_for_the_booked_session_is_preferred(self, write_agent, client, state):
        # A -> B is an earlier, unrelated venue change; B -> NEW is the booked session's walk.
        state.book(
            make_session("A", "Venetian", start=at(10)),
            make_session("B", "Wynn", start=at(11, 20)),
        )
        state.known(make_session("NEW", "Mandalay Bay", start=at(20)))

        result = await write_agent.optimize_and_book_session("NEW")

        assert client.create_personal_time.await_count == 2
        assert result.transit_buffer_added == "Transit: Wynn → Mandalay Bay"


class TestOtherTools:
    async def test_manage_favorites(self, agent, client, state):
        state.favorites["SEC301"] = make_session("SEC301")  # the API answers alreadyFavorited

        added = await agent.manage_favorites("add", "SEC301")
        removed = await agent.manage_favorites("remove", "SEC301")

        assert added.success and removed.success
        client.disassociate_favorite.assert_awaited_once_with("SEC301")
        assert "SEC301" not in state.favorites

    async def test_manage_favorites_reports_refusal(self, agent, client):
        client.associate_favorites.side_effect = [
            BulkResult(failed=[BulkFailure(session_id="X", code="insufficientAccess")])
        ]

        result = await agent.manage_favorites("add", "X")

        assert result.success is False and "insufficientAccess" in result.message

    async def test_check_transit_prefers_local_catalog(self, agent, client, ingestion):
        sessions = {
            "S1": make_session("S1", "Venetian", start=at(18)),
            "S2": make_session("S2", "Wynn", start=at(19, 20)),
        }
        ingestion.get_session_local.side_effect = lambda sid: sessions[sid]

        result = await agent.check_transit("S1", "S2")

        # 19 min required: 10 matrix + 5 (Venetian) + 4 (Wynn) inside_walk.
        assert (result.from_venue, result.to_venue, result.transit_minutes) == (
            "Venetian",
            "Wynn",
            19,
        )
        assert result.is_feasible and result.gap_minutes == 20
        client.get_session.assert_not_awaited()

    async def test_add_to_waitlist_stores_title_and_venue(self, agent, state, waitlist):
        state.known(make_session("SEC301", "Wynn", title="Zero trust"))

        result = await agent.add_to_waitlist("SEC301", priority=10)

        [entry] = waitlist.entries()
        assert (result.priority, entry.priority, entry.title, entry.venue_id) == (
            10,
            10,
            "Zero trust",
            "Wynn",
        )

    async def test_every_tool_call_is_audited(self, agent, state, config):
        state.known(make_session("S1"))
        await agent.search_catalog("x")
        await agent.get_my_schedule()
        await agent.manage_favorites("add", "S1")
        await agent.add_to_waitlist("S1")
        await agent.optimize_and_book_session("S1")

        actions = [e["action"] for e in read_audit(config.planner_db_path)]
        assert len(actions) == 5
        assert TOKEN not in audit_dump_text(config.planner_db_path)


class TestPreferenceTools:
    async def test_returns_reviewable_profile_proposals_and_conflict_responses(
        self, config, client, ingestion, credentials, waitlist
    ):
        planner_state = PlannerStateStore(config.planner_db_path)
        await planner_state.initialize()
        agent = MCPAgent(
            config,
            client,
            ingestion,
            TransitEngine(),
            ConflictManager(TransitEngine()),
            AuditLogger(config.planner_db_path, credentials.hash_token),
            waitlist,
            PreferenceProfileStore(planner_state),
        )
        profile = AttendeePreferenceProfile(
            event_id=config.aws_events_event_id,
            name="Learning plan",
            interest_queries=[WeightedInterestQuery(query="serverless", weight=100)],
        )

        saved = await agent.save_preference_profile(profile)
        proposed = await agent.propose_preference_update(
            "advanced GenAI and serverless, keep weekday afternoon free"
        )
        stale = await agent.save_preference_profile(
            profile.model_copy(update={"name": "Stale edit"}), profile.updated_at
        )

        assert saved.status == "SAVED"
        assert proposed.proposed_patch["preferred_levels"] == [400]
        assert proposed.ambiguities
        assert stale.status == "CONFLICT"
        audit_text = audit_dump_text(config.planner_db_path)
        assert "weekday afternoon" not in audit_text
        assert "serverless" not in audit_text


def test_write_tool_outputs_keep_the_approval_token_out_of_repr():
    """M2: approval tokens are secrets; repr must not show them."""
    from reinvent_concierge.agent.tools import CancelReservationOutput, OptimizeAndBookOutput

    secret = "tok-secret-value"
    book = OptimizeAndBookOutput(
        status="CONFLICT_DETECTED", session_title="T", message="m", approval_token=secret
    )
    cancel = CancelReservationOutput(
        session_id="A", status="APPROVAL_REQUIRED", message="m", approval_token=secret
    )

    assert secret not in repr(book) and secret not in repr(cancel)
    assert book.approval_token == secret and cancel.approval_token == secret
    assert book.model_dump()["approval_token"] == secret
