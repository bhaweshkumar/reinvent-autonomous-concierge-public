"""Transit engine for calculating travel times between venues."""

import logging
import math
from datetime import datetime
from decimal import Decimal
from typing import List, Literal, Optional, Tuple

import networkx as nx

from ..exceptions import UnknownVenueError
from ..models import FeasibilityResult
from ..venues import VenueRegistry, load_registry

logger = logging.getLogger(__name__)

EstimateQuality = Literal["NORMAL", "LOW"]


def _fold(text: str) -> str:
    return " ".join((text or "").casefold().split())


class TransitEngine:
    """Calculates transit times between AWS re:Invent venues using NetworkX.

    Venues, aliases and minutes come from the packaged venue registry (ADR-011).
    """

    def __init__(self, registry: Optional[VenueRegistry] = None):
        self.registry = registry or load_registry()
        self.graph = nx.Graph()
        for a in self.registry.venues:
            self.graph.add_node(a)
            for b in self.registry.venues:
                minutes = self.registry.minutes(a, b)
                if a != b and minutes is not None:
                    self.graph.add_edge(a, b, weight=minutes)
        logger.info("TransitEngine initialized with %d venues", self.graph.number_of_nodes())

    def get_transit_minutes(self, from_venue: str, to_venue: str) -> int:
        """Direct transit time between venues; 0 for the same venue.

        Raises:
            UnknownVenueError: If either venue is not recognized (and they differ)
        """
        if _fold(from_venue) and _fold(from_venue) == _fold(to_venue):
            return 0
        source = self.normalize_venue(from_venue)
        target = self.normalize_venue(to_venue)
        minutes = self.registry.minutes(source, target)
        if minutes is None:
            raise UnknownVenueError(f"No transit time for {source} → {target}")
        return minutes

    def required_minutes(
        self,
        from_venue: str,
        to_venue: str,
        *,
        from_floor: Optional[int] = None,
        to_floor: Optional[int] = None,
        from_zone: Optional[str] = None,
        to_zone: Optional[str] = None,
        transit_multiplier: Decimal = Decimal("1.0"),
    ) -> Tuple[int, EstimateQuality]:
        """``inter_venue + inside_walk(a) + inside_walk(b) + floor_delta + zone_penalty``,
        scaled by ``transit_multiplier`` (SPEC-19 §19.1).

        Never raises: an unregistered venue, floor, or zone contributes 0 to the sum rather
        than blocking the calculation, and ``estimate_quality`` drops to ``LOW`` instead.
        """
        same_text = bool(_fold(from_venue)) and _fold(from_venue) == _fold(to_venue)
        source_canon = self.registry.canonical(from_venue)
        target_canon = self.registry.canonical(to_venue)
        quality: EstimateQuality = "NORMAL" if (source_canon and target_canon) else "LOW"

        if same_text and source_canon is None:
            # Identical unregistered venue text: treat as one place, no registry data to add.
            raw = 0
        else:
            source = source_canon or from_venue
            target = target_canon or to_venue
            same_venue = source_canon is not None and source_canon == target_canon
            if same_venue:
                # Staying inside one venue: no inside_walk (that's the walk out to transit),
                # only the floor and zone crossed to get from one room to the other.
                floor_delta = (
                    abs(from_floor - to_floor) * self.registry.floor_minutes(source)
                    if from_floor is not None and to_floor is not None
                    else 0
                )
                zone_penalty = self.registry.zone_penalty(source, from_zone, to_zone)
                raw = floor_delta + zone_penalty
            else:
                inter_venue = self.registry.minutes(source, target) or 0
                walk_from = self.registry.inside_walk(source_canon) if source_canon else 0
                walk_to = self.registry.inside_walk(target_canon) if target_canon else 0
                raw = inter_venue + walk_from + walk_to

        return math.ceil(raw * float(transit_multiplier)), quality

    def check_feasibility(
        self,
        session_a_end: datetime,
        session_b_start: datetime,
        from_venue: str,
        to_venue: str,
        *,
        from_floor: Optional[int] = None,
        to_floor: Optional[int] = None,
        from_zone: Optional[str] = None,
        to_zone: Optional[str] = None,
        transit_multiplier: Decimal = Decimal("1.0"),
        reserved_seat_lead_minutes: int = 0,
    ) -> FeasibilityResult:
        """Check whether the gap between two sessions covers the transit time (+ any lead).

        Never raises (SPEC-19 §19.1): an unrecognized venue degrades ``estimate_quality`` to
        ``LOW`` instead of blocking the check.
        """
        transit_needed, quality = self.required_minutes(
            from_venue,
            to_venue,
            from_floor=from_floor,
            to_floor=to_floor,
            from_zone=from_zone,
            to_zone=to_zone,
            transit_multiplier=transit_multiplier,
        )
        if _fold(from_venue) == _fold(to_venue) and self.registry.canonical(from_venue) is None:
            # Identical unregistered venues: keep the caller's text as the label.
            source = target = " ".join(from_venue.split())
        else:
            source = self.registry.canonical(from_venue) or " ".join(from_venue.split())
            target = self.registry.canonical(to_venue) or " ".join(to_venue.split())
        # Floor so a partial-minute shortfall still counts against feasibility.
        gap_minutes = math.floor((session_b_start - session_a_end).total_seconds() / 60)
        required_with_lead = transit_needed + reserved_seat_lead_minutes
        margin = gap_minutes - required_with_lead

        return FeasibilityResult(
            is_feasible=margin >= 0,
            transit_needed=transit_needed,
            available_gap=gap_minutes,
            deficit=max(0, -margin),
            from_venue=source,
            to_venue=target,
            margin_minutes=margin,
            estimate_quality=quality,
            tight=margin >= 0 and margin < 10,
        )

    def normalize_venue(self, raw: str) -> str:
        """Map a venue alias or free-text location (e.g. "Venetian | Level 2") to its
        canonical name.

        Raises:
            UnknownVenueError: If no registered venue is recognized
        """
        if not (raw or "").strip():
            raise UnknownVenueError("Empty venue name")
        name = self.registry.canonical(raw)
        if name is None:
            raise UnknownVenueError(f"Unknown venue: {raw}")
        return name

    def list_venues(self) -> List[str]:
        """Return all canonical venue names.

        Returns:
            List of venue names
        """
        return list(self.registry.venues)
