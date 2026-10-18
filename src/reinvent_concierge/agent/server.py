"""FastMCP server exposing the concierge tools over Stdio (Claude Desktop) or SSE (web UI)."""

from datetime import datetime
from typing import Annotated, Literal, Optional

from fastmcp import FastMCP
from pydantic import Field

from ..models import AttendeeSchedule
from ..planner import (
    ApplicationMode,
    ApplyResult,
    AttendeePreferenceProfile,
    ItineraryPlan,
    PlanPreview,
    PlanSet,
    PlanStrategy,
    PreferenceUpdateProposal,
)
from .tools import (
    AddToWaitlistOutput,
    CancelReservationOutput,
    CheckTransitOutput,
    CompareItineraryPlansOutput,
    ExplainPlanChoiceOutput,
    GetPreferenceProfileOutput,
    GetRecentOutcomesOutput,
    ListWaitlistOutput,
    ManageFavoritesOutput,
    MCPAgent,
    OptimizeAndBookOutput,
    RemoveFromWaitlistOutput,
    ResetPreferenceProfileOutput,
    SavePreferenceProfileOutput,
    SearchCatalogOutput,
    UpdateWaitlistEntryOutput,
)

INSTRUCTIONS = (
    "Concierge for AWS re:Invent 2026. Search the session catalog, inspect the attendee's "
    "schedule, book sessions with venue-transit conflict handling, manage favorites and "
    "queue full sessions for automatic seat snagging."
)

SessionId = Annotated[str, Field(min_length=1, description="Session ID from search results")]


