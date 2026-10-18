"""Venue registry (ADR-011): canonical venues, aliases, and inter-venue minutes.

Low-level and dependency-free so both the wire mapping and the transit engine can use it.
"""

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any, Dict, List, Literal, Optional, Tuple

VenueSource = Literal["REGISTRY", "ROOM_PARSED", "UNKNOWN"]
_SEGMENT_SPLIT = re.compile(r"[,|]")
_DEFAULT_COLOR = "#7f7f7f"
_SOURCES = frozenset({"estimate", "published", "measured"})


@dataclass(frozen=True)
class VenueMatch:
    canonical: Optional[str]
    raw: str
    source: VenueSource


class VenueRegistry:
    def __init__(self, data: Dict[str, Any]) -> None:
        venues = data["venues"]
        if not isinstance(venues, list):
            raise ValueError("venues.json: 'venues' must be a list")
        self._colors: Dict[str, str] = {}
        aliases: Dict[str, str] = {}
        names: List[str] = []
        for venue in venues:
            name = str(venue["name"])
            names.append(name)
            self._colors[name] = str(venue.get("color") or _DEFAULT_COLOR)
            aliases[name.casefold()] = name
            for alias in venue.get("aliases", []):
                aliases[" ".join(str(alias).casefold().split())] = name
        self.venues: Tuple[str, ...] = tuple(names)
        # Longest aliases first so "mandalay bay" wins over "mandalay" in free text.
        self._patterns = [
            (re.compile(rf"\b{re.escape(alias)}\b"), alias, name)
            for alias, name in sorted(aliases.items(), key=lambda item: len(item[0]), reverse=True)
        ]
        self._minutes: Dict[Tuple[str, str], int] = {}
        matrix = data["matrix"]
        if not isinstance(matrix, list):
            raise ValueError("venues.json: 'matrix' must be a list")
        for entry in matrix:
            a, b, minutes = str(entry["a"]), str(entry["b"]), int(entry["minutes"])
            if a not in self._colors or b not in self._colors:
                raise ValueError(f"venues.json: matrix names unregistered venue: {a} / {b}")
            if entry.get("source") not in _SOURCES:
                raise ValueError(f"venues.json: invalid matrix source for {a} / {b}")
            self._minutes[(a, b)] = minutes
            self._minutes[(b, a)] = minutes

        # SPEC-19 §19.1: each term is optional and defaults to 0, never raises on a gap.
        self._inside_walk: Dict[str, int] = {}
        for name, entry in (data.get("inside_walk") or {}).items():
            if name not in self._colors:
                raise ValueError(f"venues.json: inside_walk unregistered venue: {name}")
            if entry.get("source") not in _SOURCES:
                raise ValueError(f"venues.json: invalid inside_walk source for {name}")
            self._inside_walk[name] = int(entry["minutes"])

        self._floor_minutes: Dict[str, int] = {}
        for name, entry in (data.get("floor_minutes") or {}).items():
            if name not in self._colors:
                raise ValueError(f"venues.json: floor_minutes unregistered venue: {name}")
            if entry.get("source") not in _SOURCES:
                raise ValueError(f"venues.json: invalid floor_minutes source for {name}")
            self._floor_minutes[name] = int(entry["minutes"])

        self._zone_penalties: Dict[Tuple[str, str, str], int] = {}
        for name, zones in (data.get("zone_penalties") or {}).items():
            if name not in self._colors:
                raise ValueError(f"venues.json: zone_penalties unregistered venue: {name}")
            if not isinstance(zones, dict):
                raise ValueError(f"venues.json: zone_penalties for {name} must be an object")
            for pair_key, entry in zones.items():
                if entry.get("source") not in _SOURCES:
                    raise ValueError(
                        f"venues.json: invalid zone_penalties source for {name}/{pair_key}"
                    )
                zone_a, _sep, zone_b = str(pair_key).partition("|")
                minutes = int(entry["minutes"])
                self._zone_penalties[(name, zone_a, zone_b)] = minutes
                self._zone_penalties[(name, zone_b, zone_a)] = minutes

    def canonical(self, text: str) -> Optional[str]:
        """Registered venue for free text: first `,`/`|` segment wins (longest alias first);
        otherwise the leftmost alias match anywhere, ties broken by the longer alias."""
        key = " ".join((text or "").casefold().split())
        if not key:
            return None
        first = _SEGMENT_SPLIT.split(key, maxsplit=1)[0].strip()
        for pattern, _alias, name in self._patterns:
            if first and pattern.search(first):
                return name
        best: Optional[Tuple[int, int, str]] = None
        for pattern, alias, name in self._patterns:
            found = pattern.search(key)
            if found is not None:
                rank = (found.start(), -len(alias), name)
                if best is None or rank < best:
                    best = rank
        return best[2] if best else None

    def resolve(self, text: Optional[str]) -> VenueMatch:
        raw = " ".join((text or "").split())
        if not raw:
            return VenueMatch(None, "", "UNKNOWN")
        name = self.canonical(raw)
        if name is not None:
            return VenueMatch(name, name, "REGISTRY")
        first = _SEGMENT_SPLIT.split(raw, maxsplit=1)[0].strip()
        return VenueMatch(None, first or raw, "ROOM_PARSED")

    def minutes(self, a: str, b: str) -> Optional[int]:
        if a == b and a in self._colors:
            return 0
        return self._minutes.get((a, b))

    def color(self, name: str) -> str:
        return self._colors.get(name, _DEFAULT_COLOR)

    def inside_walk(self, venue: str) -> int:
        """Minutes to walk from a session's room to the venue's transit point."""
        return self._inside_walk.get(venue, 0)

    def floor_minutes(self, venue: str) -> int:
        """Minutes per floor crossed inside ``venue`` (SPEC-19 §19.1's ``floor_delta`` basis)."""
        return self._floor_minutes.get(venue, 0)

    def zone_penalty(self, venue: str, zone_a: Optional[str], zone_b: Optional[str]) -> int:
        """Extra minutes to cross from ``zone_a`` to ``zone_b`` inside ``venue``."""
        if not zone_a or not zone_b or zone_a == zone_b:
            return 0
        return self._zone_penalties.get((venue, zone_a, zone_b), 0)


@lru_cache(maxsize=1)
def load_registry() -> VenueRegistry:
    text = resources.files("reinvent_concierge.data").joinpath("venues.json").read_text("utf-8")
    return VenueRegistry(json.loads(text))
