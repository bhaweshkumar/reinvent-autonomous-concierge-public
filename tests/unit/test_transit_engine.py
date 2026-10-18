"""Unit tests for TransitEngine (SPEC-04)."""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from reinvent_concierge.exceptions import UnknownVenueError
from reinvent_concierge.transit import TransitEngine


class TestTransitEngine:
    """Test suite for TransitEngine."""

    @pytest.fixture
    def engine(self):
        """Create TransitEngine instance."""
        return TransitEngine()

    def test_initialization(self, engine):
        """Test that TransitEngine initializes properly."""
        assert engine is not None
        assert engine.graph is not None
        assert len(engine.graph.nodes) == 6
        # Should have edges between all venue pairs (bidirectional)
        assert len(engine.graph.edges) >= 15  # 6 venues, fully connected

    def test_list_venues(self, engine):
        """Test list_venues returns all canonical venue names."""
        venues = engine.list_venues()
        assert len(venues) == 6
        assert set(venues) == {
            "Venetian",
            "Wynn",
            "Caesars Forum",
            "Caesars Palace",
            "Mandalay Bay",
            "MGM Grand",
        }

    # Acceptance Criteria Tests
    def test_get_transit_minutes_mandalay_to_venetian(self, engine):
        """AC: get_transit_minutes('Mandalay Bay', 'Venetian') == 35."""
        transit = engine.get_transit_minutes("Mandalay Bay", "Venetian")
        assert transit == 35

    def test_get_transit_minutes_same_venue(self, engine):
        """AC: get_transit_minutes('Venetian', 'Venetian') == 0."""
        transit = engine.get_transit_minutes("Venetian", "Venetian")
        assert transit == 0

    def test_get_transit_minutes_bidirectional(self, engine):
        """Test that transit times are bidirectional."""
        # Mandalay to Venetian should equal Venetian to Mandalay
        forward = engine.get_transit_minutes("Mandalay Bay", "Venetian")
        backward = engine.get_transit_minutes("Venetian", "Mandalay Bay")
        assert forward == backward == 35

    def test_get_transit_minutes_all_pairs(self, engine):
        """Test all venue pairs match the distance matrix."""
        expected_times = {
            ("Venetian", "Wynn"): 10,
            ("Venetian", "Caesars Forum"): 15,
            ("Venetian", "Mandalay Bay"): 35,
            ("Venetian", "MGM Grand"): 25,
            ("Wynn", "Caesars Forum"): 20,
            ("Wynn", "Mandalay Bay"): 40,
            ("Wynn", "MGM Grand"): 30,
            ("Caesars Forum", "Mandalay Bay"): 35,
            ("Caesars Forum", "MGM Grand"): 25,
            ("Mandalay Bay", "MGM Grand"): 20,
        }

        for (from_venue, to_venue), expected in expected_times.items():
            actual = engine.get_transit_minutes(from_venue, to_venue)
            assert actual == expected, f"{from_venue} -> {to_venue}: {actual} != {expected}"

    def test_check_feasibility_not_feasible(self, engine):
        """Mandalay Bay 13:30 -> Venetian 14:00 is infeasible (35 min matrix + 10 min walk)."""
        end_time = datetime(2024, 12, 2, 13, 30)
        start_time = datetime(2024, 12, 2, 14, 0)
        result = engine.check_feasibility(end_time, start_time, "Mandalay Bay", "Venetian")
        assert result.is_feasible is False
        assert result.transit_needed == 45
        assert result.available_gap == 30
        assert result.deficit == 15

    def test_check_feasibility_feasible(self, engine):
        """Mandalay Bay 13:30 -> Venetian 14:30 is feasible (35 min matrix + 10 min walk)."""
        end_time = datetime(2024, 12, 2, 13, 30)
        start_time = datetime(2024, 12, 2, 14, 30)
        result = engine.check_feasibility(end_time, start_time, "Mandalay Bay", "Venetian")
        assert result.is_feasible is True
        assert result.transit_needed == 45
        assert result.available_gap == 60
        assert result.deficit == 0

    def test_normalize_venue_lowercase(self, engine):
        """AC: normalize_venue('THE VENETIAN') -> 'Venetian'."""
        assert engine.normalize_venue("THE VENETIAN") == "Venetian"
        assert engine.normalize_venue("the venetian") == "Venetian"
        assert engine.normalize_venue("venetian") == "Venetian"

    def test_normalize_venue_aliases(self, engine):
        """Test normalize_venue with various aliases."""
        test_cases = {
            "the venetian": "Venetian",
            "palazzo": "Venetian",
            "wynn las vegas": "Wynn",
            "wynn": "Wynn",
            "caesars forum": "Caesars Forum",
            "mgm": "MGM Grand",
        }
        for alias, expected in test_cases.items():
            actual = engine.normalize_venue(alias)
            assert actual == expected

    def test_normalize_venue_unknown_raises_error(self, engine):
        """Test that unknown venue raises UnknownVenueError."""
        with pytest.raises(UnknownVenueError):
            engine.normalize_venue("Bellagio")

    def test_get_transit_minutes_with_aliases(self, engine):
        """Test get_transit_minutes works with venue aliases."""
        transit1 = engine.get_transit_minutes("the venetian", "mgm")
        transit2 = engine.get_transit_minutes("Venetian", "MGM Grand")
        assert transit1 == transit2 == 25

    def test_networkx_graph_structure(self, engine):
        """Test that the NetworkX graph is properly structured."""
        assert not engine.graph.is_directed()
        nodes = set(engine.graph.nodes())
        assert nodes == {
            "Venetian",
            "Wynn",
            "Caesars Forum",
            "Caesars Palace",
            "Mandalay Bay",
            "MGM Grand",
        }
        assert engine.graph["Venetian"]["Wynn"]["weight"] == 10

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("Venetian | Level 2 | Lando 4201", "Venetian"),
            ("Mandalay Bay | South Convention Center", "Mandalay Bay"),
            ("Caesars Forum", "Caesars Forum"),
            ("Encore", "Wynn"),
            ("  mgm   GRAND ", "MGM Grand"),
        ],
    )
    def test_normalize_venue_free_text(self, engine, raw, expected):
        assert engine.normalize_venue(raw) == expected

    def test_alias_must_match_whole_words(self, engine):
        with pytest.raises(UnknownVenueError):
            engine.normalize_venue("Convention Center")

    @pytest.mark.parametrize("gap_minutes, feasible, deficit", [(18, False, 1), (19, True, 0)])
    def test_check_feasibility_edges(self, engine, gap_minutes, feasible, deficit):
        # Venetian <-> Wynn: 10 min matrix + 5 + 4 min inside_walk = 19 min required.
        end = datetime(2026, 12, 1, 13, 0)
        start = end + timedelta(minutes=gap_minutes)

        result = engine.check_feasibility(end, start, "Venetian", "Wynn")

        assert (result.is_feasible, result.deficit) == (feasible, deficit)

    def test_check_feasibility_same_venue_back_to_back(self, engine):
        end = datetime(2026, 12, 1, 13, 0)

        result = engine.check_feasibility(end, end, "Venetian", "the venetian")

        assert result.is_feasible and result.transit_needed == 0


