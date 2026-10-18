"""Bounded beam-search itinerary solver (SPEC-18 §18.1 split of itinerary.py).

A deterministic, local-first planner: candidate construction and solving never call
AWS. Generation is read-only and reuses the pure scoring functions in ``scoring.py``.
"""

import asyncio
import math
import time
from collections import defaultdict
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from ..booking.transit_blocks import transit_title
from ..models import Session, utc_now
from ..transit import TransitEngine
from .models import ScheduleSnapshot
from .plan_models import (
    Candidate,
    CandidateSet,
    ExcludedReasonSummary,
    ItineraryPlan,
    PlanItem,
    PlanItemKind,
    PlanSet,
    PlanStrategy,
    ReasonCode,
    ScoreComponents,
    SolverMetadata,
    plan_item_sort_key,
)
from .preferences import AttendeePreferenceProfile, RequiredBreak
from .scoring import (
    candidate_value,
    excluded_reasons,
    is_diverse,
    partial_score,
    plan_fingerprint,
    plan_set_seed,
    relaxations,
    score_components,
    selected_explanations,
    weighted_score,
)


class ItineraryPlanner:
    """A bounded, deterministic beam planner with explicit hard-constraint rejection."""

    def __init__(
        self,
        transit: TransitEngine,
        solve_timeout_seconds: int = 8,
        beam_width: int = 64,
        timezone_name: str = "UTC",
    ) -> None:
        self._transit = transit
        self._timeout_seconds = solve_timeout_seconds
        self._beam_width = beam_width
        self._timezone = ZoneInfo(timezone_name)

    async def plan(
        self,
        candidate_set: CandidateSet,
        profile: AttendeePreferenceProfile,
        snapshot: ScheduleSnapshot,
        strategies: Optional[Sequence[PlanStrategy]] = None,
    ) -> PlanSet:
        """Run the (CPU-bound, up to ``solve_timeout_seconds``) beam search off the event loop."""
        return await asyncio.to_thread(
            self._plan_sync, candidate_set, profile, snapshot, strategies
        )

    def _plan_sync(
        self,
        candidate_set: CandidateSet,
        profile: AttendeePreferenceProfile,
        snapshot: ScheduleSnapshot,
        strategies: Optional[Sequence[PlanStrategy]] = None,
    ) -> PlanSet:
        started = time.monotonic()
        chosen_strategies = tuple(strategies or tuple(PlanStrategy))
        plan_set_id = uuid5(
            NAMESPACE_URL,
            plan_set_seed(profile, snapshot, candidate_set, chosen_strategies),
        )
        by_id = {candidate.session_id: candidate for candidate in candidate_set.candidates}
        conflicts: List[ExcludedReasonSummary] = []
        must_candidates: List[Candidate] = []
        for session_id in sorted(profile.must_attend_session_ids):
            candidate = by_id.get(session_id)
            if candidate is None:
                conflicts.append(
                    ExcludedReasonSummary(
                        session_id=session_id,
                        reason_code=ReasonCode.MISSING_MUST_ATTEND,
                        summary=(
                            "Must-attend session is absent from the feasible local candidate set."
                        ),
                    )
                )
            else:
                must_candidates.append(candidate)
        if conflicts or not self._selection_feasible(tuple(must_candidates), profile, snapshot):
            if not conflicts:
                conflicts.extend(self._minimal_conflicts(must_candidates, profile, snapshot))
            return PlanSet(
                plan_set_id=plan_set_id,
                event_id=profile.event_id,
                profile_id=profile.profile_id,
                source_snapshot_fingerprint=snapshot.fingerprint,
                generated_at=utc_now(),
                plans=(),
                conflict_reasons=tuple(conflicts),
                relaxation_suggestions=relaxations(conflicts),
                warnings=candidate_set.warnings,
            )

        alternatives: List[ItineraryPlan] = []
        warnings = list(candidate_set.warnings)
        for strategy in chosen_strategies:
            candidate_plans, timed_out = self._solve_strategy(
                candidate_set.candidates,
                must_candidates,
                profile,
                snapshot,
                strategy,
                started,
            )
            selected = next(
                (item for item in candidate_plans if is_diverse(item[0], alternatives, profile)),
                None,
            )
            if selected is None:
                warnings.append(
                    f"No {strategy.value} alternative met the 20% non-commitment "
                    "diversity requirement."
                )
                continue
            selected_candidates, total_score, components, items = selected
            duration = int((time.monotonic() - started) * 1000)
            fingerprint = plan_fingerprint(
                profile,
                snapshot,
                strategy,
                items,
                total_score,
            )
            plan_id = uuid5(NAMESPACE_URL, fingerprint)
            alternatives.append(
                ItineraryPlan(
                    plan_id=plan_id,
                    plan_set_id=plan_set_id,
                    event_id=profile.event_id,
                    profile_id=profile.profile_id,
                    source_profile_version=profile.updated_at.isoformat(),
                    source_profile_updated_at=profile.updated_at,
                    source_snapshot_fingerprint=snapshot.fingerprint,
                    strategy=strategy,
                    generated_at=utc_now(),
                    day_items=items,
                    total_score=total_score,
                    score_components=components,
                    selected_explanations=selected_explanations(selected_candidates, profile),
                    top_excluded_reasons=excluded_reasons(candidate_set, selected_candidates),
                    warnings=(),
                    assumptions=(
                        "Unknown seat availability is scored neutrally at 50/100.",
                        "Unrecognized venue pairs use an estimated transit time instead of "
                        "being treated as infeasible; the estimate may be 0 minutes.",
                    ),
                    solver=SolverMetadata(
                        duration_milliseconds=duration,
                        candidate_count=len(candidate_set.candidates),
                        optimality_status=(
                            "BEST_FEASIBLE_TIMEOUT" if timed_out else "BEST_FEASIBLE_BOUNDED"
                        ),
                        beam_width=self._beam_width,
                    ),
                    plan_fingerprint=fingerprint,
                )
            )
        if len(alternatives) < min(3, len(chosen_strategies)):
            warnings.append(
                "Fewer alternatives were returned because the bounded catalog could "
                "not produce additional distinct feasible plans."
            )
        return PlanSet(
            plan_set_id=plan_set_id,
            event_id=profile.event_id,
            profile_id=profile.profile_id,
            source_snapshot_fingerprint=snapshot.fingerprint,
            generated_at=utc_now(),
            plans=tuple(alternatives),
            warnings=tuple(dict.fromkeys(warnings)),
        )

    def _solve_strategy(
        self,
        candidates: Sequence[Candidate],
        must: Sequence[Candidate],
        profile: AttendeePreferenceProfile,
        snapshot: ScheduleSnapshot,
        strategy: PlanStrategy,
        started: float,
    ) -> Tuple[
        List[
            Tuple[
                Tuple[Candidate, ...],
                int,
                ScoreComponents,
                Tuple[Tuple[PlanItem, ...], ...],
            ]
        ],
        bool,
    ]:
        required_ids = {candidate.session_id for candidate in must}
        optional = [
            candidate for candidate in candidates if candidate.session_id not in required_ids
        ]
        optional.sort(
            key=lambda item: (
                -self._candidate_value(item, profile, strategy),
                item.session_id,
            )
        )
        states: List[Tuple[Candidate, ...]] = [
            tuple(sorted(must, key=lambda item: (item.session.start_time, item.session_id)))
        ]
        timed_out = False
        for candidate in optional:
            if time.monotonic() - started >= self._timeout_seconds:
                timed_out = True
                break
            next_states = list(states)
            for selected in states:
                proposed = tuple(
                    sorted(
                        (*selected, candidate),
                        key=lambda item: (item.session.start_time, item.session_id),
                    )
                )
                if self._selection_feasible(proposed, profile, snapshot):
                    next_states.append(proposed)
            unique = {tuple(item.session_id for item in state): state for state in next_states}
            states = sorted(
                unique.values(),
                key=lambda state: (
                    -partial_score(state, profile, strategy),
                    tuple(item.session_id for item in state),
                ),
            )[: self._beam_width]
        results = []
        for selected in states:
            items = self._items_for_selection(selected, profile, snapshot)
            if items is None:
                continue
            components = score_components(selected, items, profile)
            results.append(
                (
                    selected,
                    weighted_score(components, profile, strategy),
                    components,
                    items,
                )
            )
        return (
            sorted(
                results,
                key=lambda item: (
                    -item[1],
                    -len(item[0]),
                    tuple(candidate.session_id for candidate in item[0]),
                ),
            ),
            timed_out,
        )

    def _candidate_value(
        self,
        candidate: Candidate,
        profile: AttendeePreferenceProfile,
        strategy: PlanStrategy,
    ) -> int:
        return candidate_value(candidate, profile, strategy)

    def _selection_feasible(
        self,
        selected: Sequence[Candidate],
        profile: AttendeePreferenceProfile,
        snapshot: ScheduleSnapshot,
    ) -> bool:
        by_day: Dict[date, List[Candidate]] = defaultdict(list)
        for candidate in selected:
            if candidate.session_id in profile.avoid_session_ids:
                return False
            if not self._in_time_window(candidate.session, profile) or self._overlaps_fixed(
                candidate.session, snapshot
            ):
                return False
            by_day[candidate.session.start_time.astimezone(self._timezone).date()].append(candidate)
        occurrence_keys = [
            candidate.occurrence_key for candidate in selected if candidate.occurrence_key
        ]
        if len(occurrence_keys) != len(set(occurrence_keys)):
            return False
        for day_candidates in by_day.values():
            if len(day_candidates) > profile.max_sessions_per_day:
                return False
            ordered = sorted(
                day_candidates,
                key=lambda item: (item.session.start_time, item.session_id),
            )
            venue_changes = sum(
                previous.session.venue_id != current.session.venue_id
                for previous, current in zip(ordered, ordered[1:], strict=False)
            )
            if venue_changes > profile.max_venue_changes_per_day:
                return False
            for previous, current in zip(ordered, ordered[1:], strict=False):
                if current.session.start_time < previous.session.end_time:
                    return False
                if not self._transit_feasible(previous.session, current.session, profile):
                    return False
        return True

    def _in_time_window(self, session: Session, profile: AttendeePreferenceProfile) -> bool:
        local_start = session.start_time.astimezone(self._timezone)
        local_end = session.end_time.astimezone(self._timezone)
        window = next(
            (
                item
                for item in profile.daily_time_windows
                if item.day == local_start.strftime("%A").casefold()
            ),
            None,
        )
        if window is None:
            return True
        return local_start.time() >= window.earliest and local_end.time() <= window.latest

    @staticmethod
    def _overlaps_fixed(session: Session, snapshot: ScheduleSnapshot) -> bool:
        return any(
            session.start_time < item.end_time and item.start_time < session.end_time
            for item in snapshot.personal_time
        )

    def _transit_feasible(
        self,
        earlier: Session,
        later: Session,
        profile: AttendeePreferenceProfile,
    ) -> bool:
        # Never raises: an unrecognized venue estimates 0 rather than forcing infeasibility
        # (SPEC-19 §19.1).
        transit_needed, _quality = self._transit.required_minutes(
            earlier.venue_id,
            later.venue_id,
            from_floor=earlier.floor,
            to_floor=later.floor,
            from_zone=earlier.zone,
            to_zone=later.zone,
            transit_multiplier=profile.transit_multiplier,
        )
        needed = transit_needed + profile.minimum_gap_minutes
        available = math.floor((later.start_time - earlier.end_time).total_seconds() / 60)
        return available >= needed

    def _items_for_selection(
        self,
        selected: Sequence[Candidate],
        profile: AttendeePreferenceProfile,
        snapshot: ScheduleSnapshot,
    ) -> Optional[Tuple[Tuple[PlanItem, ...], ...]]:
        items_by_day: Dict[date, List[PlanItem]] = defaultdict(list)
        for candidate in selected:
            session = candidate.session
            items_by_day[session.start_time.astimezone(self._timezone).date()].append(
                PlanItem(
                    kind=PlanItemKind.SESSION,
                    session_id=session.session_id,
                    title=session.title,
                    venue_id=session.venue_id,
                    start_time=session.start_time,
                    end_time=session.end_time,
                )
            )
        for fixed in snapshot.personal_time:
            items_by_day[fixed.start_time.astimezone(self._timezone).date()].append(
                PlanItem(
                    kind=PlanItemKind.FIXED_TIME,
                    title=fixed.title,
                    venue_id=fixed.location,
                    start_time=fixed.start_time,
                    end_time=fixed.end_time,
                    detail=fixed.description,
                )
            )
        for day, items in list(items_by_day.items()):
            breaks = self._place_hard_breaks(day, items, profile.required_breaks)
            if breaks is None:
                return None
            items.extend(breaks)
            sessions = sorted(
                (item for item in items if item.kind is PlanItemKind.SESSION),
                key=lambda item: (item.start_time, item.session_id or ""),
            )
            for earlier, later in zip(sessions, sessions[1:], strict=False):
                # Never raises and never hard-rejects an unrecognized venue pair (SPEC-19 §19.1,
                # acceptance criterion 4): it estimates, worst case 0 added minutes.
                raw, _quality = self._transit.required_minutes(
                    earlier.venue_id or "",
                    later.venue_id or "",
                    transit_multiplier=profile.transit_multiplier,
                )
                adjusted = raw + profile.minimum_gap_minutes
                if adjusted:
                    transit_end = earlier.end_time + timedelta(minutes=adjusted)
                    if transit_end > later.start_time or any(
                        transit_end > item.start_time and earlier.end_time < item.end_time
                        for item in items
                        if item.kind is not PlanItemKind.SESSION
                    ):
                        return None
                    items.append(
                        PlanItem(
                            kind=PlanItemKind.TRANSIT,
                            title=transit_title(
                                earlier.venue_id or "Unknown", later.venue_id or "Unknown"
                            ),
                            start_time=earlier.end_time,
                            end_time=transit_end,
                            venue_id=later.venue_id,
                            detail=f"{adjusted} adjusted minutes",
                        )
                    )
        return tuple(
            tuple(sorted(items, key=plan_item_sort_key))
            for _, items in sorted(items_by_day.items())
        )

    def _place_hard_breaks(
        self,
        day: date,
        items: Sequence[PlanItem],
        breaks: Sequence[RequiredBreak],
    ) -> Optional[List[PlanItem]]:
        placed: List[PlanItem] = []
        for policy in sorted(
            (item for item in breaks if item.is_hard),
            key=lambda item: (item.start_time, item.label),
        ):
            day_start = datetime.combine(day, policy.start_time, tzinfo=self._timezone).astimezone(
                timezone.utc
            )
            day_end = datetime.combine(day, policy.end_time, tzinfo=self._timezone).astimezone(
                timezone.utc
            )
            if day_end <= day_start:
                day_end += timedelta(days=1)
            cursor = day_start
            duration = timedelta(minutes=policy.duration_minutes)
            for occupied in sorted((*items, *placed), key=lambda item: item.start_time):
                if cursor + duration <= occupied.start_time:
                    break
                if cursor < occupied.end_time:
                    cursor = occupied.end_time
            if cursor + duration > day_end:
                return None
            placed.append(
                PlanItem(
                    kind=PlanItemKind.BREAK,
                    title=policy.label,
                    start_time=cursor,
                    end_time=cursor + duration,
                )
            )
        return placed

    def _minimal_conflicts(
        self,
        must: Sequence[Candidate],
        profile: AttendeePreferenceProfile,
        snapshot: ScheduleSnapshot,
    ) -> List[ExcludedReasonSummary]:
        reasons: List[ExcludedReasonSummary] = []
        for candidate in must:
            session = candidate.session
            if not self._in_time_window(session, profile):
                reasons.append(
                    ExcludedReasonSummary(
                        session_id=session.session_id,
                        reason_code=ReasonCode.OUTSIDE_TIME_WINDOW,
                        summary="Must-attend session is outside its daily time window.",
                    )
                )
            elif self._overlaps_fixed(session, snapshot):
                reasons.append(
                    ExcludedReasonSummary(
                        session_id=session.session_id,
                        reason_code=ReasonCode.FIXED_TIME_CONFLICT,
                        summary="Must-attend session overlaps fixed personal time.",
                    )
                )
        ordered = sorted(must, key=lambda item: item.session.start_time)
        for first, second in zip(ordered, ordered[1:], strict=False):
            if second.session.start_time < first.session.end_time:
                reasons.append(
                    ExcludedReasonSummary(
                        session_id=second.session_id,
                        reason_code=ReasonCode.TIME_CONFLICT,
                        summary=f"Must-attend session overlaps {first.session_id}.",
                    )
                )
            elif not self._transit_feasible(first.session, second.session, profile):
                reasons.append(
                    ExcludedReasonSummary(
                        session_id=second.session_id,
                        reason_code=ReasonCode.TRANSIT_INFEASIBLE,
                        summary=f"Insufficient adjusted transit time after {first.session_id}.",
                    )
                )
        return reasons or [
            ExcludedReasonSummary(
                session_id="profile",
                reason_code=ReasonCode.HARD_BREAK,
                summary="Hard break or daily session limits make the commitments infeasible.",
            )
        ]
