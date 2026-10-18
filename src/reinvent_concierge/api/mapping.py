"""Anti-corruption layer between AWS Events API wire payloads and domain models.

Wire shapes follow https://api.awsevents.com/v1/openapi.json (camelCase, optional
fields, local session times). Domain models use snake_case and timezone-aware UTC.
"""

import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..models import BulkFailure, BulkResult, Event, PersonalTimeBlock, Session
from ..venues import load_registry

UNKNOWN_VENUE = "Unknown"
PERSONAL_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"
PERSONAL_TIME_STEP_MINUTES = 5

_LEVEL_PATTERN = re.compile(r"\b([1-4]00)\b")
_FULL_BANDS = {"unavailable"}
# Assumption: the API has no hero flag, so hero sessions are recognized from taxonomy tags.
_HERO_TAG_FIELDS = ("features", "experiences", "segments", "areasOfInterest")
_REPEAT_SUFFIX = re.compile(r"-R\d*$", re.IGNORECASE)
_FLOOR = re.compile(r"\blevel\s+(\d{1,2})\b", re.IGNORECASE)


def _zone(*candidates: Optional[str]) -> ZoneInfo:
    for name in candidates:
        if not name:
            continue
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return ZoneInfo("UTC")


def parse_level(raw: Optional[Any]) -> Optional[int]:
    """Extract 100-400 from values such as ``"300 - Advanced"``."""
    if raw is None:
        return None
    match = _LEVEL_PATTERN.search(str(raw))
    return int(match.group(1)) if match else None


def _parse_clock(raw: Optional[str]) -> time:
    if not raw:
        return time(0, 0)
    for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p"):
        try:
            return datetime.strptime(raw.strip(), fmt).time()
        except ValueError:
            continue
    raise ValueError(f"Unrecognized session time {raw!r}")


def _mentions_hero(payload: Dict[str, Any]) -> bool:
    tags: Iterable[str] = (tag for field in _HERO_TAG_FIELDS for tag in payload.get(field) or [])
    return any("hero" in str(tag).lower() for tag in tags)


def repeat_group_for(code: Optional[str]) -> Optional[str]:
    """``AIM301-R1`` -> ``AIM301``; sessions sharing a group are repeats of one another."""
    text = (code or "").strip().upper()
    return _REPEAT_SUFFIX.sub("", text) or None


def _strings(payload: Dict[str, Any], field: str) -> List[str]:
    value = payload.get(field)
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _venue(payload: Dict[str, Any]) -> Tuple[str, str, Optional[str]]:
    """Return ``(venue_id, venue_source, zone)``.

    An API venue wins. Otherwise the venue comes from ``room``: a registered venue is canonical;
    an unregistered first segment is a *zone* (e.g. ``Expo``), not a venue, so ``venue_id`` stays
    ``Unknown`` and the segment is reported as ``zone``.
    """
    registry = load_registry()
    api_venue = payload.get("venue")
    if api_venue:
        match = registry.resolve(str(api_venue))
        return (match.canonical or str(api_venue)), ("REGISTRY" if match.canonical else "API"), None
    match = registry.resolve(payload.get("room"))
    if match.source == "UNKNOWN":
        return UNKNOWN_VENUE, "UNKNOWN", None
    if match.canonical:
        return match.canonical, match.source, None
    return UNKNOWN_VENUE, match.source, match.raw


def _floor(room: Optional[str]) -> Optional[int]:
    found = _FLOOR.search(room or "")
    return int(found.group(1)) if found else None


