"""Audit redaction and approval-guard policy shared by every tool (SPEC-18 §18.1)."""

from typing import Any, Callable, Dict, FrozenSet, List, Optional, Tuple

from pydantic import BaseModel

from ..booking.changes import ChangeKind, PlanChange
from ..models import AttendeeSchedule, ConflictReport, Session
from ..planner import (
    ApplyResult,
    ItineraryPlan,
    PlanPreview,
    PlanSet,
    PreferenceUpdateProposal,
)
from .contracts import (
    GetPreferenceProfileOutput,
    SavePreferenceProfileOutput,
    SearchCatalogOutput,
)

# Favoriting something already favorited is not a failure from the attendee's view.
_BENIGN_FAVORITE_CODES = {"alreadyFavorited"}

# (audit action, status) -> outcome label shown in "Recent snapper outcomes" (SPEC-18 §18.4).
_OUTCOME_LABELS: Dict[Tuple[str, str], str] = {
    ("RESERVE_SESSION", "CONFIRMED"): "SNAGGED",
    ("RESERVE_SESSION", "DEFERRED"): "DEFERRED",
    ("RESERVE_SESSION", "FAILED"): "FAILED",
    ("SWAP_PROPOSED", "PROPOSED"): "SWAP_PROPOSED",
    ("TRANSIT_CONFLICT", "MANUAL_REQUIRED"): "TRANSIT_CONFLICT",
    ("TRANSIT_CONFLICT", "SKIPPED"): "TRANSIT_CHECK_SKIPPED",
    ("THROTTLED", "WATCHING"): "THROTTLED",
}

FAVORITE_RISK = "Adds a schedule favorite; no reservation is changed."
UNFAVORITE_RISK = "Removes a schedule favorite; no reservation is changed."
RESERVE_RISK = "Reserves once without cancelling an existing reservation."
SWAP_RISK = (
    "Releases an approved reservation before one reservation attempt; "
    "restoration is best effort only."
)
CANCEL_RISK = "Cancels an approved reservation; the seat may not be available again."
INCOMPLETE_APPROVAL = "apply_run_id and approval_token must be given together"
OTHER_CHANGE = "approval is for another change"


def describe(conflicts: List[ConflictReport]) -> str:
    return "; ".join(
        f"{c.session_a_id} -> {c.session_b_id}: {c.deficit_minutes} min short "
        f"({c.from_venue} -> {c.to_venue})"
        for c in conflicts
    )


def overlaps(a: Session, b: Session) -> bool:
    return a.start_time < b.end_time and b.start_time < a.end_time


def other_side(conflict: ConflictReport, session_id: str) -> str:
    return conflict.session_b_id if conflict.session_a_id == session_id else conflict.session_a_id


def failure_reason(result: ApplyResult) -> str:
    """Refusal codes, else journaled errors, else the run state; never a token."""
    codes = [code for _session_id, code in result.refusal_codes]
    errors = [op.error for op in result.operations if op.error]
    return ", ".join(codes or errors) or f"run ended {result.state}"


def favorite_failure(result: ApplyResult) -> Optional[str]:
    """None when the favorite change took effect (``alreadyFavorited`` included)."""
    if result.state == "APPLIED":
        return None
    codes = [code for _session_id, code in result.refusal_codes]
    if codes and all(code in _BENIGN_FAVORITE_CODES for code in codes):
        return None
    return next((c for c in codes if c not in _BENIGN_FAVORITE_CODES), None) or failure_reason(
        result
    )


def held_by_must_attend(session_ids: List[str]) -> str:
    return f"{', '.join(session_ids)} is must-attend; remove it from your preference profile first"


def approval_guard(
    kind: ChangeKind, session_id: str, must_attend: FrozenSet[str]
) -> Callable[[Tuple[PlanChange, ...]], Optional[str]]:
    """Bind an approval to one change of ``kind`` for ``session_id``; never release a
    session the current profile marks must-attend."""

    def guard(changes: Tuple[PlanChange, ...]) -> Optional[str]:
        if len(changes) != 1 or changes[0].kind is not kind:
            return OTHER_CHANGE
        change = changes[0]
        if change.session_id != session_id:
            return OTHER_CHANGE
        released = change.blocking_session_ids if kind is ChangeKind.SWAP else (session_id,)
        held = sorted(set(released) & must_attend)
        return held_by_must_attend(held) if held else None

    return guard


