"""Pure plan scoring and explanation functions (SPEC-18 §18.1 split of itinerary.py).

Every function here is pure: no AWS access, no persistence, no randomness. The solver
calls these to turn a selected candidate set into a score and into attendee-facing
explanations.
"""

import hashlib
import json
from collections.abc import Sequence
from typing import Dict, List, Tuple

from .models import ScheduleSnapshot
from .plan_models import (
    Candidate,
    CandidateSet,
    ExcludedReasonSummary,
    ItineraryPlan,
    PlanItem,
    PlanItemKind,
    PlanStrategy,
    ReasonCode,
    ScoreComponents,
    SelectionExplanation,
)
from .preferences import AttendeePreferenceProfile


def with_minimum(weights: Dict[str, int], key: str, minimum: int) -> Dict[str, int]:
    adjusted = dict(weights)
    delta = max(0, minimum - adjusted[key])
    for donor in ("interest_relevance", "topic_fit", "content_diversity", "pacing_and_breaks"):
        transfer = min(delta, adjusted[donor])
        adjusted[donor] -= transfer
        adjusted[key] += transfer
        delta -= transfer
        if not delta:
            break
    return adjusted


def weighted_score(
    components: ScoreComponents,
    profile: AttendeePreferenceProfile,
    strategy: PlanStrategy,
) -> int:
    weights = profile.strategy_weights.model_dump()
    if strategy is PlanStrategy.LOWEST_TRAVEL:
        weights = with_minimum(weights, "transit_efficiency", 30)
    elif strategy is PlanStrategy.BEST_SEAT_CONFIDENCE:
        weights = with_minimum(weights, "seat_confidence", 30)
    positive = (
        components.relevance * weights["interest_relevance"]
        + components.preferred_fit * weights["topic_fit"]
        + components.seat_confidence * weights["seat_confidence"]
        + components.pacing_and_breaks * weights["pacing_and_breaks"]
        + components.diversity * weights["content_diversity"]
    )
    negative = (
        components.transit_and_venue_penalty * weights["transit_efficiency"]
        + components.change_penalty
    )
    return (positive - negative) // 100


def candidate_value(
    candidate: Candidate,
    profile: AttendeePreferenceProfile,
    strategy: PlanStrategy,
) -> int:
    components = ScoreComponents(
        relevance=candidate.relevance_score,
        preferred_fit=candidate.preferred_fit_score,
        seat_confidence=candidate.seat_confidence_score,
        pacing_and_breaks=100,
        diversity=50,
        transit_and_venue_penalty=0,
        change_penalty=0,
    )
    return weighted_score(components, profile, strategy)


def partial_score(
    selected: Sequence[Candidate],
    profile: AttendeePreferenceProfile,
    strategy: PlanStrategy,
) -> int:
    return sum(candidate_value(candidate, profile, strategy) for candidate in selected)


def score_components(
    selected: Sequence[Candidate],
    items: Tuple[Tuple[PlanItem, ...], ...],
    profile: AttendeePreferenceProfile,
) -> ScoreComponents:
    if not selected:
        return ScoreComponents(
            relevance=0,
            preferred_fit=0,
            seat_confidence=0,
            pacing_and_breaks=100,
            diversity=0,
            transit_and_venue_penalty=0,
            change_penalty=0,
        )
    relevance = sum(item.relevance_score for item in selected) // len(selected)
    preferred = sum(item.preferred_fit_score for item in selected) // len(selected)
    seats = sum(item.seat_confidence_score for item in selected) // len(selected)
    tracks = {item.session.track for item in selected if item.session.track}
    formats = {item.session.title.split(" ", 1)[0] for item in selected if item.session.title}
    distinct_content_types = len(tracks) + len(formats)
    diversity = min(
        100,
        (distinct_content_types * 100) // max(1, 2 * min(len(selected), distinct_content_types)),
    )
    transit_minutes = sum(
        int((item.end_time - item.start_time).total_seconds() // 60)
        for day in items
        for item in day
        if item.kind is PlanItemKind.TRANSIT
    )
    penalty = min(100, (transit_minutes * 100) // 240)
    hard_break_count = sum(1 for item in profile.required_breaks if item.is_hard)
    actual_break_count = sum(1 for day in items for item in day if item.kind is PlanItemKind.BREAK)
    pacing = 100 if actual_break_count >= hard_break_count else 0
    return ScoreComponents(
        relevance=relevance,
        preferred_fit=preferred,
        seat_confidence=seats,
        pacing_and_breaks=pacing,
        diversity=diversity,
        transit_and_venue_penalty=penalty,
        change_penalty=0,
    )


def is_diverse(
    selected: Sequence[Candidate],
    previous: Sequence[ItineraryPlan],
    profile: AttendeePreferenceProfile,
) -> bool:
    if not previous:
        return True
    commitments = profile.must_attend_session_ids
    selected_ids = {item.session_id for item in selected if item.session_id not in commitments}
    for plan in previous:
        previous_ids = set(plan.selected_session_ids) - commitments
        baseline = max(len(selected_ids), len(previous_ids), 1)
        if len(selected_ids.symmetric_difference(previous_ids)) / baseline < 0.20:
            return False
    return True


_RELAXATION_BY_REASON = {
    ReasonCode.TIME_CONFLICT: "Choose one overlapping must-attend session.",
    ReasonCode.TRANSIT_INFEASIBLE: (
        "Reduce the transit multiplier or replace one cross-venue commitment."
    ),
    ReasonCode.OUTSIDE_TIME_WINDOW: "Widen the affected daily time window.",
    ReasonCode.HARD_BREAK: "Mark the affected break as soft or widen its window.",
    ReasonCode.FIXED_TIME_CONFLICT: (
        "Move the fixed personal-time block or remove the conflicting commitment."
    ),
    ReasonCode.MISSING_MUST_ATTEND: (
        "Sync the catalog or replace the unresolved must-attend session."
    ),
}


def relaxations(conflicts: Sequence[ExcludedReasonSummary]) -> Tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            _RELAXATION_BY_REASON.get(item.reason_code, "Review the conflicting hard constraints.")
            for item in conflicts
        )
    )