def build_server(agent: MCPAgent) -> FastMCP:
    """Register the agent's tools on a new FastMCP server."""
    mcp = FastMCP("reinvent-concierge", instructions=INSTRUCTIONS)

    @mcp.tool
    async def search_catalog(
        query: Annotated[str, Field(min_length=1, description="What to look for")],
        level: Annotated[Optional[int], Field(ge=100, le=400, multiple_of=100)] = None,
        venue: Annotated[Optional[str], Field(description="Venue, e.g. 'Venetian'")] = None,
        hero_only: bool = False,
        limit: Annotated[int, Field(ge=1, le=20)] = 5,
        session_type: Annotated[
            Optional[str],
            Field(max_length=64, description="Format, e.g. Workshop, Breakout session"),
        ] = None,
        topic: Annotated[
            Optional[str],
            Field(max_length=100, description="Topic, e.g. Artificial Intelligence"),
        ] = None,
    ) -> SearchCatalogOutput:
        """Search open re:Invent sessions by topic with optional level/venue/hero/format/topic
        filters."""
        return await agent.search_catalog(
            query, level, venue, hero_only, limit, session_type, topic
        )

    @mcp.tool
    async def get_my_schedule(
        include_favorites: bool = True, include_personal_time: bool = True
    ) -> AttendeeSchedule:
        """Return the attendee's reserved sessions, favorites and personal time blocks."""
        return await agent.get_my_schedule(include_favorites, include_personal_time)

    @mcp.tool
    async def optimize_and_book_session(
        session_id: SessionId,
        auto_resolve_conflicts: Annotated[
            bool,
            Field(description="Add transit buffers for venue changes; never cancels anything"),
        ] = True,
        apply_run_id: Annotated[
            Optional[str], Field(description="Swap proposal to execute (from a prior call)")
        ] = None,
        approval_token: Annotated[
            Optional[str], Field(description="Single-use approval token of that proposal")
        ] = None,
    ) -> OptimizeAndBookOutput:
        """Reserve a session and add transit buffers. A clash is never resolved by canceling:
        it returns a swap proposal with an approval token; call again with apply_run_id and
        approval_token once the attendee approves. Favorites the session while reservations
        are closed."""
        return await agent.optimize_and_book_session(
            session_id, auto_resolve_conflicts, apply_run_id, approval_token
        )

    @mcp.tool
    async def manage_favorites(
        action: Literal["add", "remove"], session_id: SessionId
    ) -> ManageFavoritesOutput:
        """Add a session to favorites or remove it."""
        return await agent.manage_favorites(action, session_id)

    @mcp.tool
    async def check_transit(
        from_session_id: SessionId, to_session_id: SessionId
    ) -> CheckTransitOutput:
        """Check whether the gap between two sessions covers the walk between venues."""
        return await agent.check_transit(from_session_id, to_session_id)

    @mcp.tool
    async def add_to_waitlist(
        session_id: SessionId,
        priority: Annotated[int, Field(ge=1, le=100, description="1 = highest")] = 50,
    ) -> AddToWaitlistOutput:
        """Queue a full session; the seat snapper reserves it when a seat opens."""
        return await agent.add_to_waitlist(session_id, priority)

    @mcp.tool
    async def list_waitlist() -> ListWaitlistOutput:
        """List every queued waitlist entry, by priority then age."""
        return await agent.list_waitlist()

    @mcp.tool
    async def update_waitlist_entry(
        session_id: SessionId,
        priority: Annotated[Optional[int], Field(ge=1, le=100, description="1 = highest")] = None,
        enabled: Annotated[
            Optional[bool], Field(description="Whether the snapper may auto-reserve it")
        ] = None,
    ) -> UpdateWaitlistEntryOutput:
        """Change a waitlist entry's priority and/or whether the snapper may act on it."""
        return await agent.update_waitlist_entry(session_id, priority, enabled)

    @mcp.tool
    async def remove_from_waitlist(session_id: SessionId) -> RemoveFromWaitlistOutput:
        """Remove one entry from the waitlist."""
        return await agent.remove_from_waitlist(session_id)

    @mcp.tool
    async def get_recent_outcomes(
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> GetRecentOutcomesOutput:
        """Latest snapper outcomes: reservations, swap proposals, transit warnings, throttling."""
        return await agent.get_recent_outcomes(limit)

    @mcp.tool
    async def cancel_reservation(
        session_id: SessionId,
        apply_run_id: Annotated[
            Optional[str], Field(description="Cancel proposal to execute (from a prior call)")
        ] = None,
        approval_token: Annotated[
            Optional[str], Field(description="Single-use approval token of that proposal")
        ] = None,
    ) -> CancelReservationOutput:
        """Cancel one reservation in two steps: the first call returns APPROVAL_REQUIRED with
        an approval token; call again with apply_run_id and approval_token to cancel."""
        return await agent.cancel_reservation(session_id, apply_run_id, approval_token)

    @mcp.tool
    async def get_preference_profile(
        event_id: Annotated[Optional[str], Field(min_length=1, description="Event ID")] = None,
    ) -> GetPreferenceProfileOutput:
        """Return the local preference profile for an event; no AWS data is read."""
        return await agent.get_preference_profile(event_id)

    @mcp.tool
    async def propose_preference_update(
        intent: Annotated[str, Field(min_length=1, max_length=2000)],
        event_id: Annotated[Optional[str], Field(min_length=1, description="Event ID")] = None,
    ) -> PreferenceUpdateProposal:
        """Propose a deterministic preference patch without saving or changing a schedule."""
        return await agent.propose_preference_update(intent, event_id)

    @mcp.tool
    async def save_preference_profile(
        profile: AttendeePreferenceProfile,
        expected_updated_at: Annotated[
            Optional[datetime],
            Field(description="Current profile updated_at for optimistic concurrency"),
        ] = None,
    ) -> SavePreferenceProfileOutput:
        """Persist an explicitly reviewed local profile, returning conflict details when stale."""
        return await agent.save_preference_profile(profile, expected_updated_at)

    @mcp.tool
    async def reset_preference_profile(
        event_id: Annotated[Optional[str], Field(min_length=1, description="Event ID")] = None,
    ) -> ResetPreferenceProfileOutput:
        """Delete one local event preference profile; AWS schedule data is untouched."""
        return await agent.reset_preference_profile(event_id)

    @mcp.tool
    async def generate_itinerary_plans(
        event_id: Annotated[str, Field(min_length=1)],
        profile_id: Annotated[str, Field(min_length=1)],
        strategies: Optional[list[PlanStrategy]] = None,
    ) -> PlanSet:
        """Generate read-only, deterministic itinerary alternatives from local catalog data."""
        return await agent.generate_itinerary_plans(event_id, profile_id, strategies)

    @mcp.tool
    async def get_itinerary_plan(plan_id: Annotated[str, Field(min_length=1)]) -> ItineraryPlan:
        """Return an immutable itinerary plan persisted locally during generation."""
        return await agent.get_itinerary_plan(plan_id)

    @mcp.tool
    async def compare_itinerary_plans(
        plan_ids: Annotated[list[str], Field(min_length=2, max_length=3)],
    ) -> CompareItineraryPlansOutput:
        """Compare score and trade-offs across plans from the same plan set."""
        return await agent.compare_itinerary_plans(plan_ids)

    @mcp.tool
    async def explain_plan_choice(
        plan_id: Annotated[str, Field(min_length=1)],
        session_id: Annotated[Optional[str], Field(min_length=1)] = None,
    ) -> ExplainPlanChoiceOutput:
        """Return deterministic selected and excluded reason codes for a plan choice."""
        return await agent.explain_plan_choice(plan_id, session_id)

    @mcp.tool
    async def preview_itinerary_application(
        plan_id: Annotated[str, Field(min_length=1)], mode: ApplicationMode
    ) -> PlanPreview:
        """Build a fingerprint-bound, expiring application preview without schedule writes."""
        return await agent.preview_itinerary_application(plan_id, mode)

    @mcp.tool
    async def apply_itinerary_plan(
        plan_id: Annotated[str, Field(min_length=1)],
        mode: ApplicationMode,
        approval_token: Annotated[str, Field(min_length=1)],
    ) -> ApplyResult:
        """Apply one approved preview and reconcile the resulting AWS schedule."""
        return await agent.apply_itinerary_plan(plan_id, mode, approval_token)

    @mcp.tool
    async def get_apply_status(
        apply_run_id: Annotated[str, Field(min_length=1)],
    ) -> ApplyResult:
        """Return durable operation states and any manual follow-up required."""
        return await agent.get_apply_status(apply_run_id)

    @mcp.tool
    async def resume_apply_recovery(
        apply_run_id: Annotated[str, Field(min_length=1)],
        approval_token: Annotated[str, Field(min_length=1)],
    ) -> ApplyResult:
        """Reconcile interrupted operation attempts; it does not issue blind retries."""
        return await agent.resume_apply_recovery(apply_run_id, approval_token)

    return mcp
