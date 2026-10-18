"""Pure helpers that turn tool payloads into display data for the Streamlit UI."""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from ..exceptions import UnknownVenueError
from ..transit import TransitEngine

SEAT_BANDS = {
    "available": "Seats available",
    "limited": "Limited seats",
    "veryLimited": "Very limited seats",
    "unavailable": "Full",
    "walkUp": "Walk-up only",
}
TRANSIT_LABEL = "Transit"
OTHER_VENUE = "Other"


def to_local(value: str, zone: ZoneInfo) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(zone)


def seat_label(session: Dict[str, Any]) -> str:
    capacity, reserved = session.get("capacity"), session.get("reserved_count")
    if capacity is not None and reserved is not None:
        return f"{max(0, capacity - reserved)} of {capacity} seats available"
    return SEAT_BANDS.get(session.get("seat_availability") or "", "Seat availability unknown")


def canonical_venue(raw: str, transit: TransitEngine) -> str:
    try:
        return transit.normalize_venue(raw)
    except UnknownVenueError:
        return OTHER_VENUE


def agenda_blocks(
    schedule: Dict[str, Any], zone: ZoneInfo, transit: TransitEngine
) -> List[Dict[str, Any]]:
    """One calendar block per booked session and personal-time entry, in local time."""
    blocks = []
    for session in schedule.get("booked", []):
        blocks.append(
            _block(
                session["session_id"],
                session["title"],
                canonical_venue(session["venue_id"], transit),
                session["start_time"],
                session["end_time"],
                zone,
                level=session.get("level"),
                venue_label=session["venue_id"],
            )
        )
    for entry in schedule.get("personal_time", []):
        is_transit = entry["title"].startswith("Transit:")
        blocks.append(
            _block(
                None,
                entry["title"],
                TRANSIT_LABEL if is_transit else OTHER_VENUE,
                entry["start_time"],
                entry["end_time"],
                zone,
                venue_label=entry.get("location") or "",
            )
        )
    return blocks


def _block(
    session_id: Optional[str],
    title: str,
    group: str,
    start_raw: str,
    end_raw: str,
    zone: ZoneInfo,
    level: Optional[int] = None,
    venue_label: str = "",
) -> Dict[str, Any]:
    start, end = to_local(start_raw, zone), to_local(end_raw, zone)
    return {
        "session_id": session_id,
        "title": title,
        "group": group,
        "venue": venue_label,
        "level": level,
        # ISO date first so category order is chronological.
        "day": start.strftime("%Y-%m-%d (%a)"),
        "start_hour": start.hour + start.minute / 60,
        "hours": max((end - start).total_seconds() / 3600, 0.05),
        "time": f"{start:%H:%M}–{end:%H:%M}",
    }
