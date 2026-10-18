"""SPEC-11 deterministic itinerary planner coverage."""

from reinvent_concierge.models import SessionSummary
from reinvent_concierge.planner import (
    AttendeePreferenceProfile,
    Candidate,
    CandidateBuilder,
    CandidateSet,
    CandidateSource,
    ItineraryPlanner,
    PlanStrategy,
    ScheduleSnapshot,
    WeightedInterestQuery,
)
from reinvent_concierge.planner.candidates import _session_occurrence_key
from reinvent_concierge.transit import TransitEngine
from tests.factories import at, make_schedule, make_session


def profile(**updates):
    values = {
        "event_id": "reinvent2026",
        "name": "Learning plan",
        "interest_queries": [WeightedInterestQuery(query="agents", weight=100)],
    }
    values.update(updates)
    return AttendeePreferenceProfile(**values)


def candidate(session, relevance=80, preferred=40, seat=70):
    return Candidate(
        session=session,
        sources=(CandidateSource.INTEREST_SEARCH,),
        relevance_score=relevance,
        preferred_fit_score=preferred,
        seat_confidence_score=seat,
    )


def snapshot():
    return ScheduleSnapshot.from_schedule("reinvent2026", make_schedule())


async def test_planner_keeps_must_attend_and_excludes_overlap_and_avoided_sessions():
    must = make_session("A100", start=at(10), minutes=60)
    overlap = make_session("B100", start=at(10, 30), minutes=60)
    follow_up = make_session("C100", start=at(11), minutes=60)
    plan_set = await ItineraryPlanner(TransitEngine()).plan(
        CandidateSet(candidates=(candidate(must), candidate(overlap), candidate(follow_up))),
        profile(must_attend_session_ids={"A100"}, avoid_session_ids={"B100"}),
        snapshot(),
        [PlanStrategy.BEST_LEARNING_FIT],
    )

    assert len(plan_set.plans) == 1
    assert plan_set.plans[0].selected_session_ids == ("A100", "C100")


async def test_planner_is_deterministic_for_equivalent_candidate_ordering():
    sessions = (
        candidate(make_session("A100", start=at(9))),
        candidate(make_session("B100", start=at(10))),
    )
    planner = ItineraryPlanner(TransitEngine())

    first = await planner.plan(CandidateSet(candidates=sessions), profile(), snapshot())
    second = await planner.plan(
        CandidateSet(candidates=tuple(reversed(sessions))), profile(), snapshot()
    )

    assert [plan.plan_fingerprint for plan in first.plans] == [
        plan.plan_fingerprint for plan in second.plans
    ]


async def test_async_plan_matches_the_sync_solver_it_wraps():
    """SPEC-20 §20.3: ``plan()`` only moves ``_plan_sync`` off the event loop; same result."""
    candidates = CandidateSet(
        candidates=(
            candidate(make_session("A100", start=at(9))),
            candidate(make_session("B100", start=at(10))),
        )
    )
    planner = ItineraryPlanner(TransitEngine())
    args = (candidates, profile(), snapshot())

    via_thread = await planner.plan(*args)
    direct = planner._plan_sync(*args)

    assert [p.plan_fingerprint for p in via_thread.plans] == [
        p.plan_fingerprint for p in direct.plans
    ]


async def test_unknown_venue_pair_does_not_force_plan_infeasible():
    """SPEC-19 AC4: an unrecognized venue never hard-rejects the planner's selection."""
    first = make_session("A100", venue="Spaceport Alpha", start=at(9), minutes=60)
    second = make_session("B100", venue="Venetian", start=at(11), minutes=60)

    plan_set = await ItineraryPlanner(TransitEngine()).plan(
        CandidateSet(candidates=(candidate(first), candidate(second))),
        profile(must_attend_session_ids={"A100", "B100"}),
        snapshot(),
        [PlanStrategy.BEST_LEARNING_FIT],
    )

    assert len(plan_set.plans) == 1
    assert plan_set.plans[0].selected_session_ids == ("A100", "B100")