def test_caesars_palace_is_a_separate_venue():
    engine = TransitEngine()
    assert engine.get_transit_minutes("Caesars Palace", "Caesars Forum") == 15


def test_unregistered_identical_venues_need_no_transit():
    engine = TransitEngine()
    assert engine.get_transit_minutes("Excel London", "Excel London") == 0


def test_check_feasibility_identical_unregistered_venues_is_feasible():
    engine = TransitEngine()
    end = datetime(2026, 12, 1, 13, 0)

    result = engine.check_feasibility(end, end, "Excel London", "excel  london")

    assert result.is_feasible and result.transit_needed == 0


@pytest.mark.parametrize(
    "other", ["Venetian", "Wynn", "Caesars Forum", "Mandalay Bay", "MGM Grand"]
)
def test_caesars_palace_has_transit_time_to_every_other_venue(other):
    engine = TransitEngine()
    assert engine.get_transit_minutes("Caesars Palace", other) > 0
    assert engine.get_transit_minutes(other, "Caesars Palace") > 0


class TestSpec19RequiredMinutes:
    """SPEC-19 §19.1: required_minutes and its acceptance criteria."""

    @pytest.fixture
    def engine(self):
        return TransitEngine()

    def test_two_rooms_on_different_floors_of_one_venue_need_nonzero_minutes(self, engine):
        """AC2: two rooms on different floors of one venue produce a nonzero transit time."""
        minutes, quality = engine.required_minutes("Venetian", "Venetian", from_floor=1, to_floor=3)
        assert minutes > 0
        assert quality == "NORMAL"

    def test_same_floor_same_venue_needs_no_minutes(self, engine):
        minutes, _quality = engine.required_minutes(
            "Venetian", "Venetian", from_floor=2, to_floor=2
        )
        assert minutes == 0

    def test_unknown_venue_pair_never_raises_and_is_a_low_confidence_estimate(self, engine):
        """AC4: unknown-venue pairs never cause a hard rejection."""
        minutes, quality = engine.required_minutes("Spaceport Alpha", "Moon Base")

        assert minutes == 0  # no registry data on either side to estimate from
        assert quality == "LOW"

    def test_one_known_one_unknown_venue_still_estimates_without_raising(self, engine):
        minutes, quality = engine.required_minutes("Spaceport Alpha", "Venetian")

        assert minutes == engine.registry.inside_walk("Venetian")
        assert quality == "LOW"

    def test_check_feasibility_unknown_venue_is_not_forced_infeasible(self, engine):
        """AC4: a generous gap between two unrecognized venues is feasible, not rejected."""
        end = datetime(2026, 12, 1, 13, 0)
        start = end + timedelta(hours=2)

        result = engine.check_feasibility(end, start, "Spaceport Alpha", "Moon Base")

        assert result.is_feasible
        assert result.estimate_quality == "LOW"

    def test_reservable_session_with_lead_time_is_tight_not_infeasible(self, engine):
        """AC3: an 8-minute gap in the same room, reservable, is tight when lead + walk <= 8."""
        end = datetime(2026, 12, 1, 13, 0)
        start = end + timedelta(minutes=8)

        result = engine.check_feasibility(
            end,
            start,
            "Venetian",
            "Venetian",
            reserved_seat_lead_minutes=5,
        )

        assert result.is_feasible
        assert result.tight
        assert result.margin_minutes == 3

    def test_feasible_with_ample_margin_is_not_tight(self, engine):
        end = datetime(2026, 12, 1, 13, 0)
        start = end + timedelta(minutes=30)

        result = engine.check_feasibility(end, start, "Venetian", "Venetian")

        assert result.is_feasible
        assert not result.tight

    def test_transit_multiplier_scales_required_minutes(self, engine):
        minutes_1x, _ = engine.required_minutes("Mandalay Bay", "Venetian")
        minutes_2x, _ = engine.required_minutes(
            "Mandalay Bay", "Venetian", transit_multiplier=Decimal("2.0")
        )
        assert minutes_2x == minutes_1x * 2
