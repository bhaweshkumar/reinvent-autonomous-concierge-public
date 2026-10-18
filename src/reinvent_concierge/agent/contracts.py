"""Pydantic I/O contracts for every MCP tool (SPEC-18 §18.1): one declaration each."""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from ..models import SessionSummary, WaitlistEntry
from ..planner import ApplicationMode, AttendeePreferenceProfile, ItineraryPlan, PlanStrategy


class SearchCatalogInput(BaseModel):
    query: str = Field(..., min_length=1, description="Search query")
    level: Optional[int] = Field(
        None, ge=100, le=400, multiple_of=100, description="Filter by level (100-400)"
    )
    venue: Optional[str] = Field(None, description="Filter by venue")
    hero_only: bool = Field(False, description="Only AWS Hero speaker sessions")
    limit: int = Field(5, ge=1, le=20, description="Max results (1-20)")
    session_type: Optional[str] = Field(
        None, max_length=64, description="Format, e.g. Workshop, Breakout session"
    )
    topic: Optional[str] = Field(
        None, max_length=100, description="Topic, e.g. Artificial Intelligence"
    )


class SearchCatalogOutput(BaseModel):
    sessions: List[SessionSummary]
    total_found: int


class GetScheduleInput(BaseModel):
    include_favorites: bool = Field(True, description="Include favorites")
    include_personal_time: bool = Field(True, description="Include personal time blocks")


class OptimizeAndBookInput(BaseModel):
    session_id: str = Field(..., min_length=1, description="Session ID to book")
    auto_resolve_conflicts: bool = Field(
        True, description="Add transit buffers for venue changes; never cancels anything"
    )
    apply_run_id: Optional[str] = Field(None, description="Swap proposal to execute")
    approval_token: Optional[str] = Field(None, description="Approval token of that proposal")


class OptimizeAndBookOutput(BaseModel):
    status: Literal["CONFIRMED", "FAVORITED", "CONFLICT_DETECTED", "FAILED"]
    session_title: str
    transit_buffer_added: Optional[str] = None
    canceled_sessions: List[str] = Field(default_factory=list)
    message: str
    apply_run_id: Optional[str] = None
    approval_token: Optional[str] = Field(None, repr=False)
    expires_at: Optional[datetime] = None
    proposed_changes: List[str] = Field(default_factory=list)


class ManageFavoritesInput(BaseModel):
    action: Literal["add", "remove"] = Field(..., description="Add or remove favorite")
    session_id: str = Field(..., min_length=1, description="Session ID")


class ManageFavoritesOutput(BaseModel):
    action: Literal["add", "remove"]
    session_id: str
    success: bool
    message: str


class CheckTransitInput(BaseModel):
    from_session_id: str = Field(..., min_length=1, description="Earlier session ID")
    to_session_id: str = Field(..., min_length=1, description="Later session ID")


class CheckTransitOutput(BaseModel):
    from_venue: str
    to_venue: str
    transit_minutes: int
    gap_minutes: int
    is_feasible: bool
    deficit_minutes: int
    margin_minutes: int = 0
    estimate_quality: Literal["NORMAL", "LOW"] = "NORMAL"
    tight: bool = False


class AddToWaitlistInput(BaseModel):
    session_id: str = Field(..., min_length=1, description="Session ID")
    priority: int = Field(50, ge=1, le=100, description="Priority (1=highest, 100=lowest)")


class AddToWaitlistOutput(BaseModel):
    session_id: str
    priority: int
    message: str


class ListWaitlistInput(BaseModel):
    pass


class ListWaitlistOutput(BaseModel):
    entries: List[WaitlistEntry]


class UpdateWaitlistEntryInput(BaseModel):
    session_id: str = Field(..., min_length=1, description="Session ID")
    priority: Optional[int] = Field(None, ge=1, le=100, description="New priority")
    enabled: Optional[bool] = Field(None, description="Whether the snapper may auto-reserve it")


class UpdateWaitlistEntryOutput(BaseModel):
    session_id: str
    entry: Optional[WaitlistEntry] = None
    message: str


class RemoveFromWaitlistInput(BaseModel):
    session_id: str = Field(..., min_length=1, description="Session ID")


class RemoveFromWaitlistOutput(BaseModel):
    session_id: str
    removed: bool
    message: str


class GetRecentOutcomesInput(BaseModel):
    limit: int = Field(10, ge=1, le=50, description="Max outcomes to return")


class RecentOutcome(BaseModel):
    session_id: str
    status: str
    timestamp: str
    details: str


class GetRecentOutcomesOutput(BaseModel):
    outcomes: List[RecentOutcome]


class CancelReservationInput(BaseModel):
    session_id: str = Field(..., min_length=1, description="Session ID to cancel")
    apply_run_id: Optional[str] = Field(None, description="Cancel proposal to execute")
    approval_token: Optional[str] = Field(None, description="Approval token of that proposal")


class CancelReservationOutput(BaseModel):
    session_id: str
    status: Literal["APPROVAL_REQUIRED", "CANCELED", "FAILED"] = "CANCELED"
    message: str
    apply_run_id: Optional[str] = None
    approval_token: Optional[str] = Field(None, repr=False)
    expires_at: Optional[datetime] = None


class GetPreferenceProfileInput(BaseModel):
    event_id: Optional[str] = Field(
        None, min_length=1, description="Event ID; defaults to configured event"
    )


class GetPreferenceProfileOutput(BaseModel):
    event_id: str
    profile: Optional[AttendeePreferenceProfile] = None
    warnings: List[str] = Field(default_factory=list)


class ProposePreferenceUpdateInput(BaseModel):
    intent: str = Field(min_length=1, max_length=2000)
    event_id: Optional[str] = Field(
        None, min_length=1, description="Event ID; defaults to configured event"
    )


class SavePreferenceProfileInput(BaseModel):
    profile: AttendeePreferenceProfile
    expected_updated_at: Optional[datetime] = None


class SavePreferenceProfileOutput(BaseModel):
    status: Literal["SAVED", "CONFLICT"]
    profile: AttendeePreferenceProfile
    message: str


class ResetPreferenceProfileInput(BaseModel):
    event_id: Optional[str] = Field(
        None, min_length=1, description="Event ID; defaults to configured event"
    )


class ResetPreferenceProfileOutput(BaseModel):
    event_id: str
    deleted: bool
    message: str


class GenerateItineraryPlansInput(BaseModel):
    event_id: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    strategies: Optional[List[PlanStrategy]] = None


class GetItineraryPlanInput(BaseModel):
    plan_id: str = Field(min_length=1)


class CompareItineraryPlansInput(BaseModel):
    plan_ids: List[str] = Field(min_length=2, max_length=3)


class CompareItineraryPlansOutput(BaseModel):
    plans: List[ItineraryPlan]
    comparison: Dict[str, str]


class ExplainPlanChoiceInput(BaseModel):
    plan_id: str = Field(min_length=1)
    session_id: Optional[str] = Field(default=None, min_length=1)


class ExplainPlanChoiceOutput(BaseModel):
    plan: ItineraryPlan
    selected_explanations: List[Dict[str, Any]]
    excluded_reasons: List[Dict[str, Any]]


class PreviewItineraryApplicationInput(BaseModel):
    plan_id: str = Field(min_length=1)
    mode: ApplicationMode


class ApplyItineraryPlanInput(PreviewItineraryApplicationInput):
    approval_token: str = Field(min_length=1)


class GetApplyStatusInput(BaseModel):
    apply_run_id: str = Field(min_length=1)


class ResumeApplyRecoveryInput(GetApplyStatusInput):
    approval_token: str = Field(min_length=1)
