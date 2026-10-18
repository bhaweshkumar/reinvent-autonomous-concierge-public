"""Planner snapshots and apply journal records."""

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Dict, Literal, Optional, Tuple
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from ..models import AttendeeSchedule, utc_now
from ..util.time import as_utc as _as_utc

ApplyRunState = Literal[
    "PREVIEWED",
    "APPROVED",
    "APPLYING",
    "VERIFYING",
    "APPLIED",
    "STALE",
    "PARTIAL",
    "COMPENSATING",
    "COMPENSATED",
    "COMPENSATION_INCOMPLETE",
    "FAILED",
]
ApplyOperationState = Literal[
    "PENDING",
    "ATTEMPTING",
    "CONFIRMED",
    "REFUSED",
    "FAILED",
    "COMPENSATED",
    "COMPENSATION_FAILED",
]

_SENSITIVE_KEYS = {
    "authorization",
    "access_token",
    "refresh_token",
    "id_token",
    "token",
    "token_hash",
    "api_key",
}


def _reject_sensitive_mapping_keys(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested_value in value.items():
            if isinstance(key, str) and key.casefold() in _SENSITIVE_KEYS:
                raise ValueError(f"sensitive key is not allowed: {key}")
            _reject_sensitive_mapping_keys(nested_value)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_sensitive_mapping_keys(item)


class _NaiveDatetimeAsUtcModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_assignment=True)

    @field_validator("*")
    @classmethod
    def normalize_datetimes(cls, value: object) -> object:
        if not isinstance(value, datetime):
            return value
        return _as_utc(value)


class CanonicalPersonalTime(_NaiveDatetimeAsUtcModel):
    personal_time_id: str
    title: str
    start_time: datetime
    end_time: datetime
    description: Optional[str] = None
    location: Optional[str] = None


def _canonicalize_session_ids(session_ids: Tuple[str, ...]) -> Tuple[str, ...]:
    return tuple(sorted(session_ids))


def _canonicalize_personal_time(
    personal_time: Tuple[CanonicalPersonalTime, ...],
) -> Tuple[CanonicalPersonalTime, ...]:
    return tuple(
        sorted(
            personal_time,
            key=lambda item: (item.personal_time_id, item.start_time, item.end_time),
        )
    )


def _schedule_fingerprint(
    booked_session_ids: Tuple[str, ...],
    favorite_session_ids: Tuple[str, ...],
    personal_time: Tuple[CanonicalPersonalTime, ...],
) -> str:
    booked_session_ids = _canonicalize_session_ids(booked_session_ids)
    favorite_session_ids = _canonicalize_session_ids(favorite_session_ids)
    personal_time = _canonicalize_personal_time(personal_time)
    payload = {
        "booked_session_ids": booked_session_ids,
        "favorite_session_ids": favorite_session_ids,
        "personal_time": [
            {
                "personal_time_id": item.personal_time_id,
                "title": item.title,
                "start_time": item.start_time.isoformat(),
                "end_time": item.end_time.isoformat(),
                "description": item.description,
                "location": item.location,
            }
            for item in personal_time
        ],
    }
    canonical_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


class ScheduleSnapshot(_NaiveDatetimeAsUtcModel):
    snapshot_id: UUID = Field(default_factory=uuid4)
    event_id: str
    captured_at: datetime = Field(default_factory=utc_now)
    booked_session_ids: Tuple[str, ...]
    favorite_session_ids: Tuple[str, ...]
    personal_time: Tuple[CanonicalPersonalTime, ...]
    fingerprint: str

    @field_validator("booked_session_ids", "favorite_session_ids")
    @classmethod
    def canonicalize_session_ids(cls, value: Tuple[str, ...]) -> Tuple[str, ...]:
        return _canonicalize_session_ids(value)

    @field_validator("personal_time")
    @classmethod
    def canonicalize_and_reject_duplicate_personal_time_ids(
        cls, value: Tuple[CanonicalPersonalTime, ...]
    ) -> Tuple[CanonicalPersonalTime, ...]:
        seen = set()
        for item in value:
            if item.personal_time_id in seen:
                raise ValueError(f"duplicate personal_time ID: {item.personal_time_id}")
            seen.add(item.personal_time_id)
        return _canonicalize_personal_time(value)

    @model_validator(mode="after")
    def validate_fingerprint(self) -> "ScheduleSnapshot":
        expected = _schedule_fingerprint(
            self.booked_session_ids,
            self.favorite_session_ids,
            self.personal_time,
        )
        if self.fingerprint != expected:
            raise ValueError("fingerprint does not match snapshot contents")
        return self

    @classmethod
    def from_schedule(cls, event_id: str, schedule: AttendeeSchedule) -> "ScheduleSnapshot":
        booked_session_ids = tuple(session.session_id for session in schedule.booked)
        favorite_session_ids = tuple(session.session_id for session in schedule.favorites)
        personal_time = tuple(
            CanonicalPersonalTime(
                personal_time_id=item.id,
                title=item.title,
                start_time=_as_utc(item.start_time),
                end_time=_as_utc(item.end_time),
                description=item.description,
                location=item.location,
            )
            for item in schedule.personal_time
        )
        return cls(
            event_id=event_id,
            booked_session_ids=booked_session_ids,
            favorite_session_ids=favorite_session_ids,
            personal_time=personal_time,
            fingerprint=_schedule_fingerprint(
                booked_session_ids,
                favorite_session_ids,
                personal_time,
            ),
        )


class ApplyRun(_NaiveDatetimeAsUtcModel):
    run_id: UUID = Field(default_factory=uuid4)
    event_id: str
    profile_id: UUID
    plan_id: Optional[UUID] = None
    snapshot_fingerprint: str
    state: ApplyRunState = "PREVIEWED"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    error: Optional[str] = None


class ApplyOperation(_NaiveDatetimeAsUtcModel):
    operation_id: UUID = Field(default_factory=uuid4)
    apply_run_id: UUID
    order: int = Field(ge=1)
    operation_type: str
    request: Dict[str, JsonValue]
    result: Optional[Dict[str, JsonValue]] = None
    state: ApplyOperationState = "PENDING"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    error: Optional[str] = None

    @field_validator("request", "result", "error", mode="before")
    @classmethod
    def reject_sensitive_mapping_keys(cls, value: object) -> object:
        _reject_sensitive_mapping_keys(value)
        return value

    @model_serializer(mode="wrap")
    def reject_sensitive_mapping_keys_before_serialization(
        self, handler: SerializerFunctionWrapHandler
    ) -> object:
        _reject_sensitive_mapping_keys(self.request)
        _reject_sensitive_mapping_keys(self.result)
        return handler(self)
