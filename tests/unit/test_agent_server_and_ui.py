"""Tool schemas, the FastMCP server wiring and UI view models."""

from datetime import timedelta
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from pydantic import ValidationError

from reinvent_concierge.agent import (
    AddToWaitlistInput,
    ManageFavoritesInput,
    MCPAgent,
    OptimizeAndBookInput,
    OptimizeAndBookOutput,
    SearchCatalogInput,
)
from reinvent_concierge.agent.audit_policy import recent_outcomes
from reinvent_concierge.agent.server import build_server
from reinvent_concierge.audit import AuditLogger
from reinvent_concierge.models import SessionSummary
from reinvent_concierge.schedule import ConflictManager
from reinvent_concierge.transit import TransitEngine
from reinvent_concierge.ui.mcp_client import MCPClient
from reinvent_concierge.ui.view_models import agenda_blocks, seat_label
from tests.factories import at, make_schedule, make_session

TOOLS = {
    "search_catalog",
    "get_my_schedule",
    "optimize_and_book_session",
    "manage_favorites",
    "check_transit",
    "add_to_waitlist",
    "list_waitlist",
    "update_waitlist_entry",
    "remove_from_waitlist",
    "get_recent_outcomes",
    "cancel_reservation",
    "get_preference_profile",
    "propose_preference_update",
    "save_preference_profile",
    "reset_preference_profile",
    "generate_itinerary_plans",
    "get_itinerary_plan",
    "compare_itinerary_plans",
    "explain_plan_choice",
    "preview_itinerary_application",
    "apply_itinerary_plan",
    "get_apply_status",
    "resume_apply_recovery",
}


class TestToolSchemas:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"query": "x", "limit": 0},
            {"query": "x", "limit": 21},
            {"query": "x", "level": 250},
            {"query": "x", "level": 500},
            {"query": ""},
        ],
    )
    def test_search_input_rejects(self, kwargs):
        with pytest.raises(ValidationError):
            SearchCatalogInput(**kwargs)

    def test_search_input_defaults(self):
        params = SearchCatalogInput(query="bedrock")
        assert (params.limit, params.hero_only, params.level) == (5, False, None)

    def test_other_inputs(self):
        with pytest.raises(ValidationError):
            ManageFavoritesInput(action="delete", session_id="A")
        with pytest.raises(ValidationError):
            AddToWaitlistInput(session_id="A", priority=101)
        with pytest.raises(ValidationError):
            OptimizeAndBookInput()
        assert AddToWaitlistInput(session_id="A").priority == 50
        assert OptimizeAndBookInput(session_id="A").auto_resolve_conflicts is True
        assert OptimizeAndBookInput.model_fields["auto_resolve_conflicts"].description == (
            "Add transit buffers for venue changes; never cancels anything"
        )

    def test_booking_status_is_constrained(self):
        with pytest.raises(ValidationError):
            OptimizeAndBookOutput(status="MAYBE", session_title="t", message="m")


