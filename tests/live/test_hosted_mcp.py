"""Tier 1b - hosted AWS Events MCP server: tool inventory and parity with the REST API,
using the same Builder ID token. Read-only.
"""

from typing import Any

import pytest

from reinvent_concierge.api.hosted_mcp import EXPECTED_TOOLS, HostedMCPClient
from reinvent_concierge.api.mapping import session_from_api
from tests.live.conftest import LIVE

pytestmark = pytest.mark.live


def unwrap(payload: Any, key: str) -> Any:
    return payload.get(key, payload) if isinstance(payload, dict) else payload


def items(payload: Any) -> list:
    if isinstance(payload, list):
        return payload
    return payload.get("items", []) if isinstance(payload, dict) else []


@pytest.fixture
async def hosted(live_credentials, live_config, registration):
    async with HostedMCPClient(
        live_credentials.get_token(), live_config.aws_events_hosted_mcp_url
    ) as mcp:
        yield mcp


async def test_rejects_an_invalid_token(anonymous_config):
    with pytest.raises(Exception):  # noqa: B017 - transport-specific auth error
        async with HostedMCPClient("not-a-valid-token", anonymous_config.aws_events_hosted_mcp_url):
            pass


async def test_exposes_every_rest_operation(hosted):
    names = set(await hosted.list_tool_names())

    assert EXPECTED_TOOLS <= names, f"missing tools: {sorted(EXPECTED_TOOLS - names)}"


async def test_list_events_matches_rest(hosted, anonymous_client):
    via_mcp = {e["eventId"] for e in items(await hosted.call("ListEvents"))}
    via_rest = {e.event_id for e in await anonymous_client.list_events()}

    assert via_mcp == via_rest


async def test_get_session_matches_rest(hosted, live_config, catalog):
    sample = catalog[: min(3, len(catalog))]

    for session in sample:
        payload = await hosted.call(
            "GetSession", {"eventId": LIVE.event_id, "sessionId": session.session_id}
        )
        mapped = session_from_api(unwrap(payload, "session"), live_config.event_timezone)
        assert mapped == session


async def test_list_sessions_page_matches_rest(hosted, catalog):
    payload = await hosted.call("ListSessions", {"eventId": LIVE.event_id})

    page_ids = {item["sessionId"] for item in items(payload)}
    assert page_ids, "first MCP page is empty"
    assert page_ids <= {s.session_id for s in catalog} | _unscheduled(payload)
    assert payload.get("totalCount") >= len(catalog)


def _unscheduled(payload: Any) -> set:
    return {i["sessionId"] for i in items(payload) if not (i.get("sessionTime") or {}).get("date")}


async def test_get_schedule_matches_rest(hosted, client):
    via_rest = await client.get_raw_schedule()
    schedule = unwrap(await hosted.call("GetSchedule", {"eventId": LIVE.event_id}), "schedule")

    assert schedule["reserved"] == via_rest["reserved"]
    assert schedule["favorites"] == via_rest["favorites"]
    assert len(schedule["personalTime"]) == len(via_rest["personalTime"])
