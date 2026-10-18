"""Waitlist and recent-outcomes tools (SPEC-18 §18.1/§18.4 split of agent/tools.py).

New tools (§18.4): list/update/remove and recent outcomes move behavior the Streamlit
UI used to perform by reading the waitlist and audit stores directly, so the UI becomes
a thin MCP client.
"""

from typing import Optional

from .audit_policy import recent_outcomes
from .contracts import (
    AddToWaitlistInput,
    AddToWaitlistOutput,
    GetRecentOutcomesInput,
    GetRecentOutcomesOutput,
    ListWaitlistInput,
    ListWaitlistOutput,
    RecentOutcome,
    RemoveFromWaitlistInput,
    RemoveFromWaitlistOutput,
    UpdateWaitlistEntryInput,
    UpdateWaitlistEntryOutput,
)
from .runner import _ToolRunner


class WaitlistToolsMixin(_ToolRunner):
    # ------------------------------------------------------------------ tool 6

    async def add_to_waitlist(self, session_id: str, priority: int = 50) -> AddToWaitlistOutput:
        """Queue a full session for the SeatSnapperWorker."""
        arguments = {"session_id": session_id, "priority": priority}
        return await self._run_tool(
            "add_to_waitlist", AddToWaitlistInput, arguments, self._add_to_waitlist
        )

    async def _add_to_waitlist(self, params: AddToWaitlistInput) -> AddToWaitlistOutput:
        session = await self._lookup_session(params.session_id)
        entry = self.waitlist.add(
            params.session_id, params.priority, title=session.title, venue_id=session.venue_id
        )
        return AddToWaitlistOutput(
            session_id=entry.session_id,
            priority=entry.priority,
            message=f"Session {entry.session_id} waitlisted with priority {entry.priority}",
        )

    # ------------------------------------------------------------- tools 6b-6d (SPEC-18 §18.4)

    async def list_waitlist(self) -> ListWaitlistOutput:
        """List every queued waitlist entry, by priority then age."""
        return await self._run_tool("list_waitlist", ListWaitlistInput, {}, self._list_waitlist)

    async def _list_waitlist(self, _params: ListWaitlistInput) -> ListWaitlistOutput:
        return ListWaitlistOutput(entries=self.waitlist.entries())

    async def update_waitlist_entry(
        self,
        session_id: str,
        priority: Optional[int] = None,
        enabled: Optional[bool] = None,
    ) -> UpdateWaitlistEntryOutput:
        """Change a waitlist entry's priority and/or whether the snapper may act on it."""
        return await self._run_tool(
            "update_waitlist_entry",
            UpdateWaitlistEntryInput,
            {"session_id": session_id, "priority": priority, "enabled": enabled},
            self._update_waitlist_entry,
        )

    async def _update_waitlist_entry(
        self, params: UpdateWaitlistEntryInput
    ) -> UpdateWaitlistEntryOutput:
        entry = self.waitlist.update(
            params.session_id, priority=params.priority, enabled=params.enabled
        )
        message = (
            f"Session {params.session_id} updated"
            if entry
            else f"Session {params.session_id} is not on the waitlist"
        )
        return UpdateWaitlistEntryOutput(session_id=params.session_id, entry=entry, message=message)

    async def remove_from_waitlist(self, session_id: str) -> RemoveFromWaitlistOutput:
        """Remove one entry from the waitlist."""
        return await self._run_tool(
            "remove_from_waitlist",
            RemoveFromWaitlistInput,
            {"session_id": session_id},
            self._remove_from_waitlist,
        )

    async def _remove_from_waitlist(
        self, params: RemoveFromWaitlistInput
    ) -> RemoveFromWaitlistOutput:
        removed = self.waitlist.remove(params.session_id)
        message = (
            f"Session {params.session_id} removed from the waitlist"
            if removed
            else f"Session {params.session_id} was not on the waitlist"
        )
        return RemoveFromWaitlistOutput(
            session_id=params.session_id, removed=removed, message=message
        )

    async def get_recent_outcomes(self, limit: int = 10) -> GetRecentOutcomesOutput:
        """Latest snapper outcomes: reservations, swap proposals, transit warnings, throttling."""
        return await self._run_tool(
            "get_recent_outcomes",
            GetRecentOutcomesInput,
            {"limit": limit},
            self._get_recent_outcomes,
        )

    async def _get_recent_outcomes(self, params: GetRecentOutcomesInput) -> GetRecentOutcomesOutput:
        entries = self.audit.recent_entries()
        outcomes = recent_outcomes(entries, params.limit)
        return GetRecentOutcomesOutput(outcomes=[RecentOutcome(**item) for item in outcomes])
