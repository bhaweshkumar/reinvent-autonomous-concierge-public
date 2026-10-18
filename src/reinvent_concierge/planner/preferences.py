"""Event-scoped attendee preference profiles and local persistence."""

import json
import re
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from datetime import datetime, time
from decimal import Decimal
from enum import Enum
from typing import Dict, FrozenSet, List, Optional, Tuple, Union, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from ..models import SESSION_CODE_PATTERN, utc_now
from ..util.time import as_utc as _as_utc
from .state_store import PlannerStateStore


class _PreferenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_assignment=True)

    @field_validator("*")
    @classmethod
    def normalize_datetimes(cls, value: object) -> object:
        return _as_utc(value) if isinstance(value, datetime) else value


class SeatPolicy(str, Enum):
    RESERVABLE_ONLY = "RESERVABLE_ONLY"
    INCLUDE_WALK_UP = "INCLUDE_WALK_UP"
    ANY = "ANY"


class WeightedInterestQuery(_PreferenceModel):
    query: str = Field(min_length=1, max_length=200)
    weight: int = Field(ge=1, le=100)


class FixedPersonalTime(_PreferenceModel):
    title: str = Field(min_length=1, max_length=120)
    start_time: datetime
    end_time: datetime
    description: Optional[str] = Field(default=None, max_length=500)
    location: Optional[str] = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def validate_duration(self) -> "FixedPersonalTime":
        if self.end_time <= self.start_time:
            raise ValueError("fixed personal time must end after it starts")
        return self


class DailyTimeWindow(_PreferenceModel):
    day: str = Field(min_length=1, max_length=20)
    earliest: time
    latest: time

    @field_validator("day")
    @classmethod
    def normalize_day(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if normalized not in {
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
        }:
            raise ValueError("daily time window day must be a weekday name")
        return normalized

    @model_validator(mode="after")
    def validate_window(self) -> "DailyTimeWindow":
        if self.latest <= self.earliest:
            raise ValueError("daily time window latest time must be after earliest time")
        return self


class RequiredBreak(_PreferenceModel):
    label: str = Field(min_length=1, max_length=120)
    start_time: time
    end_time: time
    duration_minutes: int = Field(ge=5, le=720, multiple_of=5)
    is_hard: bool = True

    @model_validator(mode="after")
    def validate_duration_fits_window(self) -> "RequiredBreak":
        start_minutes = self.start_time.hour * 60 + self.start_time.minute
        end_minutes = self.end_time.hour * 60 + self.end_time.minute
        available = (end_minutes - start_minutes) % (24 * 60)
        if available == 0 or self.duration_minutes > available:
            raise ValueError("break duration must fit within its time window")
        return self


class StrategyWeights(_PreferenceModel):
    interest_relevance: int = Field(35, ge=0, le=100)
    topic_fit: int = Field(15, ge=0, le=100)
    seat_confidence: int = Field(15, ge=0, le=100)
    transit_efficiency: int = Field(15, ge=0, le=100)
    pacing_and_breaks: int = Field(10, ge=0, le=100)
    content_diversity: int = Field(10, ge=0, le=100)

    @model_validator(mode="after")
    def validate_total(self) -> "StrategyWeights":
        if sum(self.model_dump().values()) != 100:
            raise ValueError("strategy weights must total 100")
        return self


class AttendeePreferenceProfile(_PreferenceModel):
    profile_id: UUID = Field(default_factory=uuid4)
    event_id: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=80)
    interest_queries: Tuple[WeightedInterestQuery, ...] = Field(min_length=1)
    preferred_levels: FrozenSet[int] = Field(default_factory=frozenset)
    preferred_tracks: FrozenSet[str] = Field(default_factory=frozenset)
    preferred_services: FrozenSet[str] = Field(default_factory=frozenset)
    must_attend_session_ids: FrozenSet[str] = Field(default_factory=frozenset)
    avoid_session_ids: FrozenSet[str] = Field(default_factory=frozenset)
    fixed_personal_time: Tuple[FixedPersonalTime, ...] = Field(default_factory=tuple)
    daily_time_windows: Tuple[DailyTimeWindow, ...] = Field(default_factory=tuple)
    required_breaks: Tuple[RequiredBreak, ...] = Field(default_factory=tuple)
    max_sessions_per_day: int = Field(6, ge=1, le=12)
    max_venue_changes_per_day: int = Field(3, ge=0, le=10)
    transit_multiplier: Decimal = Field(Decimal("1.0"), ge=Decimal("1.0"), le=Decimal("3.0"))
    minimum_gap_minutes: int = Field(0, ge=0, le=60)
    seat_policy: SeatPolicy = SeatPolicy.RESERVABLE_ONLY
    strategy_weights: StrategyWeights = Field(default_factory=StrategyWeights)
    unresolved_session_ids: FrozenSet[str] = Field(default_factory=frozenset)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator(
        "preferred_tracks",
        "preferred_services",
        "must_attend_session_ids",
        "avoid_session_ids",
        "unresolved_session_ids",
    )
    @classmethod
    def normalize_string_sets(cls, value: FrozenSet[str]) -> FrozenSet[str]:
        normalized = frozenset(item.strip() for item in value if item.strip())
        if len(normalized) != len(value):
            raise ValueError("preference set values must be non-empty strings")
        return normalized

    @field_validator("preferred_levels")
    @classmethod
    def validate_levels(cls, value: FrozenSet[int]) -> FrozenSet[int]:
        valid_levels = {100, 200, 300, 400}
        if not value.issubset(valid_levels):
            raise ValueError("preferred levels must be one of 100, 200, 300, or 400")
        return value

    @model_validator(mode="after")
    def validate_commitments_and_windows(self) -> "AttendeePreferenceProfile":
        overlap = self.must_attend_session_ids & self.avoid_session_ids
        if overlap:
            raise ValueError(
                "session IDs cannot be both must-attend and avoided: " + ", ".join(sorted(overlap))
            )
        days = [window.day for window in self.daily_time_windows]
        if len(days) != len(set(days)):
            raise ValueError("daily time windows may contain each weekday only once")
        return self