def session_from_api(payload: Dict[str, Any], default_timezone: str) -> Session:
    """Build a domain ``Session`` from a wire session.

    Raises:
        KeyError: If ``sessionId`` or ``title`` is missing
        ValueError: If the session has no schedulable date/time
    """
    session_time = payload.get("sessionTime") or {}
    if not session_time.get("date"):
        raise ValueError(f"Session {payload.get('sessionId')} has no scheduled date")

    zone = _zone(session_time.get("timezone"), default_timezone)
    local_start = datetime.combine(
        date.fromisoformat(session_time["date"]),
        _parse_clock(session_time.get("time")),
        tzinfo=zone,
    )
    default_length = 24 * 60 if payload.get("isAllDaySession") else 0
    length = int(session_time.get("length") or default_length)
    start = local_start.astimezone(timezone.utc)

    speakers = [s.get("name") for s in payload.get("speakers") or [] if s.get("name")]
    tracks = payload.get("tracks") or []
    band = payload.get("seatAvailability")
    venue_id, venue_source, venue_zone = _venue(payload)
    code = payload.get("abbreviation") or None

    return Session(
        session_id=payload["sessionId"],
        title=payload["title"],
        description=payload.get("abstract"),
        level=parse_level(payload.get("level")),
        track=", ".join(tracks) or None,
        venue_id=venue_id,
        venue_source=venue_source,
        zone=venue_zone,
        room_name=payload.get("room"),
        floor=_floor(payload.get("room")),
        session_code=code,
        session_type=payload.get("type"),
        features=_strings(payload, "features"),
        topics=_strings(payload, "topics"),
        roles=_strings(payload, "roles"),
        services=_strings(payload, "services"),
        repeat_group=repeat_group_for(code or payload.get("sessionId")),
        start_time=start,
        end_time=start + timedelta(minutes=length),
        status="FULL" if band in _FULL_BANDS else "OPEN",
        speaker_names=", ".join(speakers) or None,
        is_hero_speaker=_mentions_hero(payload),
        seat_availability=band,
        is_reservable=payload.get("isReservable"),
    )


def event_from_api(payload: Dict[str, Any]) -> Event:
    return Event(
        event_id=payload["eventId"],
        name=payload.get("name") or payload["eventId"],
        start_date=payload.get("startDate"),
        end_date=payload.get("endDate"),
        timezone=payload.get("timezone"),
        authentication_required=payload.get("authenticationRequired"),
    )


def _parse_utc(raw: str) -> datetime:
    return datetime.strptime(raw, PERSONAL_TIME_FORMAT).replace(tzinfo=timezone.utc)


def personal_time_from_api(payload: Dict[str, Any]) -> PersonalTimeBlock:
    return PersonalTimeBlock(
        id=payload["personalTimeId"],
        title=payload["title"],
        description=payload.get("description"),
        location=payload.get("location"),
        start_time=_parse_utc(payload["startDateTime"]),
        end_time=_parse_utc(payload["endDateTime"]),
    )


def bulk_result_from_api(payload: Dict[str, Any]) -> BulkResult:
    result = payload.get("result") or {}
    return BulkResult(
        successful=list(result.get("successful") or []),
        failed=[
            BulkFailure(
                session_id=failure["sessionId"],
                code=failure.get("code") or "other",
                conflicts_with=list(failure.get("conflictsWith") or []),
            )
            for failure in result.get("failed") or []
        ],
    )


def format_personal_time(value: datetime) -> str:
    """Format as the API expects: UTC, no offset, whole minutes (naive input is UTC)."""
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc)
    if value.second or value.microsecond:
        raise ValueError(f"Personal time must be on a whole minute, got {value.isoformat()}")
    return value.strftime(PERSONAL_TIME_FORMAT)


def build_personal_time_payload(
    title: str,
    start_time: datetime,
    end_time: datetime,
    description: str,
    location: Optional[str] = None,
) -> Dict[str, str]:
    """Validate the PersonalTimeInput contract and return the wire payload.

    Raises:
        ValueError: If any field violates the API constraints
    """
    if not 1 <= len(title) <= 128:
        raise ValueError("title must be 1-128 characters")
    if not 1 <= len(description) <= 250:
        raise ValueError("description must be 1-250 characters")
    if location is not None and not 1 <= len(location) <= 255:
        raise ValueError("location must be 1-255 characters")

    start, end = format_personal_time(start_time), format_personal_time(end_time)
    minutes = (_parse_utc(end) - _parse_utc(start)).total_seconds() / 60
    if minutes <= 0 or minutes % PERSONAL_TIME_STEP_MINUTES:
        raise ValueError("duration must be a positive multiple of 5 minutes")

    payload = {
        "title": title,
        "description": description,
        "startDateTime": start,
        "endDateTime": end,
    }
    if location:
        payload["location"] = location
    return payload
