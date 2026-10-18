"""Itinerary data models: candidates, plan items and the generated plan set.

Split out of ``itinerary.py`` (SPEC-18 §18.1); ``itinerary.py`` re-exports these for
backward compatibility.
"""

from datetime import datetime
from enum import Enum
from typing import Optional, Tuple
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..models import Session


class _ImmutableModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CandidateSource(str, Enum):
    INTEREST_SEARCH = "INTEREST_SEARCH"
    MUST_ATTEND = "MUST_ATTEND"
    CURRENT_RESERVATION = "CURRENT_RESERVATION"
    FAVORITE = "FAVORITE"
    PREFERRED_METADATA = "PREFERRED_METADATA"


class PlanStrategy(str, Enum):
    BEST_LEARNING_FIT = "BEST_LEARNING_FIT"
    LOWEST_TRAVEL = "LOWEST_TRAVEL"
    BEST_SEAT_CONFIDENCE = "BEST_SEAT_CONFIDENCE"


class PlanItemKind(str, Enum):
    SESSION = "SESSION"
    BREAK = "BREAK"
    TRANSIT = "TRANSIT"
    FIXED_TIME = "FIXED_TIME"


class ReasonCode(str, Enum):
    MUST_ATTEND = "MUST_ATTEND"
    HIGH_RELEVANCE = "HIGH_RELEVANCE"
    PREFERRED_TRACK = "PREFERRED_TRACK"
    PREFERRED_SERVICE = "PREFERRED_SERVICE"
    SEAT_CONFIDENCE = "SEAT_CONFIDENCE"
    LOW_TRAVEL = "LOW_TRAVEL"
    TIME_CONFLICT = "TIME_CONFLICT"
    TRANSIT_INFEASIBLE = "TRANSIT_INFEASIBLE"
    OUTSIDE_TIME_WINDOW = "OUTSIDE_TIME_WINDOW"
    HARD_BREAK = "HARD_BREAK"
    AVOIDED = "AVOIDED"
    EQUIVALENT_OCCURRENCE_SELECTED = "EQUIVALENT_OCCURRENCE_SELECTED"
    LOWER_OBJECTIVE_SCORE = "LOWER_OBJECTIVE_SCORE"
    SEAT_POLICY = "SEAT_POLICY"
    CANDIDATE_LIMIT = "CANDIDATE_LIMIT"
    CANCELLED = "CANCELLED"
    MISSING_MUST_ATTEND = "MISSING_MUST_ATTEND"
    MAX_SESSIONS_PER_DAY = "MAX_SESSIONS_PER_DAY"
    FIXED_TIME_CONFLICT = "FIXED_TIME_CONFLICT"


class Candidate(_ImmutableModel):
    session: Session
    sources: Tuple[CandidateSource, ...]
    relevance_score: int = Field(ge=0, le=100)
    preferred_fit_score: int = Field(ge=0, le=100)
    seat_confidence_score: int = Field(ge=0, le=100)
    occurrence_key: Optional[str] = None

    @field_validator("sources")
    @classmethod
    def canonicalize_sources(
        cls, value: Tuple[CandidateSource, ...]
    ) -> Tuple[CandidateSource, ...]:
        return tuple(sorted(set(value), key=lambda source: source.value))

    @property
    def session_id(self) -> str:
        return self.session.session_id


class CandidateExclusion(_ImmutableModel):
    session_id: str
    reason_code: ReasonCode
    detail: str


class CandidateSet(_ImmutableModel):
    candidates: Tuple[Candidate, ...]
    exclusions: Tuple[CandidateExclusion, ...] = ()
    warnings: Tuple[str, ...] = ()

    @field_validator("candidates")
    @classmethod
    def canonicalize_candidates(cls, value: Tuple[Candidate, ...]) -> Tuple[Candidate, ...]:
        return tuple(
            sorted(
                value,
                key=lambda candidate: (candidate.session.start_time, candidate.session_id),
            )
        )

    @field_validator("exclusions")
    @classmethod
    def canonicalize_exclusions(
        cls, value: Tuple[CandidateExclusion, ...]
    ) -> Tuple[CandidateExclusion, ...]:
        return tuple(
            sorted(
                value,
                key=lambda exclusion: (
                    exclusion.reason_code.value,
                    exclusion.session_id,
                    exclusion.detail,
                ),
            )
        )

    @field_validator("warnings")
    @classmethod
    def canonicalize_warnings(cls, value: Tuple[str, ...]) -> Tuple[str, ...]:
        return tuple(sorted(set(value)))


class PlanItem(_ImmutableModel):
    kind: PlanItemKind
    start_time: datetime
    end_time: datetime
    title: str
    session_id: Optional[str] = None
    venue_id: Optional[str] = None
    detail: Optional[str] = None


def plan_item_sort_key(item: PlanItem) -> Tuple[datetime, datetime, str, str, str, str, str]:
    return (
        item.start_time,
        item.end_time,
        item.kind.value,
        item.session_id or "",
        item.title,
        item.venue_id or "",
        item.detail or "",
    )


class ScoreComponents(_ImmutableModel):
    relevance: int = Field(ge=0, le=100)
    preferred_fit: int = Field(ge=0, le=100)
    seat_confidence: int = Field(ge=0, le=100)
    pacing_and_breaks: int = Field(ge=0, le=100)
    diversity: int = Field(ge=0, le=100)
    transit_and_venue_penalty: int = Field(ge=0, le=100)
    change_penalty: int = Field(ge=0, le=100)


class SelectionExplanation(_ImmutableModel):
    session_id: str
    reasons: Tuple[ReasonCode, ...]
    summary: str


class ExcludedReasonSummary(_ImmutableModel):
    session_id: str
    reason_code: ReasonCode
    summary: str


class SolverMetadata(_ImmutableModel):
    solver_name: str = "deterministic-beam-search"
    solver_version: str = "1"
    duration_milliseconds: int = Field(ge=0)
    candidate_count: int = Field(ge=0)
    optimality_status: str
    beam_width: int = Field(ge=1)


class ItineraryPlan(_ImmutableModel):
    plan_id: UUID
    plan_set_id: UUID
    event_id: str
    profile_id: UUID
    source_profile_version: str
    source_profile_updated_at: datetime
    source_snapshot_fingerprint: str
    strategy: PlanStrategy
    generated_at: datetime
    day_items: Tuple[Tuple[PlanItem, ...], ...]
    total_score: int
    score_components: ScoreComponents
    selected_explanations: Tuple[SelectionExplanation, ...]
    top_excluded_reasons: Tuple[ExcludedReasonSummary, ...]
    warnings: Tuple[str, ...]
    assumptions: Tuple[str, ...]
    solver: SolverMetadata
    plan_fingerprint: str

    @property
    def selected_session_ids(self) -> Tuple[str, ...]:
        return tuple(
            item.session_id
            for day in self.day_items
            for item in day
            if item.kind is PlanItemKind.SESSION and item.session_id is not None
        )


class PlanSet(_ImmutableModel):
    plan_set_id: UUID
    event_id: str
    profile_id: UUID
    source_snapshot_fingerprint: str
    generated_at: datetime
    plans: Tuple[ItineraryPlan, ...]
    conflict_reasons: Tuple[ExcludedReasonSummary, ...] = ()
    relaxation_suggestions: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()
