"""The single convention for concierge-owned transit personal-time blocks (SPEC-17 §17.2)."""

import re
from typing import Optional, Tuple

from ..models import PersonalTimeBlock

TRANSIT_MARKER = "concierge-transit:"
_LEGACY_DESCRIPTION = re.compile(r"^Travel \d+ min from .+ to .+ for ")
_MAX_DESCRIPTION = 250
_ROUTE = re.compile(r"^Transit: (?P<a>.+?) (?:→|to) (?P<b>.+)$")


def transit_title(from_venue: str, to_venue: str) -> str:
    return f"Transit: {from_venue} → {to_venue}"


def transit_description(reference: str, detail: str) -> str:
    return f"{TRANSIT_MARKER}{reference}:{detail}"[:_MAX_DESCRIPTION]


def _route(title: str) -> Optional[Tuple[str, str]]:
    match = _ROUTE.match(title or "")
    return (match.group("a"), match.group("b")) if match else None


def is_concierge_transit(block: PersonalTimeBlock) -> bool:
    description = block.description or ""
    if description.startswith(TRANSIT_MARKER):
        return True
    return _route(block.title) is not None and _LEGACY_DESCRIPTION.match(description) is not None


def same_route(title_a: str, title_b: str) -> bool:
    route_a, route_b = _route(title_a), _route(title_b)
    if route_a is not None and route_b is not None:
        return route_a == route_b
    return title_a == title_b
