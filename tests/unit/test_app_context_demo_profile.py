"""SPEC-21 AC1: the DEMO profile can never construct a credential or an authenticated client."""

import re

import respx

from reinvent_concierge.api import AWSEventsRestClient
from reinvent_concierge.app_context import create_app_context
from reinvent_concierge.credentials import CredentialManager
from reinvent_concierge.demo import DemoCatalogClient, SimulatedScheduleClient
from tests.factories import API_BASE, EVENT_ID, FakeEmbedder, wire_session

_ALLOWED = re.compile(rf"^{re.escape(API_BASE)}/events(/[^/]+)?(/sessions(/[^/]+)?)?$")
_EVENT_JSON = {
    "eventId": EVENT_ID,
    "name": "AWS re:Invent 2026",
    "eventType": "re:Invent",
    "startDate": "2026-11-30T05:30:00.000-08:00",
    "endDate": "2026-12-04T23:59:00.000-08:00",
    "isOnline": False,
}


def _object_graph(obj, seen=None):
    """Every object reachable from ``obj`` through its own ``__dict__``, for a blast-radius scan."""
    seen = seen if seen is not None else set()
    if id(obj) in seen or not hasattr(obj, "__dict__"):
        return
    seen.add(id(obj))
    yield obj
    for value in vars(obj).values():
        yield from _object_graph(value, seen)


async def test_demo_profile_constructs_no_credential_manager_or_live_client(config):
    context = await create_app_context(config, profile="DEMO")
    try:
        graph = list(_object_graph(context))

        assert not any(isinstance(obj, CredentialManager) for obj in graph)
        assert not any(isinstance(obj, AWSEventsRestClient) for obj in graph)
        assert context.credentials is None
        assert isinstance(context.client, SimulatedScheduleClient)
        assert isinstance(context.catalog_client, DemoCatalogClient)
    finally:
        await context.aclose()


async def test_live_profile_is_unchanged_by_default(config, credentials):
    context = await create_app_context(config)
    try:
        assert isinstance(context.credentials, CredentialManager)
        assert isinstance(context.client, AWSEventsRestClient)
        assert context.catalog_client is context.client
    finally:
        await context.aclose()


async def test_demo_profile_search_and_schedule_work_without_any_network_mock(config):
    """No respx mock is set up: a network call here would fail the test, proving DEMO never
    reaches out even for ordinary startup."""
    context = await create_app_context(config, profile="DEMO")
    try:
        schedule = await context.reader.read()
        assert schedule.booked == []
    finally:
        await context.aclose()


@respx.mock
async def test_demo_sync_never_sends_a_non_allowlisted_request_or_an_auth_header(config):
    """AC1's network spy: every request a DEMO catalog sync makes matches the allowlist, and
    none ever carries an Authorization header."""
    respx.get(f"{API_BASE}/events/{EVENT_ID}").respond(200, json={"event": _EVENT_JSON})
    respx.get(f"{API_BASE}/events/{EVENT_ID}/sessions").respond(
        200, json={"items": [wire_session("S1")], "nextToken": None}
    )

    context = await create_app_context(config, embedder=FakeEmbedder(), profile="DEMO")
    try:
        synced = await context.ingestion.initial_sync()
        assert synced == 1

        for call in respx.calls:
            url = str(call.request.url).split("?", 1)[0]
            assert _ALLOWED.match(url), f"non-allowlisted request: {url}"
            assert "authorization" not in {h.lower() for h in call.request.headers}
    finally:
        await context.aclose()
