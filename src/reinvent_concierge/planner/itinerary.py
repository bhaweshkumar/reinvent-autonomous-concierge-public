"""Deterministic, local-first itinerary planning for SPEC-11.

Split per SPEC-18 §18.1 into ``plan_models`` (data models), ``candidates`` (bounded
candidate-set construction), ``scoring`` (pure scoring and explanation functions),
``solver`` (the beam-search ``ItineraryPlanner``) and ``plan_store`` (persistence). This
module re-exports everything that used to live here directly, for backward compatibility.
"""

from .candidates import (
    CandidateBuilder,
    CandidateRepository,
    IngestionCandidateRepository,
    IngestionCatalog,
)
from .plan_models import (
    Candidate,
    CandidateExclusion,
    CandidateSet,
    CandidateSource,
    ExcludedReasonSummary,
    ItineraryPlan,
    PlanItem,
    PlanItemKind,
    PlanSet,
    PlanStrategy,
    ReasonCode,
    ScoreComponents,
    SelectionExplanation,
    SolverMetadata,
)
from .plan_store import ItineraryPlanStore
from .solver import ItineraryPlanner

__all__ = [
    "Candidate",
    "CandidateBuilder",
    "CandidateExclusion",
    "CandidateRepository",
    "CandidateSet",
    "CandidateSource",
    "ExcludedReasonSummary",
    "IngestionCandidateRepository",
    "IngestionCatalog",
    "ItineraryPlan",
    "ItineraryPlanner",
    "ItineraryPlanStore",
    "PlanItem",
    "PlanItemKind",
    "PlanSet",
    "PlanStrategy",
    "ReasonCode",
    "ScoreComponents",
    "SelectionExplanation",
    "SolverMetadata",
]
