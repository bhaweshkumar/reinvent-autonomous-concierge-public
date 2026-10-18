"""Preference profile tools (SPEC-18 §18.1 split of agent/tools.py)."""

from datetime import datetime
from typing import Optional

from ..planner import (
    AttendeePreferenceProfile,
    PreferenceProfileConflictError,
    PreferenceUpdateProposal,
    propose_update,
)
from .contracts import (
    GetPreferenceProfileInput,
    GetPreferenceProfileOutput,
    ProposePreferenceUpdateInput,
    ResetPreferenceProfileInput,
    ResetPreferenceProfileOutput,
    SavePreferenceProfileInput,
    SavePreferenceProfileOutput,
)
from .runner import _ToolRunner


class PreferencesToolsMixin(_ToolRunner):
    # ------------------------------------------------------------------ tool 8

    async def get_preference_profile(
        self, event_id: Optional[str] = None
    ) -> GetPreferenceProfileOutput:
        """Return the local event-scoped preference profile without contacting AWS."""
        return await self._run_tool(
            "get_preference_profile",
            GetPreferenceProfileInput,
            {"event_id": event_id},
            self._get_preference_profile,
        )

    async def _get_preference_profile(
        self, params: GetPreferenceProfileInput
    ) -> GetPreferenceProfileOutput:
        event_id = self._event_id(params.event_id)
        profile = await self._preference_store().get(event_id)
        warnings = (
            [
                f"Session {session_id} is unresolved until the local catalog is synced"
                for session_id in sorted(profile.unresolved_session_ids)
            ]
            if profile
            else []
        )
        return GetPreferenceProfileOutput(event_id=event_id, profile=profile, warnings=warnings)

    # ------------------------------------------------------------------ tool 9

    async def propose_preference_update(
        self, intent: str, event_id: Optional[str] = None
    ) -> PreferenceUpdateProposal:
        """Translate clear preference intent into a reviewable patch without saving it."""
        return await self._run_tool(
            "propose_preference_update",
            ProposePreferenceUpdateInput,
            {"intent": intent, "event_id": event_id},
            self._propose_preference_update,
        )

    async def _propose_preference_update(
        self, params: ProposePreferenceUpdateInput
    ) -> PreferenceUpdateProposal:
        event_id = self._event_id(params.event_id)
        current = await self._preference_store().get(event_id)
        return propose_update(event_id, params.intent, current)

    # ------------------------------------------------------------------ tool 10

    async def save_preference_profile(
        self,
        profile: AttendeePreferenceProfile,
        expected_updated_at: Optional[datetime] = None,
    ) -> SavePreferenceProfileOutput:
        """Save an explicitly reviewed local profile using optimistic concurrency."""
        return await self._run_tool(
            "save_preference_profile",
            SavePreferenceProfileInput,
            {"profile": profile, "expected_updated_at": expected_updated_at},
            self._save_preference_profile,
        )

    async def _save_preference_profile(
        self, params: SavePreferenceProfileInput
    ) -> SavePreferenceProfileOutput:
        try:
            profile = await self._preference_store().save(
                params.profile, params.expected_updated_at
            )
        except PreferenceProfileConflictError as error:
            return SavePreferenceProfileOutput(
                status="CONFLICT",
                profile=error.latest,
                message=(
                    "Profile changed in another editor; review the latest version before saving."
                ),
            )
        return SavePreferenceProfileOutput(
            status="SAVED", profile=profile, message="Preference profile saved locally."
        )

    # ------------------------------------------------------------------ tool 11

    async def reset_preference_profile(
        self, event_id: Optional[str] = None
    ) -> ResetPreferenceProfileOutput:
        """Delete the local preference profile for one event; AWS data is unaffected."""
        return await self._run_tool(
            "reset_preference_profile",
            ResetPreferenceProfileInput,
            {"event_id": event_id},
            self._reset_preference_profile,
        )

    async def _reset_preference_profile(
        self, params: ResetPreferenceProfileInput
    ) -> ResetPreferenceProfileOutput:
        event_id = self._event_id(params.event_id)
        deleted = await self._preference_store().reset(event_id)
        return ResetPreferenceProfileOutput(
            event_id=event_id,
            deleted=deleted,
            message=(
                "Preference profile reset locally." if deleted else "No preference profile exists."
            ),
        )
