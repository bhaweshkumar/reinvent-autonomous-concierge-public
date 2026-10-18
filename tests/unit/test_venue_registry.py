import json
from importlib import resources

import pytest

from reinvent_concierge.venues import VenueRegistry, load_registry


def test_registry_matrix_is_complete_symmetric_and_sourced():
    registry = load_registry()
    names = registry.venues
    data = json.loads(
        resources.files("reinvent_concierge.data").joinpath("venues.json").read_text()
    )
    assert all(entry["source"] in {"estimate", "published", "measured"} for entry in data["matrix"])
    for a in names:
        for b in names:
            if a != b:
                assert registry.minutes(a, b) is not None
            expected = 0 if a == b else registry.minutes(b, a)
            assert registry.minutes(a, b) == expected


@pytest.mark.parametrize(
    "text, canonical",
    [
        ("Palazzo", "Venetian"),
        ("Venetian | Level 2 | Lando 4301", "Venetian"),
        ("Caesars Forum, Level 1, Alliance 305", "Caesars Forum"),
        ("Caesars Palace, Promenade", "Caesars Palace"),
        ("Encore", "Wynn"),
        ("MGM Grand", "MGM Grand"),
        ("Mandalay Bay South", "Mandalay Bay"),
        ("Caesar's Palace", "Caesars Palace"),
        ("Caesars Forum | Caesars Palace", "Caesars Forum"),
        ("Convention Center | MGM Grand Garden", "MGM Grand"),
        ("Excel London, Palazzo Ballroom", "Venetian"),
    ],
)
def test_resolve_registered_aliases_in_free_text(text, canonical):
    match = load_registry().resolve(text)
    assert match.canonical == canonical
    assert match.source == "REGISTRY"


def test_resolve_unregistered_room_uses_first_segment():
    match = load_registry().resolve("Excel London, Maritime Suites, Level 3, Albert 3")
    assert match.canonical is None
    assert match.raw == "Excel London"
    assert match.source == "ROOM_PARSED"


@pytest.mark.parametrize("text", [None, "", "   "])
def test_resolve_empty_is_unknown(text):
    match = load_registry().resolve(text)
    assert match.canonical is None and match.source == "UNKNOWN"


def test_resolve_short_ambiguous_word_is_not_a_venue():
    match = load_registry().resolve("Ven Diagram Room")
    assert match.canonical is None
    assert match.source == "ROOM_PARSED"
    assert match.raw == "Ven Diagram Room"


_VENUES = [{"name": "A", "aliases": []}, {"name": "B", "aliases": []}]


def test_registry_rejects_matrix_entry_for_unknown_venue():
    data = {"venues": _VENUES, "matrix": [{"a": "A", "b": "Z", "minutes": 5, "source": "estimate"}]}
    with pytest.raises(ValueError):
        VenueRegistry(data)


def test_registry_rejects_unknown_source():
    data = {"venues": _VENUES, "matrix": [{"a": "A", "b": "B", "minutes": 5, "source": "guess"}]}
    with pytest.raises(ValueError):
        VenueRegistry(data)


class TestSpec19RegistryTerms:
    """SPEC-19 §19.1/§16.3: inside_walk, floor_minutes, zone_penalties, every one sourced."""

    def test_packaged_registry_carries_a_source_for_every_new_term(self):
        data = json.loads(
            resources.files("reinvent_concierge.data").joinpath("venues.json").read_text()
        )
        for section in ("inside_walk", "floor_minutes"):
            assert data[section], f"{section} is empty"
            assert all(
                entry["source"] in {"estimate", "published", "measured"}
                for entry in data[section].values()
            )
        for venue, zones in data["zone_penalties"].items():
            assert zones, f"zone_penalties for {venue} is empty"
            assert all(
                entry["source"] in {"estimate", "published", "measured"} for entry in zones.values()
            )

    def test_inside_walk_defaults_to_zero_for_an_unregistered_venue(self):
        registry = load_registry()
        assert registry.inside_walk("Nonexistent Hall") == 0

    def test_floor_minutes_defaults_to_zero_for_an_unregistered_venue(self):
        registry = load_registry()
        assert registry.floor_minutes("Nonexistent Hall") == 0

    def test_zone_penalty_round_trips_both_directions(self):
        data = {
            "venues": _VENUES,
            "matrix": [],
            "zone_penalties": {"A": {"Expo|Main": {"minutes": 8, "source": "estimate"}}},
        }
        registry = VenueRegistry(data)
        assert registry.zone_penalty("A", "Expo", "Main") == 8
        assert registry.zone_penalty("A", "Main", "Expo") == 8

    def test_zone_penalty_is_zero_when_zones_match_or_are_missing(self):
        registry = load_registry()
        assert registry.zone_penalty("Caesars Forum", "Expo", "Expo") == 0
        assert registry.zone_penalty("Caesars Forum", None, "Main") == 0
        assert registry.zone_penalty("Caesars Forum", "Expo", None) == 0

    def test_registry_rejects_inside_walk_for_unknown_venue(self):
        data = {
            "venues": _VENUES,
            "matrix": [],
            "inside_walk": {"Z": {"minutes": 5, "source": "estimate"}},
        }
        with pytest.raises(ValueError):
            VenueRegistry(data)

    def test_registry_rejects_invalid_zone_penalty_source(self):
        data = {
            "venues": _VENUES,
            "matrix": [],
            "zone_penalties": {"A": {"Expo|Main": {"minutes": 8, "source": "guess"}}},
        }
        with pytest.raises(ValueError):
            VenueRegistry(data)