class TestServer:
    @pytest.fixture
    def server(self, config, credentials, waitlist):
        client = Mock()
        ingestion = Mock()
        ingestion.search = AsyncMock(
            return_value=[
                SessionSummary(
                    session_id="AI301",
                    title="Bedrock",
                    level=300,
                    venue_id="Venetian",
                    start_time=at(18),
                    end_time=at(19),
                )
            ]
        )
        transit = TransitEngine()
        agent = MCPAgent(
            config,
            client,
            ingestion,
            transit,
            ConflictManager(transit),
            AuditLogger(config.planner_db_path, credentials.hash_token),
            waitlist,
        )
        return build_server(agent)

    async def test_exposes_tools(self, server):
        async with Client(server) as client:
            names = {tool.name for tool in await client.list_tools()}

        assert names == TOOLS

    async def test_write_tools_take_optional_approval_fields(self, server):
        async with Client(server) as client:
            tools = {tool.name: tool.input_schema for tool in await client.list_tools()}

        book, cancel = tools["optimize_and_book_session"], tools["cancel_reservation"]
        assert set(book["properties"]) == {
            "session_id",
            "auto_resolve_conflicts",
            "apply_run_id",
            "approval_token",
        }
        assert set(cancel["properties"]) == {"session_id", "apply_run_id", "approval_token"}
        assert book["required"] == ["session_id"] and cancel["required"] == ["session_id"]
        assert book["properties"]["auto_resolve_conflicts"]["default"] is True

    async def test_call_returns_structured_result(self, server):
        async with Client(server) as client:
            result = await client.call_tool("search_catalog", {"query": "bedrock", "level": 300})

        assert result.structured_content["total_found"] == 1
        assert result.structured_content["sessions"][0]["session_id"] == "AI301"

    async def test_invalid_arguments_are_tool_errors(self, server):
        async with Client(server) as client:
            with pytest.raises(ToolError):
                await client.call_tool("search_catalog", {"query": "x", "limit": 99})

    def test_ui_client_drops_unset_arguments(self, server):
        # fastmcp's Client accepts a server instance in place of a URL.
        result = MCPClient(server).call("search_catalog", query="bedrock", level=None)

        assert result["sessions"][0]["session_id"] == "AI301"

    def test_ui_client_raises_tool_errors(self, server):
        with pytest.raises(ToolError):
            MCPClient(server).call("search_catalog", query="x", limit=99)


class TestViewModels:
    def test_seat_label(self):
        assert seat_label({"capacity": 100, "reserved_count": 97}) == "3 of 100 seats available"
        assert seat_label({"seat_availability": "veryLimited"}) == "Very limited seats"
        assert seat_label({}) == "Seat availability unknown"

    def test_agenda_blocks_are_local_and_grouped(self):
        session = make_session("A", "Venetian | Level 2", start=at(18))
        schedule = make_schedule(session).model_dump(mode="json")
        start = at(19)
        schedule["personal_time"] = [
            {
                "id": "pt",
                "title": "Transit: Venetian → Wynn",
                "start_time": start.isoformat(),
                "end_time": (start + timedelta(minutes=10)).isoformat(),
            }
        ]

        blocks = agenda_blocks(schedule, ZoneInfo("America/Los_Angeles"), TransitEngine())

        assert [(b["group"], b["start_hour"], b["day"]) for b in blocks] == [
            ("Venetian", 10.0, "2026-12-01 (Tue)"),
            ("Transit", 11.0, "2026-12-01 (Tue)"),
        ]

    def test_recent_outcomes(self, config, credentials):
        audit = AuditLogger(config.planner_db_path, credentials.hash_token)
        audit.record("RESERVE_SESSION", session_id="A", status="CONFIRMED")
        audit.record("search_catalog", status="OK")
        audit.record("RESERVE_SESSION", session_id="B", status="DEFERRED", details="409")
        audit.record("RESERVE_SESSION", session_id="C", status="FAILED")
        audit.record("SWAP_PROPOSED", session_id="D", status="PROPOSED", details="overlaps X")
        audit.record("TRANSIT_CONFLICT", session_id="A", status="MANUAL_REQUIRED")
        audit.record("TRANSIT_CONFLICT", session_id="E", status="SKIPPED")
        audit.record("THROTTLED", session_id="F", status="WATCHING")
        audit.record("WAITLIST_REMOVE", session_id="A", status="CONFIRMED")
        audit.record("RESERVE_SESSION", session_id="G", status="MANUAL_REQUIRED")

        outcomes = recent_outcomes(audit.recent_entries())

        assert [(o["session_id"], o["status"]) for o in outcomes] == [
            ("F", "THROTTLED"),
            ("E", "TRANSIT_CHECK_SKIPPED"),
            ("A", "TRANSIT_CONFLICT"),
            ("D", "SWAP_PROPOSED"),
            ("C", "FAILED"),
            ("B", "DEFERRED"),
            ("A", "SNAGGED"),
        ]
        assert outcomes[3]["details"] == "overlaps X"
