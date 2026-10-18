"""Wire-to-domain translation (AWS Events API OpenAPI shapes)."""

import json
from datetime import datetime
from pathlib import Path

import pytest

from reinvent_concierge.api.mapping import (
    UNKNOWN_VENUE,
    build_personal_time_payload,
    bulk_result_from_api,
    parse_level,
    personal_time_from_api,
    repeat_group_for,
    session_from_api,
)
from reinvent_concierge.transit import TransitEngine
from tests.factories import UTC, wire_session

FIXTURES = Path(__file__).parent.parent / "fixtures" / "public_catalog"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("300 - Advanced", 300),
        ("Level 100", 100),
        ("400", 400),
        ("Expert", None),
        (None, None),
    ],
)
def test_parse_level(raw, expected):
    assert parse_level(raw) == expected


_REQUIRED_WIRE_FIELDS = {"sessionId", "title", "sessionTime"}
_OPTIONAL_WIRE_FIELDS = sorted(set(wire_session()) - _REQUIRED_WIRE_FIELDS)
_OPTIONAL_TIME_FIELDS = sorted(set(wire_session()["sessionTime"]) - {"date"})


@pytest.mark.parametrize("field", _OPTIONAL_WIRE_FIELDS)
def test_omitting_any_optional_wire_field_never_raises(field):
    raw = wire_session()
    del raw[field]

    session = session_from_api(raw, "America/Los_Angeles")

    assert session.session_id == "SEC301"


@pytest.mark.parametrize("field", _OPTIONAL_TIME_FIELDS)
def test_omitting_any_optional_session_time_field_never_raises(field):
    raw = wire_session()
    del raw["sessionTime"][field]

    session = session_from_api(raw, "America/Los_Angeles")

    assert session.session_id == "SEC301"


def test_session_times_are_converted_from_event_local_time_to_utc():
    session = session_from_api(wire_session(time="10:00", length="45"), "America/Los_Angeles")

    assert session.start_time == datetime(2026, 12, 1, 18, 0, tzinfo=UTC)
    assert session.end_time == datetime(2026, 12, 1, 18, 45, tzinfo=UTC)


def test_session_timezone_overrides_default():
    payload = wire_session(
        sessionTime={"date": "2026-12-01", "time": "10:00", "length": "60", "timezone": "UTC"}
    )

    assert session_from_api(payload, "America/Los_Angeles").start_time.hour == 10


def test_session_fields_are_mapped():
    payload = wire_session(
        seatAvailability="unavailable",
        speakers=[{"name": "A"}, {"name": "B"}],
        tracks=["AI/ML", "Security"],
        features=["AWS Hero"],
    )

    session = session_from_api(payload, "UTC")

    assert session.level == 300
    assert session.description == "An abstract"
    assert session.status == "FULL"
    assert session.has_open_seat is False
    assert session.speaker_names == "A, B"
    assert session.track == "AI/ML, Security"
    assert session.is_hero_speaker is True


def test_missing_venue_and_unscheduled_sessions():
    no_venue = wire_session()
    del no_venue["venue"]
    del no_venue["room"]
    assert session_from_api(no_venue, "UTC").venue_id == UNKNOWN_VENUE

    with pytest.raises(ValueError):
        session_from_api(wire_session(sessionTime={}), "UTC")


def test_personal_time_round_trip():
    block = personal_time_from_api(
        {
            "personalTimeId": "pt-1",
            "title": "Transit",
            "description": "walk",
            "startDateTime": "2026-12-01T19:00:00",
            "endDateTime": "2026-12-01T19:35:00",
        }
    )

    assert block.start_time == datetime(2026, 12, 1, 19, 0, tzinfo=UTC)
    assert block.end_time.minute == 35


def test_personal_time_payload_uses_utc_without_offset():
    payload = build_personal_time_payload(
        "Transit",
        datetime.fromisoformat("2026-12-01T11:00:00-08:00"),
        datetime.fromisoformat("2026-12-01T11:35:00-08:00"),
        "walk",
    )

    assert payload == {
        "title": "Transit",
        "description": "walk",
        "startDateTime": "2026-12-01T19:00:00",
        "endDateTime": "2026-12-01T19:35:00",
    }


@pytest.mark.parametrize(
    "start, end, description",
    [
        (datetime(2026, 12, 1, 19, 0, 30), datetime(2026, 12, 1, 19, 30), "walk"),  # seconds
        (datetime(2026, 12, 1, 19, 0), datetime(2026, 12, 1, 19, 7), "walk"),  # not 5-min
        (datetime(2026, 12, 1, 19, 0), datetime(2026, 12, 1, 19, 0), "walk"),  # empty
        (datetime(2026, 12, 1, 19, 0), datetime(2026, 12, 1, 19, 30), ""),  # description
        (datetime(2026, 12, 1, 19, 0), datetime(2026, 12, 1, 19, 30), "x" * 251),
    ],
)
def test_personal_time_payload_rejects_contract_violations(start, end, description):
    with pytest.raises(ValueError):
        build_personal_time_payload("Transit", start, end, description)


