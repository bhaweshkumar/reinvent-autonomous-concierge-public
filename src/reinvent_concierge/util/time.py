"""Time policy (ADR-014): store and transmit UTC; display and compute in the event zone."""

from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def as_utc(value: datetime) -> datetime:
    """Return ``value`` as aware UTC; naive input is interpreted as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def is_valid_zone(name: Optional[str]) -> bool:
    """True when ``name`` is a real IANA zone key."""
    if not name:
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


def event_zone(name: Optional[str], fallback: str) -> ZoneInfo:
    """Resolve the event's IANA zone, falling back when it is missing or unknown."""
    for candidate in (name, fallback):
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            continue
    return ZoneInfo("UTC")


def to_event_local(value: datetime, zone: ZoneInfo) -> datetime:
    """Convert ``value`` to the event zone."""
    return as_utc(value).astimezone(zone)


def local_iso(value: datetime, zone: ZoneInfo) -> str:
    """ISO 8601 in the event zone with offset, truncated to the minute."""
    return (
        to_event_local(value, zone).replace(second=0, microsecond=0).isoformat(timespec="minutes")
    )