def confirmed_session_ids(result: ApplyResult, operation_type: str) -> List[str]:
    ids = (
        op.request.get("session_id")
        for op in result.operations
        if op.operation_type == operation_type and op.state == "CONFIRMED"
    )
    return [session_id for session_id in ids if isinstance(session_id, str)]


def without_tokens(value: Any) -> Any:
    """Approval tokens are secrets: drop them at any depth before auditing."""
    if isinstance(value, dict):
        return {k: without_tokens(v) for k, v in value.items() if k != "approval_token"}
    if isinstance(value, list):
        return [without_tokens(item) for item in value]
    return value


def audit_result(result: BaseModel) -> Any:
    """Keep audit lines small and free of approval tokens."""
    return without_tokens(audit_summary(result))


def audit_summary(result: BaseModel) -> Any:
    """Record IDs and counts rather than full payloads."""
    if isinstance(result, SearchCatalogOutput):
        return {
            "total_found": result.total_found,
            "session_ids": [s.session_id for s in result.sessions],
        }
    if isinstance(result, AttendeeSchedule):
        return {
            "booked": [s.session_id for s in result.booked],
            "favorites": [s.session_id for s in result.favorites],
            "personal_time_count": len(result.personal_time),
        }
    if isinstance(result, GetPreferenceProfileOutput):
        return {
            "event_id": result.event_id,
            "profile_id": str(result.profile.profile_id) if result.profile else None,
            "unresolved_session_count": len(result.profile.unresolved_session_ids)
            if result.profile
            else 0,
        }
    if isinstance(result, PreferenceUpdateProposal):
        return {
            "event_id": result.event_id,
            "proposed_fields": sorted(result.proposed_patch),
            "ambiguity_count": len(result.ambiguities),
            "unsupported_request_count": len(result.unsupported_requests),
        }
    if isinstance(result, SavePreferenceProfileOutput):
        return {"status": result.status, "profile_id": str(result.profile.profile_id)}
    if isinstance(result, PlanSet):
        return {
            "plan_set_id": str(result.plan_set_id),
            "plan_ids": [str(plan.plan_id) for plan in result.plans],
            "plan_count": len(result.plans),
            "conflict_count": len(result.conflict_reasons),
        }
    if isinstance(result, ItineraryPlan):
        return {"plan_id": str(result.plan_id), "strategy": result.strategy.value}
    if isinstance(result, PlanPreview):
        return {
            "apply_run_id": str(result.apply_run_id),
            "plan_id": str(result.plan_id),
            "effective_mode": result.effective_mode.value,
            "change_count": len(result.changes),
            "expires_at": result.expires_at.isoformat(),
        }
    if isinstance(result, ApplyResult):
        return {"apply_run_id": str(result.apply_run_id), "state": result.state}
    return result.model_dump(mode="json")


def recent_outcomes(entries: List[Dict[str, Any]], limit: int = 10) -> List[Dict[str, str]]:
    """Latest snapper outcomes: reservations, swap proposals, transit warnings, throttling.

    ``entries`` is newest-first (as returned by ``AuditLogger.recent_entries``).
    """
    outcomes = []
    for entry in entries:
        label = _OUTCOME_LABELS.get((entry.get("action", ""), entry.get("status", "")))
        if label is None:
            continue
        outcomes.append(
            {
                "session_id": entry.get("session_id", ""),
                "status": label,
                "timestamp": entry.get("timestamp", ""),
                "details": entry.get("details", ""),
            }
        )
        if len(outcomes) >= limit:
            break
    return outcomes


def audit_arguments(tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Do not place approval tokens, local preference content or free text in the audit log."""
    arguments = {key: value for key, value in arguments.items() if key != "approval_token"}
    if tool_name == "propose_preference_update":
        return {
            "event_id": arguments.get("event_id"),
            "intent_present": bool(arguments.get("intent")),
        }
    if tool_name == "save_preference_profile":
        profile = arguments.get("profile")
        return {
            "event_id": getattr(profile, "event_id", None),
            "profile_id": str(getattr(profile, "profile_id", "")) or None,
            "expected_updated_at": (
                arguments["expected_updated_at"].isoformat()
                if arguments.get("expected_updated_at")
                else None
            ),
        }
    return arguments
