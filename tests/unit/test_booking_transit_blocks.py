import pytest

from reinvent_concierge.booking.transit_blocks import (
    TRANSIT_MARKER,
    is_concierge_transit,
    same_route,
    transit_description,
    transit_title,
)
from reinvent_concierge.models import PersonalTimeBlock
from tests.factories import at


def block(title, description):
    return PersonalTimeBlock(
        id="p1", title=title, description=description, start_time=at(10), end_time=at(11)
    )


def test_title_uses_arrow():
    assert transit_title("Venetian", "Wynn") == "Transit: Venetian → Wynn"


def test_description_is_marked_and_bounded():
    text = transit_description("ref", "x" * 400)
    assert text.startswith(f"{TRANSIT_MARKER}ref:") and len(text) == 250


@pytest.mark.parametrize(
    "title, description, owned",
    [
        ("Transit: Venetian → Wynn", f"{TRANSIT_MARKER}plan:abc", True),
        ("Transit: Venetian to Wynn", f"{TRANSIT_MARKER}plan:abc", True),
        ("Transit: Venetian → Wynn", "Travel 10 min from Venetian to Wynn for X", True),
        ("Transit: Venetian → Wynn", "Travel 10 min from Venetian to Wynn for AIM301", True),
        ("Transit: Uber to Airport", "Travel by Uber to the airport", False),
        ("Transit: Venetian → Wynn", "My own note", False),
        ("Lunch with team", f"Travel {10} min", False),
        ("Transit: Venetian → Wynn", None, False),
    ],
)
def test_ownership_recognizes_current_and_legacy_blocks(title, description, owned):
    assert is_concierge_transit(block(title, description)) is owned


def test_same_route_treats_arrow_and_to_alike():
    assert same_route("Transit: Venetian to Wynn", "Transit: Venetian → Wynn")
    assert not same_route("Transit: Venetian → Wynn", "Transit: Wynn → Venetian")
