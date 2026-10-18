"""Smoke test for ``concierge audit export`` (SPEC-18 §18.3)."""

import json

from click.testing import CliRunner

from reinvent_concierge import cli as cli_module
from reinvent_concierge.audit import AuditLogger
from reinvent_concierge.credentials import CredentialManager
from reinvent_concierge.planner.state_store import PlannerStateStore
from tests.factories import TOKEN


async def test_audit_export_writes_json_lines(tmp_path, monkeypatch):
    for name in ("PLANNER_DB_PATH", "AWS_EVENTS_CREDENTIALS_PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PLANNER_DB_PATH", str(tmp_path / "planner_state.db"))
    monkeypatch.setenv("AWS_EVENTS_CREDENTIALS_PATH", str(tmp_path / "credentials"))
    monkeypatch.chdir(tmp_path)

    db_path = tmp_path / "planner_state.db"
    await PlannerStateStore(db_path).initialize()
    credentials = CredentialManager(tmp_path / "credentials")
    credentials.store_token(TOKEN)
    AuditLogger(db_path, credentials.hash_token).record("search_catalog", status="OK")

    output_path = tmp_path / "export.jsonl"
    result = CliRunner().invoke(cli_module.cli, ["audit", "export", "--output", str(output_path)])

    assert result.exit_code == 0, result.output
    lines = output_path.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["action"] == "search_catalog"