class PreferenceProfileConflictError(RuntimeError):
    """Raised when a caller attempts to save a stale profile revision."""

    def __init__(self, latest: AttendeePreferenceProfile) -> None:
        super().__init__("preference profile was updated by another editor")
        self.latest = latest


class PreferenceImportResult(_PreferenceModel):
    profile: AttendeePreferenceProfile
    warnings: Tuple[str, ...] = Field(default_factory=tuple)


class PreferenceUpdateProposal(_PreferenceModel):
    event_id: str
    proposed_patch: Dict[str, JsonValue] = Field(default_factory=dict)
    unchanged_fields: Tuple[str, ...] = Field(default_factory=tuple)
    ambiguities: Tuple[str, ...] = Field(default_factory=tuple)
    unsupported_requests: Tuple[str, ...] = Field(default_factory=tuple)
    confirmation_questions: Tuple[str, ...] = Field(default_factory=tuple)


class PreferenceProfileStore:
    """Stores one local, versioned preference profile per event."""

    def __init__(self, store: PlannerStateStore) -> None:
        self._store = store

    async def get(self, event_id: str) -> Optional[AttendeePreferenceProfile]:
        async with self._store._connect() as connection:
            row = await (
                await connection.execute(
                    """
                    SELECT profile_json
                    FROM preference_profiles
                    WHERE event_id = ?
                    ORDER BY rowid DESC
                    LIMIT 1
                    """,
                    (event_id,),
                )
            ).fetchone()
        return self._profile_from_json(row[0]) if row is not None else None

    async def save(
        self,
        profile: AttendeePreferenceProfile,
        expected_updated_at: Optional[datetime],
    ) -> AttendeePreferenceProfile:
        expected = _as_utc(expected_updated_at).isoformat() if expected_updated_at else None
        async with self._store._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            row = await (
                await connection.execute(
                    """
                    SELECT profile_id, profile_json, created_at, updated_at
                    FROM preference_profiles
                    WHERE event_id = ?
                    ORDER BY rowid DESC
                    LIMIT 1
                    """,
                    (profile.event_id,),
                )
            ).fetchone()
            if row is None:
                if expected is not None:
                    await connection.rollback()
                    raise ValueError("cannot update a missing preference profile")
                saved = profile.model_copy(update={"updated_at": utc_now()})
                await connection.execute(
                    """
                    INSERT INTO preference_profiles
                    (profile_id, event_id, profile_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    self._row_values(saved),
                )
            else:
                current = self._profile_from_json(row[1])
                if (
                    current.profile_id != UUID(cast(str, row[0]))
                    or current.event_id != profile.event_id
                ):
                    await connection.rollback()
                    raise RuntimeError("stored preference profile identity does not match its row")
                if expected != cast(str, row[3]):
                    await connection.rollback()
                    raise PreferenceProfileConflictError(current)
                if profile.profile_id != current.profile_id:
                    await connection.rollback()
                    raise ValueError("profile_id is immutable after creation")
                saved = profile.model_copy(
                    update={"created_at": current.created_at, "updated_at": utc_now()}
                )
                cursor = await connection.execute(
                    """
                    UPDATE preference_profiles
                    SET profile_json = ?, created_at = ?, updated_at = ?
                    WHERE event_id = ? AND updated_at = ?
                    """,
                    (
                        saved.model_dump_json(),
                        saved.created_at.isoformat(),
                        saved.updated_at.isoformat(),
                        saved.event_id,
                        expected,
                    ),
                )
                if cursor.rowcount != 1:
                    await connection.rollback()
                    latest = await self.get(profile.event_id)
                    if latest is None:
                        raise RuntimeError("preference profile disappeared during save")
                    raise PreferenceProfileConflictError(latest)
            await connection.commit()
        return saved

    async def reset(self, event_id: str) -> bool:
        async with self._store._connect() as connection:
            cursor = await connection.execute(
                "DELETE FROM preference_profiles WHERE event_id = ?", (event_id,)
            )
            await connection.commit()
        return cursor.rowcount > 0

    async def export_json(self, event_id: str) -> Optional[Dict[str, JsonValue]]:
        profile = await self.get(event_id)
        if profile is None:
            return None
        return {
            "version": 1,
            "profile": cast(JsonValue, profile.model_dump(mode="json")),
        }

    async def import_json(
        self,
        payload: Union[str, Mapping[str, JsonValue]],
        known_session_ids: Optional[AbstractSet[str]] = None,
    ) -> PreferenceImportResult:
        raw = (
            cast(Mapping[str, JsonValue], json.loads(payload))
            if isinstance(payload, str)
            else payload
        )
        if raw.get("version") != 1:
            raise ValueError("unsupported preference profile export version")
        raw_profile = raw.get("profile")
        if not isinstance(raw_profile, Mapping):
            raise ValueError("preference profile export must contain a profile object")
        profile = AttendeePreferenceProfile.model_validate(
            cast(Mapping[str, JsonValue], raw_profile)
        )
        session_ids = profile.must_attend_session_ids | profile.avoid_session_ids
        unresolved = (
            session_ids
            if known_session_ids is None
            else frozenset(
                session_id for session_id in session_ids if session_id not in known_session_ids
            )
        )
        profile = profile.model_copy(
            update={"unresolved_session_ids": profile.unresolved_session_ids | unresolved}
        )
        warnings = tuple(
            f"Session {session_id} is unresolved until the local catalog is synced"
            for session_id in sorted(unresolved)
        )
        return PreferenceImportResult(profile=profile, warnings=warnings)

    @staticmethod
    def _row_values(profile: AttendeePreferenceProfile) -> Tuple[str, str, str, str, str]:
        return (
            str(profile.profile_id),
            profile.event_id,
            profile.model_dump_json(),
            profile.created_at.isoformat(),
            profile.updated_at.isoformat(),
        )

    @staticmethod
    def _profile_from_json(payload: object) -> AttendeePreferenceProfile:
        if not isinstance(payload, (str, bytes, bytearray)):
            raise RuntimeError("preference profile JSON column is not text or bytes")
        return AttendeePreferenceProfile.model_validate_json(payload)


_VENUE_CHANGE_LIMIT = re.compile(
    r"(?:avoid\s+)?(?:no more than|more than|maximum|max)\s+"
    r"(\d+|zero|one|two|three|four|five|six|seven|eight|nine|ten)\s+venue changes?"
)
_NUMBER_WORDS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}


def propose_update(
    event_id: str,
    intent: str,
    current_profile: Optional[AttendeePreferenceProfile],
) -> PreferenceUpdateProposal:
    """Translate a constrained set of clear phrases into a reviewable profile patch."""
    normalized = intent.casefold()
    patch: Dict[str, JsonValue] = {}
    ambiguities: List[str] = []
    unsupported: List[str] = []
    questions: List[str] = []

    queries = list(current_profile.interest_queries if current_profile else ())
    existing_queries = {item.query.casefold() for item in queries}
    if "genai" in normalized or "generative ai" in normalized:
        if "generative ai" not in existing_queries:
            queries.append(WeightedInterestQuery(query="generative AI", weight=80))
    if "serverless" in normalized and "serverless" not in existing_queries:
        queries.append(WeightedInterestQuery(query="serverless", weight=80))
    if queries:
        query_payload: List[Dict[str, JsonValue]] = [
            {"query": item.query, "weight": item.weight} for item in queries
        ]
        patch["interest_queries"] = cast(JsonValue, query_payload)

    if "advanced" in normalized:
        levels = set(current_profile.preferred_levels if current_profile else ())
        levels.add(400)
        patch["preferred_levels"] = cast(JsonValue, sorted(levels))

    venue_match = _VENUE_CHANGE_LIMIT.search(normalized)
    if venue_match:
        limit = venue_match.group(1)
        venue_changes = int(limit) if limit.isdigit() else _NUMBER_WORDS[limit]
        if 0 <= venue_changes <= 10:
            patch["max_venue_changes_per_day"] = venue_changes
        else:
            ambiguities.append("Venue-change limit must be between 0 and 10.")

    if "monday afternoon" in normalized:
        window_payload: List[Dict[str, JsonValue]] = [
            {
                "day": item.day,
                "earliest": item.earliest.isoformat(),
                "latest": item.latest.isoformat(),
            }
            for item in (current_profile.daily_time_windows if current_profile else ())
            if item.day != "monday"
        ]
        window_payload.append({"day": "monday", "earliest": "00:00:00", "latest": "12:00:00"})
        patch["daily_time_windows"] = cast(JsonValue, window_payload)
    elif "weekday afternoon" in normalized or "afternoon free" in normalized:
        ambiguities.append("Which weekday afternoons should remain free?")
        questions.append("Which weekday or weekdays should the profile keep free after noon?")

    if "keynote" in normalized:
        codes = SESSION_CODE_PATTERN.findall(intent.upper())
        if codes:
            commitments = set(current_profile.must_attend_session_ids if current_profile else ())
            commitments.update(codes)
            patch["must_attend_session_ids"] = cast(JsonValue, sorted(commitments))
        else:
            ambiguities.append("The keynote was mentioned without a session ID or code.")
            questions.append("Which keynote session ID should be preserved as must-attend?")

    if any(word in normalized for word in ("book ", "reserve ", "cancel ")):
        unsupported.append(
            "Schedule writes are not preference updates and require a separate tool."
        )
    if "hotel" in normalized or "flight" in normalized:
        unsupported.append("Travel and hotel requests are outside the local preference profile.")

    profile_fields = set(AttendeePreferenceProfile.model_fields)
    excluded = {"profile_id", "event_id", "created_at", "updated_at", "unresolved_session_ids"}
    unchanged = tuple(sorted(profile_fields - excluded - set(patch)))
    return PreferenceUpdateProposal(
        event_id=event_id,
        proposed_patch=patch,
        unchanged_fields=unchanged,
        ambiguities=tuple(ambiguities),
        unsupported_requests=tuple(unsupported),
        confirmation_questions=tuple(questions),
    )
