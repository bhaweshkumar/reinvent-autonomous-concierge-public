"""Domain models for re:Invent Autonomous Concierge.

These are the internal (snake_case) shapes. Wire payloads from the AWS Events API are
translated into them by ``src.api_mapping``.
"""

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator, model_validator

from .util.time import local_iso

VALID_LEVELS = (100, 200, 300, 400)

SessionStatus = Literal["OPEN", "FULL", "CANCELLED"]
WaitlistStatus = Literal["WATCHING", "FULL", "FAILED", "SWAP_PROPOSED"]

# Shared by the itinerary candidate builder and the preference-intent parser.
SESSION_CODE_PATTERN = re.compile(r"\b([A-Z]{2,10}[0-9]{2,5})\b")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Session(BaseModel):
    """AWS re:Invent session."""

    session_id: str = Field(..., description="Unique session identifier")
    title: str = Field(..., description="Session title")
    description: Optional[str] = Field(None, description="Session abstract")
    level: Optional[int] = Field(None, description="Session level (100-400)")
    track: Optional[str] = Field(None, description="Session track(s)")
    venue_id: str = Field(..., description="Venue name as published by the event")
    room_name: Optional[str] = Field(None, description="Room name")
    start_time: datetime = Field(..., description="Session start time")
    end_time: datetime = Field(..., description="Session end time")
    capacity: Optional[int] = Field(None, ge=0, description="Maximum capacity, when known")
    reserved_count: Optional[int] = Field(None, ge=0, description="Seats taken, when known")
    status: Optional[SessionStatus] = Field(None, description="OPEN, FULL or CANCELLED")
    speaker_names: Optional[str] = Field(None, description="Comma-separated speaker names")
    is_hero_speaker: bool = Field(False, description="Whether an AWS Hero presents")
    seat_availability: Optional[str] = Field(
        None, description="API seat band: available, limited, veryLimited, unavailable, walkUp"
    )
    is_reservable: Optional[bool] = Field(None, description="Whether seats can be reserved")
    session_code: Optional[str] = Field(None, description="Published session code, e.g. AIM301")
    session_type: Optional[str] = Field(None, description="Format, e.g. Breakout session")
    features: List[str] = Field(default_factory=list, description="Format features")
    topics: List[str] = Field(default_factory=list)
    roles: List[str] = Field(default_factory=list)
    services: List[str] = Field(default_factory=list)
    venue_source: Literal["API", "REGISTRY", "ROOM_PARSED", "UNKNOWN"] = Field(
        "API", description="How venue_id was determined"
    )
    floor: Optional[int] = Field(None, description="Floor parsed from the room, when present")
    zone: Optional[str] = Field(None, description="Venue zone, when the registry defines one")
    repeat_group: Optional[str] = Field(
        None, description="Session code without repeat suffix; equal values are repeats"
    )
    updated_at: Optional[datetime] = None
    start_local: Optional[str] = Field(
        None, description="Start in the event timezone (ISO 8601 with offset); display only"
    )
    end_local: Optional[str] = Field(
        None, description="End in the event timezone (ISO 8601 with offset); display only"
    )

    def localized(self, zone: ZoneInfo) -> "Session":
        """Copy with the event-zone display fields filled."""
        return self.model_copy(
            update={
                "start_local": local_iso(self.start_time, zone),
                "end_local": local_iso(self.end_time, zone),
            }
        )

    @field_validator("level")
    @classmethod
    def _check_level(cls, value: Optional[int]) -> Optional[int]:
        if value is not None and value not in VALID_LEVELS:
            raise ValueError(f"level must be one of {VALID_LEVELS}")
        return value

    @model_validator(mode="after")
    def _check_times(self) -> "Session":
        if self.end_time < self.start_time:
            raise ValueError("end_time must not be before start_time")
        return self

    @property
    def has_open_seat(self) -> bool:
        """True when a seat can be taken; uses exact counts if known, else the status band."""
        if self.status == "CANCELLED" or self.is_reservable is False:
            return False
        if self.capacity is not None and self.reserved_count is not None:
            return self.capacity > self.reserved_count
        return self.status == "OPEN"


class PersonalTimeBlock(BaseModel):
    """Transit buffer or other personal time block."""

    id: str = Field(..., description="Personal time block ID")
    title: str = Field(..., description="Block title")
    start_time: datetime = Field(..., description="Block start time (UTC)")
    end_time: datetime = Field(..., description="Block end time (UTC)")
    description: Optional[str] = Field(None, description="Block description")
    location: Optional[str] = Field(None, description="Free-text location")