def selected_explanations(
    selected: Sequence[Candidate], profile: AttendeePreferenceProfile
) -> Tuple[SelectionExplanation, ...]:
    explanations = []
    for candidate in sorted(selected, key=lambda item: item.session_id):
        reasons: List[ReasonCode] = []
        if candidate.session_id in profile.must_attend_session_ids:
            reasons.append(ReasonCode.MUST_ATTEND)
        if candidate.relevance_score >= 70:
            reasons.append(ReasonCode.HIGH_RELEVANCE)
        if candidate.preferred_fit_score:
            reasons.append(ReasonCode.PREFERRED_TRACK)
        if candidate.seat_confidence_score >= 70:
            reasons.append(ReasonCode.SEAT_CONFIDENCE)
        if not reasons:
            reasons.append(ReasonCode.LOWER_OBJECTIVE_SCORE)
        summary = "; ".join(reason.value.replace("_", " ").title() for reason in reasons[:3])
        explanations.append(
            SelectionExplanation(
                session_id=candidate.session_id,
                reasons=tuple(reasons[:3]),
                summary=summary,
            )
        )
    return tuple(explanations)


def excluded_reasons(
    candidate_set: CandidateSet, selected: Sequence[Candidate]
) -> Tuple[ExcludedReasonSummary, ...]:
    selected_ids = {candidate.session_id for candidate in selected}
    excluded = [
        ExcludedReasonSummary(
            session_id=item.session_id,
            reason_code=item.reason_code,
            summary=item.detail,
        )
        for item in candidate_set.exclusions
    ]
    excluded.extend(
        ExcludedReasonSummary(
            session_id=item.session_id,
            reason_code=ReasonCode.LOWER_OBJECTIVE_SCORE,
            summary="A feasible alternative had a lower objective score.",
        )
        for item in candidate_set.candidates
        if item.session_id not in selected_ids
    )
    return tuple(
        sorted(
            excluded,
            key=lambda item: (item.reason_code.value, item.session_id),
        )[:10]
    )


def plan_set_seed(
    profile: AttendeePreferenceProfile,
    snapshot: ScheduleSnapshot,
    candidate_set: CandidateSet,
    strategies: Sequence[PlanStrategy],
) -> str:
    return json.dumps(
        {
            "profile": str(profile.profile_id),
            "profile_version": profile.updated_at.isoformat(),
            "snapshot": snapshot.fingerprint,
            "candidates": [
                {
                    "session_id": item.session_id,
                    "sources": sorted(source.value for source in item.sources),
                }
                for item in sorted(
                    candidate_set.candidates,
                    key=lambda candidate: candidate.session_id,
                )
            ],
            "exclusions": [
                {
                    "session_id": item.session_id,
                    "reason_code": item.reason_code.value,
                    "detail": item.detail,
                }
                for item in candidate_set.exclusions
            ],
            "strategies": [item.value for item in strategies],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def plan_fingerprint(
    profile: AttendeePreferenceProfile,
    snapshot: ScheduleSnapshot,
    strategy: PlanStrategy,
    items: Tuple[Tuple[PlanItem, ...], ...],
    total_score: int,
) -> str:
    payload = {
        "event_id": profile.event_id,
        "snapshot": snapshot.fingerprint,
        "strategy": strategy.value,
        "score": total_score,
        "items": [[item.model_dump(mode="json") for item in day] for day in items],
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()