def test_bulk_result():
    result = bulk_result_from_api(
        {
            "result": {
                "successful": ["A"],
                "failed": [{"sessionId": "B", "code": "scheduleConflict", "conflictsWith": ["C"]}],
            }
        }
    )

    assert result.successful == ["A"]
    assert result.failed[0].conflicts_with == ["C"]


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda p: p.stem)
def test_recorded_public_sessions_map_with_known_venue(path):
    data = json.loads(path.read_text())
    assert data["items"]
    for item in data["items"]:
        assert item.get("room"), item["sessionId"]
        session = session_from_api(item, "UTC")
        assert session.venue_source in {"REGISTRY", "ROOM_PARSED"}, item["room"]
        assert session.venue_id != "Unknown" or session.zone, item["room"]


@pytest.mark.parametrize("name", ["Summit-NYC-2026", "Summit-DC-2026"])
def test_recorded_single_building_sessions_have_transit_between_every_pair(name):
    items = json.loads((FIXTURES / f"{name}.json").read_text())["items"]
    sessions = [session_from_api(item, "UTC") for item in items]
    engine = TransitEngine()
    for a in sessions:
        for b in sessions:
            assert engine.get_transit_minutes(a.venue_id, b.venue_id) >= 0, (
                a.room_name,
                b.room_name,
            )


def test_venue_from_room_when_venue_missing():
    payload = wire_session(venue=None, room="Venetian | Level 2 | Lando 4301")
    payload.pop("venue")
    session = session_from_api(payload, "America/Los_Angeles")
    assert session.venue_id == "Venetian"
    assert session.venue_source == "REGISTRY"
    assert session.zone is None
    assert session.floor == 2
    assert session.room_name == "Venetian | Level 2 | Lando 4301"


def test_unregistered_room_first_segment_is_zone():
    payload = wire_session(room="Excel London, Maritime Suites, Level 3, Albert 3")
    payload.pop("venue")
    session = session_from_api(payload, "Europe/London")
    assert session.venue_id == "Unknown"
    assert session.venue_source == "ROOM_PARSED"
    assert session.zone == "Excel London"
    assert session.floor == 3


def test_api_venue_alias_is_canonicalized():
    session = session_from_api(wire_session(venue="Palazzo"), "America/Los_Angeles")
    assert session.venue_id == "Venetian"
    assert session.venue_source == "REGISTRY"


def test_taxonomy_fields_are_mapped():
    payload = wire_session(
        abbreviation="AIM301-R1",
        type="Workshop",
        features=["Hands-on"],
        topics=["Artificial Intelligence"],
        roles=["Developer / Engineer"],
        services=["Amazon Bedrock"],
    )
    session = session_from_api(payload, "America/Los_Angeles")
    assert session.session_code == "AIM301-R1"
    assert session.session_type == "Workshop"
    assert session.features == ["Hands-on"]
    assert session.topics == ["Artificial Intelligence"]
    assert session.roles == ["Developer / Engineer"]
    assert session.services == ["Amazon Bedrock"]
    assert session.repeat_group == "AIM301"


@pytest.mark.parametrize(
    "code, group",
    [
        ("AIM301", "AIM301"),
        ("AIM301-R", "AIM301"),
        ("aim301-r2", "AIM301"),
        (None, None),
        ("", None),
    ],
)
def test_repeat_group_strips_repeat_suffix(code, group):
    assert repeat_group_for(code) == group


@pytest.mark.parametrize(
    "value, expected",
    [
        ("Hands-on", ["Hands-on"]),
        (["A", " ", "B"], ["A", "B"]),
        (None, []),
        (7, []),
        ({"a": 1}, []),
    ],
)
def test_taxonomy_fields_tolerate_non_list_values(value, expected):
    session = session_from_api(wire_session(topics=value), "UTC")
    assert session.topics == expected


def test_repeat_group_falls_back_to_session_id():
    payload = wire_session(session_id="AIM301-R1")
    payload.pop("abbreviation", None)
    assert session_from_api(payload, "UTC").repeat_group == "AIM301"


def test_missing_optional_fields_never_raise():
    minimal = {"sessionId": "X1", "title": "T", "sessionTime": {"date": "2026-12-01"}}
    session = session_from_api(minimal, "UTC")
    assert session.venue_id == "Unknown" and session.venue_source == "UNKNOWN"
    assert session.zone is None
    assert session.topics == [] and session.repeat_group == "X1"
