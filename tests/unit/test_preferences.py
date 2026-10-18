"""SPEC-10 preference profile model, storage, and proposal behavior."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from reinvent_concierge.planner import (
    AttendeePreferenceProfile,
    PreferenceProfileConflictError,
    PreferenceProfileStore,
    SeatPolicy,
    WeightedInterestQuery,
    propose_update,
)
from reinvent_concierge.planner.state_store import PlannerStateStore


def profile(**updates):
    values = {
        "event_id": "reinvent2026",
        "name": "Learning plan",
        "interest_queries": [WeightedInterestQuery(query="serverless", weight=75)],
    }
    values.update(updates)
    return AttendeePreferenceProfile(**values)


def test_profile_validates_boundaries_and_mutually_exclusive_session_sets():
    saved = profile(
        preferred_levels={300, 400},
        max_sessions_per_day=12,
        max_venue_changes_per_day=10,
        transit_multiplier=Decimal("3.0"),
        minimum_gap_minutes=60,
        seat_policy=SeatPolicy.ANY,
    )

    assert saved.preferred_levels == frozenset({300, 400})
    assert saved.seat_policy is SeatPolicy.ANY
    with pytest.raises(ValidationError, match="both must-attend and avoided"):
        profile(must_attend_session_ids={"KEY123"}, avoid_session_ids={"KEY123"})
    with pytest.raises(ValidationError, match="strategy weights must total 100"):
        profile(strategy_weights={"interest_relevance": 100})


async def test_store_enforces_optimistic_concurrency_and_versioned_export_import(config):
    state_store = PlannerStateStore(config.planner_db_path)
    await state_store.initialize()
    store = PreferenceProfileStore(state_store)
    first = await store.save(profile(must_attend_session_ids={"KNOWN", "MISSING"}), None)
    latest = await store.save(first.model_copy(update={"name": "Updated plan"}), first.updated_at)

    with pytest.raises(PreferenceProfileConflictError) as conflict:
        await store.save(first, first.updated_at)

    assert conflict.value.latest == latest
    exported = await store.export_json(latest.event_id)
    assert exported and exported["version"] == 1
    imported = await store.import_json(exported, known_session_ids={"KNOWN"})
    assert imported.profile.unresolved_session_ids == frozenset({"MISSING"})
    assert imported.warnings == ("Session MISSING is unresolved until the local catalog is synced",)


def test_proposal_is_deterministic_and_keeps_missing_keynote_identifiers_ambiguous():
    proposal = propose_update(
        "reinvent2026",
        (
            "Build me an advanced GenAI and serverless agenda. Keep Monday afternoon free, "
            "avoid more than 2 venue changes per day, and preserve my keynote KEY123."
        ),
        None,
    )

    assert proposal.proposed_patch["preferred_levels"] == [400]
    assert proposal.proposed_patch["max_venue_changes_per_day"] == 2
    assert proposal.proposed_patch["must_attend_session_ids"] == ["KEY123"]
    assert {item["query"] for item in proposal.proposed_patch["interest_queries"]} == {
        "generative AI",
        "serverless",
    }
    assert proposal.ambiguities == ()

    ambiguous = propose_update(
        "reinvent2026",
        "Keep weekday afternoons free and preserve keynote.",
        None,
    )

    assert len(ambiguous.ambiguities) == 2
    assert len(ambiguous.confirmation_questions) == 2
