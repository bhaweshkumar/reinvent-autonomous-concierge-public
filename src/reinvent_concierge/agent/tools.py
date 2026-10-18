"""MCP agent orchestrator (SPEC-06): tool implementations behind the FastMCP server.

Split per SPEC-18 §18.1 into ``contracts`` (I/O models), ``audit_policy`` (redaction and
approval-guard policy), ``runner`` (construction and the audited dispatch wrapper), and
one mixin module per tool group (``tools_catalog``, ``tools_schedule``,
``tools_preferences``, ``tools_plans``). This module re-exports everything that used to
live here directly, and composes the final ``MCPAgent`` from the mixins.

Every tool validates its input with a Pydantic model and appends one audit entry,
whether it succeeds or fails.
"""

from .contracts import (
    AddToWaitlistInput,
    AddToWaitlistOutput,
    ApplyItineraryPlanInput,
    CancelReservationInput,
    CancelReservationOutput,
    CheckTransitInput,
    CheckTransitOutput,
    CompareItineraryPlansInput,
    CompareItineraryPlansOutput,
    ExplainPlanChoiceInput,
    ExplainPlanChoiceOutput,
    GenerateItineraryPlansInput,
    GetApplyStatusInput,
    GetItineraryPlanInput,
    GetPreferenceProfileInput,
    GetPreferenceProfileOutput,
    GetRecentOutcomesInput,
    GetRecentOutcomesOutput,
    GetScheduleInput,
    ListWaitlistInput,
    ListWaitlistOutput,
    ManageFavoritesInput,
    ManageFavoritesOutput,
    OptimizeAndBookInput,
    OptimizeAndBookOutput,
    PreviewItineraryApplicationInput,
    ProposePreferenceUpdateInput,
    RecentOutcome,
    RemoveFromWaitlistInput,
    RemoveFromWaitlistOutput,
    ResetPreferenceProfileInput,
    ResetPreferenceProfileOutput,
    ResumeApplyRecoveryInput,
    SavePreferenceProfileInput,
    SavePreferenceProfileOutput,
    SearchCatalogInput,
    SearchCatalogOutput,
    UpdateWaitlistEntryInput,
    UpdateWaitlistEntryOutput,
)
from .runner import _ToolRunner
from .tools_catalog import CatalogToolsMixin
from .tools_plans import PlansToolsMixin
from .tools_preferences import PreferencesToolsMixin
from .tools_schedule import ScheduleToolsMixin
from .tools_waitlist import WaitlistToolsMixin

__all__ = [
    "MCPAgent",
    "AddToWaitlistInput",
    "AddToWaitlistOutput",
    "ApplyItineraryPlanInput",
    "CancelReservationInput",
    "CancelReservationOutput",
    "CheckTransitInput",
    "CheckTransitOutput",
    "CompareItineraryPlansInput",
    "CompareItineraryPlansOutput",
    "ExplainPlanChoiceInput",
    "ExplainPlanChoiceOutput",
    "GenerateItineraryPlansInput",
    "GetApplyStatusInput",
    "GetItineraryPlanInput",
    "GetPreferenceProfileInput",
    "GetPreferenceProfileOutput",
    "GetRecentOutcomesInput",
    "GetRecentOutcomesOutput",
    "GetScheduleInput",
    "ListWaitlistInput",
    "ListWaitlistOutput",
    "ManageFavoritesInput",
    "ManageFavoritesOutput",
    "OptimizeAndBookInput",
    "OptimizeAndBookOutput",
    "PreviewItineraryApplicationInput",
    "ProposePreferenceUpdateInput",
    "RecentOutcome",
    "RemoveFromWaitlistInput",
    "RemoveFromWaitlistOutput",
    "ResetPreferenceProfileInput",
    "ResetPreferenceProfileOutput",
    "ResumeApplyRecoveryInput",
    "SavePreferenceProfileInput",
    "SavePreferenceProfileOutput",
    "SearchCatalogInput",
    "SearchCatalogOutput",
    "UpdateWaitlistEntryInput",
    "UpdateWaitlistEntryOutput",
]


class MCPAgent(
    CatalogToolsMixin,
    ScheduleToolsMixin,
    WaitlistToolsMixin,
    PreferencesToolsMixin,
    PlansToolsMixin,
    _ToolRunner,
):
    """Implements the MCP tools on top of the REST client and local services."""
