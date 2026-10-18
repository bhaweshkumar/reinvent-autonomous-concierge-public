"""`concierge doctor` checks and the hosted MCP client wrapper."""

import json
import sys

import aiosqlite
import pytest
import respx
from fastmcp import Client, FastMCP

from reinvent_concierge.api.hosted_mcp import EXPECTED_TOOLS, HostedMCPClient
from reinvent_concierge.credentials import CredentialManager
from reinvent_concierge.doctor import check_catalog_mapping, check_semantic_extra, run_checks
from tests.factories import API_BASE, EVENT_ID, wire_schedule, wire_session

EVENT = f"{API_BASE}/events/{EVENT_ID}"
EVENT_JSON = {
    "eventId": EVENT_ID,
    "name": "re:Invent 2026",
    "eventType": "re:Invent",
    "startDate": "2026-11-30T05:30:00.000-08:00",
    "endDate": "2026-12-04T23:59:00.000-08:00",
    "isOnline": False,
    "authenticationRequired": True,
}


def mock_healthy_api():
    respx.get(f"{API_BASE}/events").respond(200, json={"items": [EVENT_JSON]})
    respx.get(EVENT).respond(200, json={"event": EVENT_JSON})
    respx.get(f"{EVENT}/schedule").respond(200, json=wire_schedule(reserved=["A"]))
    respx.get(f"{EVENT}/sessions").respond(
        200, json={"items": [wire_session("A")], "totalCount": 1}
    )


async def seed_current_catalog(config):
    config.local_db_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(config.local_db_path) as db:
        await db.execute("CREATE TABLE sessions (session_id TEXT, venue_source TEXT, status TEXT)")
        await db.execute("INSERT INTO sessions VALUES ('A', 'REGISTRY', 'OPEN')")
        await db.commit()


class TestDoctor:
    @respx.mock
    async def test_all_checks_pass_when_signed_in_and_registered(self, config, credentials):
        mock_healthy_api()
        await seed_current_catalog(config)

        results = {r.name: r for r in await run_checks(config, credentials)}

        assert all(r.ok for r in results.values()), results
        assert "1 reserved" in results["Signed in and registered"].detail
        assert "registration required" in results["Event"].detail
        assert results["Catalog mapping current"].ok

    @respx.mock
    async def test_run_checks_reports_a_missing_local_catalog(self, config, credentials):
        mock_healthy_api()

        results = {r.name: r for r in await run_checks(config, credentials)}

        assert results["Catalog mapping current"].ok is False

    @respx.mock
    async def test_unregistered_attendee_gets_actionable_hint(self, config, credentials):
        mock_healthy_api()
        respx.get(f"{EVENT}/schedule").respond(403, json={"message": "not registered"})

        results = {r.name: r for r in await run_checks(config, credentials)}

        assert results["Signed in and registered"].ok is False
        assert "not registered" in results["Signed in and registered"].detail

    @respx.mock
    async def test_missing_token_points_to_login(self, config):
        mock_healthy_api()
        respx.get(f"{EVENT}/sessions").respond(401, json={"message": "Sign in to continue"})
        anonymous = CredentialManager(config.aws_events_credentials_path)

        results = {r.name: r for r in await run_checks(config, anonymous)}

        assert results["API reachable"].ok
        assert "concierge login" in results["Token stored"].detail
        assert "Signed in and registered" not in results
        assert "concierge login" in results["Catalog readable"].detail


class TestCatalogMappingCheck:
    async def seed(self, config, rows):
        config.local_db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(config.local_db_path) as db:
            await db.execute(
                "CREATE TABLE sessions (session_id TEXT, venue_source TEXT, "
                "status TEXT DEFAULT 'OPEN')"
            )
            await db.executemany(
                "INSERT INTO sessions (session_id, venue_source, status) VALUES (?, ?, ?)",
                [(*row, "OPEN") if len(row) == 2 else row for row in rows],
            )
            await db.commit()

    async def test_flags_pre_v2_rows(self, config):
        await self.seed(config, [("A", None), ("B", "REGISTRY")])

        result = await check_catalog_mapping(config.local_db_path)

        assert result.ok is False
        assert "1 of 2" in result.detail and "concierge sync" in result.detail

    async def test_passes_when_every_row_has_a_mapping_source(self, config):
        await self.seed(config, [("A", "REGISTRY"), ("B", "ROOM_PARSED")])

        result = await check_catalog_mapping(config.local_db_path)

        assert result.ok is True and "2 sessions" in result.detail

    async def test_cancelled_legacy_rows_do_not_fail_the_check(self, config):
        # Cancelled rows never reappear in a fetched catalog, so sync can never re-map them.
        await self.seed(config, [("A", "REGISTRY"), ("B", None, "CANCELLED")])

        result = await check_catalog_mapping(config.local_db_path)

        assert result.ok is True and "1 sessions" in result.detail

    async def test_unreadable_database_fails_instead_of_raising(self, config):
        config.local_db_path.parent.mkdir(parents=True, exist_ok=True)
        config.local_db_path.write_bytes(b"this is not a sqlite database" * 100)

        result = await check_catalog_mapping(config.local_db_path)

        assert result.ok is False and "concierge sync --full" in result.detail

    async def test_missing_database_points_to_sync(self, config):
        result = await check_catalog_mapping(config.local_db_path)

        assert result.ok is False and "concierge sync" in result.detail

    async def test_database_without_the_column_points_to_sync(self, config):
        config.local_db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(config.local_db_path) as db:
            await db.execute("CREATE TABLE sessions (session_id TEXT)")
            await db.commit()

        result = await check_catalog_mapping(config.local_db_path)

        assert result.ok is False and "concierge sync" in result.detail


class TestSemanticExtraCheck:
    def test_reports_installed_when_fastembed_imports(self):
        result = check_semantic_extra()

        assert result.ok is True
        assert "fastembed is installed" in result.detail

    def test_reports_missing_when_fastembed_does_not_import(self, monkeypatch):
        # `None` in sys.modules tells the import system this module failed to import before.
        monkeypatch.setitem(sys.modules, "fastembed", None)

        result = check_semantic_extra()

        assert result.ok is False
        assert "pip install '.[semantic]'" in result.detail


class TestHostedMCPClient:
    @pytest.fixture
    def fake_hosted_server(self):
        server = FastMCP("fake-awsevents")

        @server.tool(name="AWSEventsPublicApi-Mcp-prod___ListEvents")
        def list_events() -> str:
            return json.dumps({"items": [{"eventId": EVENT_ID}]})

        @server.tool(name="GetSchedule")
        def get_schedule(eventId: str) -> dict:  # noqa: N803 - wire parameter name
            return {"schedule": {"reserved": [], "favorites": ["A"], "personalTime": []}}

        return server

    async def test_parses_text_and_structured_results(self, fake_hosted_server):
        hosted = HostedMCPClient("unused-token")
        hosted._client = Client(fake_hosted_server)

        async with hosted:
            names = await hosted.list_tool_names()
            events = await hosted.call("ListEvents")
            schedule = await hosted.call("GetSchedule", {"eventId": EVENT_ID})

        assert set(names) == {"ListEvents", "GetSchedule"}
        assert events["items"][0]["eventId"] == EVENT_ID
        assert schedule["schedule"]["favorites"] == ["A"]

    def test_expected_tools_mirror_the_rest_operations(self):
        assert len(EXPECTED_TOOLS) == 12