async def test_unsatisfiable_must_attend_sessions_return_conflicts_without_a_plan():
    first = candidate(make_session("A100", start=at(10), minutes=60))
    second = candidate(make_session("B100", start=at(10, 30), minutes=60))

    plan_set = await ItineraryPlanner(TransitEngine()).plan(
        CandidateSet(candidates=(first, second)),
        profile(must_attend_session_ids={"A100", "B100"}),
        snapshot(),
    )

    assert plan_set.plans == ()
    assert {reason.reason_code.value for reason in plan_set.conflict_reasons} == {"TIME_CONFLICT"}
    assert plan_set.relaxation_suggestions


async def test_alternatives_meet_non_commitment_diversity_when_catalog_supports_them():
    sessions = (
        candidate(
            make_session("A100", start=at(9), seat_availability="veryLimited"),
            relevance=95,
            seat=25,
        ),
        candidate(
            make_session("B100", start=at(9), seat_availability="available"),
            relevance=30,
            seat=100,
        ),
        candidate(
            make_session("C100", start=at(10), seat_availability="veryLimited"),
            relevance=95,
            seat=25,
        ),
        candidate(
            make_session("D100", start=at(10), seat_availability="available"),
            relevance=30,
            seat=100,
        ),
        candidate(
            make_session("E100", start=at(11), seat_availability="veryLimited"),
            relevance=95,
            seat=25,
        ),
        candidate(
            make_session("F100", start=at(11), seat_availability="available"),
            relevance=30,
            seat=100,
        ),
    )

    plan_set = await ItineraryPlanner(TransitEngine()).plan(
        CandidateSet(candidates=sessions),
        profile(max_sessions_per_day=3),
        snapshot(),
        [PlanStrategy.BEST_LEARNING_FIT, PlanStrategy.BEST_SEAT_CONFIDENCE],
    )

    assert len(plan_set.plans) == 2
    assert set(plan_set.plans[0].selected_session_ids) != set(
        plan_set.plans[1].selected_session_ids
    )


class FakeRepository:
    def __init__(self, sessions, searchable=True):
        self.sessions = {s.session_id: s for s in sessions}
        self.searchable = searchable

    async def search(self, query, limit):
        if not self.searchable:
            return []
        return [
            SessionSummary(
                session_id=s.session_id,
                title=s.title,
                venue_id=s.venue_id,
                start_time=s.start_time,
                end_time=s.end_time,
            )
            for s in self.sessions.values()
        ][:limit]

    async def get_local(self, session_id):
        return self.sessions.get(session_id)

    async def list_local(self, limit):
        return list(self.sessions.values())[:limit]


async def test_planner_never_selects_two_offerings_of_one_repeat_group():
    # Ids and titles carry no session code, so only repeat_group can link the offerings.
    first = make_session("off-1", start=at(9), title="Agents", repeat_group="AIM301")
    repeat = make_session("off-2", start=at(14), title="Agents", repeat_group="AIM301")
    candidates = await CandidateBuilder(FakeRepository([first, repeat])).build(
        profile(), snapshot()
    )

    plan_set = await ItineraryPlanner(TransitEngine()).plan(candidates, profile(), snapshot())

    assert plan_set.plans
    for plan in plan_set.plans:
        assert len(set(plan.selected_session_ids) & {"off-1", "off-2"}) <= 1


def test_repeat_group_wins_over_session_code_regex_for_occurrence_key():
    session = make_session("off-1", title="Agents", repeat_group="AIM301")

    assert _session_occurrence_key(session) == "AIM301"


def test_preferred_fit_uses_structured_services_and_tracks():
    session = make_session("S1", title="Untitled", services=["Amazon Bedrock"], track="AI/ML")
    prefs = profile(
        preferred_services=frozenset({"amazon bedrock"}), preferred_tracks=frozenset({"AI/ML"})
    )

    assert CandidateBuilder._preferred_fit(session, prefs) >= 65


async def test_builder_scan_admits_sessions_matching_preferred_service_by_structured_field():
    match = make_session("S1", title="Untitled", services=["Amazon Bedrock"])
    other = make_session("S2", title="Untitled", services=["Amazon S3"])
    prefs = profile(preferred_services=frozenset({"Amazon Bedrock"}))
    repo = FakeRepository([match, other], searchable=False)

    result = await CandidateBuilder(repo).build(prefs, snapshot())

    assert [c.session_id for c in result.candidates] == ["S1"]