class AttendeeSchedule(BaseModel):
    """Complete attendee schedule."""

    booked: List[Session] = Field(default_factory=list, description="Reserved sessions")
    favorites: List[Session] = Field(default_factory=list, description="Favorite sessions")
    personal_time: List[PersonalTimeBlock] = Field(
        default_factory=list, description="Personal time blocks"
    )
    event_timezone: Optional[str] = Field(None, description="IANA zone used for *_local fields")


class SessionSummary(BaseModel):
    """Lightweight session summary for search results."""

    session_id: str
    title: str
    level: Optional[int] = None
    venue_id: str
    track: Optional[str] = None
    start_time: datetime
    end_time: datetime
    speaker_names: Optional[str] = None
    is_hero_speaker: bool = False
    status: Optional[str] = None
    capacity: Optional[int] = None
    reserved_count: Optional[int] = None
    seat_availability: Optional[str] = None
    relevance_score: Optional[float] = None
    start_local: Optional[str] = Field(
        None, description="Start in the event timezone (ISO 8601 with offset); display only"
    )
    end_local: Optional[str] = Field(
        None, description="End in the event timezone (ISO 8601 with offset); display only"
    )

    def localized(self, zone: ZoneInfo) -> "SessionSummary":
        """Copy with the event-zone display fields filled."""
        return self.model_copy(
            update={
                "start_local": local_iso(self.start_time, zone),
                "end_local": local_iso(self.end_time, zone),
            }
        )


class Event(BaseModel):
    """An AWS event (e.g. re:Invent 2026)."""

    event_id: str
    name: str
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    timezone: Optional[str] = None
    authentication_required: Optional[bool] = None


class BulkFailure(BaseModel):
    """One session a bulk request (reserve / favorite) was refused for."""

    session_id: str
    code: str
    conflicts_with: List[str] = Field(default_factory=list)


class BulkResult(BaseModel):
    """Per-session outcome of a bulk request; a 200 can still contain failures."""

    successful: List[str] = Field(default_factory=list)
    failed: List[BulkFailure] = Field(default_factory=list)


class ReservationResult(BaseModel):
    """Result from reservation attempt."""

    session_id: str
    status: Literal["CONFIRMED", "WAITLISTED", "FAILED"]
    message: Optional[str] = None
    code: Optional[str] = Field(None, description="API refusal code, e.g. scheduleConflict")
    conflicts_with: List[str] = Field(default_factory=list)


class WaitlistEntry(BaseModel):
    """Waitlist entry consumed by the SeatSnapperWorker."""

    session_id: str
    priority: int = Field(50, ge=1, le=100, description="Priority (1=highest, 100=lowest)")
    added_at: datetime = Field(default_factory=utc_now)
    last_checked: Optional[datetime] = None
    status: WaitlistStatus = "WATCHING"
    enabled: bool = Field(True, description="Whether the snapper may auto-reserve it")
    title: Optional[str] = None
    venue_id: Optional[str] = None


class ConflictReport(BaseModel):
    """Detected schedule conflict between two consecutive booked sessions."""

    session_a_id: str
    session_b_id: str
    from_venue: str
    to_venue: str
    deficit_minutes: int
    suggested_action: Literal["ADD_BUFFER", "CANCEL_ONE"]
    estimate_quality: Literal["NORMAL", "LOW"] = "NORMAL"


class ResolutionSummary(BaseModel):
    """Conflict resolution summary."""

    buffers_added: List[str] = Field(default_factory=list, description="Added PersonalTime IDs")
    unresolved_conflicts: List[ConflictReport] = Field(
        default_factory=list, description="Conflicts that require manual resolution"
    )


class FeasibilityResult(BaseModel):
    """Transit feasibility check result (SPEC-19 §19.1)."""

    is_feasible: bool
    transit_needed: int
    available_gap: int
    deficit: int
    from_venue: str
    to_venue: str
    margin_minutes: int = Field(
        0, description="available_gap minus the required minutes (including any lead time)"
    )
    estimate_quality: Literal["NORMAL", "LOW"] = Field(
        "NORMAL", description="LOW when either venue did not resolve through the registry"
    )
    tight: bool = Field(False, description="Feasible, but with less than 10 minutes of margin")


class AuditLogEntry(BaseModel):
    """One line of the append-only JSON-lines audit log."""

    timestamp: datetime = Field(default_factory=utc_now)
    action: str = Field(..., description="Action or MCP tool name")
    session_id: Optional[str] = None
    status: Optional[str] = None
    attendee_token_hash: str = Field(..., description="SHA-256 of the token, never the token")
    details: Optional[str] = None
    arguments: Optional[Dict[str, Any]] = None
    result: Optional[Any] = None
