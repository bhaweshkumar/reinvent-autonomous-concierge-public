"""SPEC-21 §21.1/§21.2/AC1: DemoCatalogClient is GET-only, anonymous, hard-allowlisted."""

import pytest
import respx

from reinvent_concierge.demo import DemoCatalogAllowlistError, DemoCatalogClient
from tests.factories import API_BASE, EVENT_ID, wire_session


@pytest.fixture
def client(config):
    return DemoCatalogClient(config)


EVENT_JSON = {
    "eventId": EVENT_ID,
    "name": "re:Invent 2026",
    "eventType": "re:Invent",
    "startDate": "2026-11-30T05:30:00.000-08:00",
    "endDate": "2026-12-04T23:59:00.000-08:00",
    "isOnline": False,
}


@respx.mock
async def test_get_event_hits_the_allowlisted_path(client):
    respx.get(f"{API_BASE}/events/{EVENT_ID}").respond(200, json={"event": EVENT_JSON})

    event = await client.get_event()

    assert event.event_id == EVENT_ID


@respx.mock
async def test_list_sessions_paginates_the_allowlisted_path(client):
    respx.get(f"{API_BASE}/events/{EVENT_ID}/sessions").respond(
        200, json={"items": [wire_session("S1")], "nextToken": None}
    )

    sessions = [s async for s in client.list_sessions()]

    assert [s.session_id for s in sessions] == ["S1"]


@respx.mock
async def test_get_session_hits_the_allowlisted_path(client):
    respx.get(f"{API_BASE}/events/{EVENT_ID}/sessions/S1").respond(
        200, json={"session": wire_session("S1")}
    )

    session = await client.get_session("S1")

    assert session.session_id == "S1"


@pytest.mark.parametrize(
    "path",
    [
        "/events/reinvent2026/schedule",
        "/events/reinvent2026/reservations",
        "/events/reinvent2026/favorites",
        "/events/reinvent2026/personal-time",
        "/login",
        "/events/reinvent2026/sessions/S1/extra",
    ],
)
async def test_non_allowlisted_paths_raise_before_any_network_call(client, path):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{API_BASE}{path}").respond(200, json={})

        with pytest.raises(DemoCatalogAllowlistError):
            await client._get(path)

        assert not mock.calls, "the network must never be reached for a non-allowlisted path"


@respx.mock
async def test_no_request_ever_carries_an_authorization_header(client):
    respx.get(f"{API_BASE}/events/{EVENT_ID}").respond(200, json={"event": EVENT_JSON})

    await client.get_event()

    [call] = respx.calls
    assert "authorization" not in {h.lower() for h in call.request.headers}


def test_client_has_no_field_that_could_hold_a_credential(client):
    # Defense in depth: even a future bug can't attach a token that was never stored.
    forbidden = ("token", "credential", "auth")
    for name in vars(client):
        assert not any(word in name.lower() for word in forbidden), name


@respx.mock
async def test_malformed_session_items_are_skipped_not_raised(client):
    respx.get(f"{API_BASE}/events/{EVENT_ID}/sessions").respond(
        200,
        json={
            "items": [{"sessionId": "BAD"}, wire_session("S1")],  # BAD is missing required fields
            "nextToken": None,
        },
    )

    sessions = [s async for s in client.list_sessions()]

    assert [s.session_id for s in sessions] == ["S1"]
