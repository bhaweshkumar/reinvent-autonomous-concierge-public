"""SPEC-18 §18.3: one-time JSON -> SQLite import and the migration-6 table backup."""

import json
import sqlite3

from reinvent_concierge.legacy_migration import (
    backup_and_drop_unused_tables,
    import_legacy_audit_log,
    import_legacy_waitlist,
)
from reinvent_concierge.planner.state_store import PlannerStateStore
from reinvent_concierge.waitlist import WaitlistStore


async def test_import_legacy_waitlist_migrates_and_renames_original(tmp_path):
    db_path = tmp_path / "planner_state.db"
    await PlannerStateStore(db_path).initialize()
    legacy_path = tmp_path / "waitlist.json"
    legacy_path.write_text(
        json.dumps(
            [
                {"session_id": "A", "priority": 5, "enabled": True},
                {"session_id": "B", "priority": 90, "enabled": False, "status": "FULL"},
            ]
        )
    )
    (tmp_path / "waitlist.json.lock").write_text("")

    imported = import_legacy_waitlist(db_path, legacy_path)

    assert imported == 2
    entries = {e.session_id: e for e in WaitlistStore(db_path).entries()}
    assert entries["A"].priority == 5 and entries["A"].enabled is True
    assert entries["B"].status == "FULL" and entries["B"].enabled is False
    assert not legacy_path.exists()
    assert (tmp_path / "waitlist.json.migrated").exists()
    assert not (tmp_path / "waitlist.json.lock").exists()


async def test_import_legacy_waitlist_is_a_no_op_once_migrated(tmp_path):
    db_path = tmp_path / "planner_state.db"
    await PlannerStateStore(db_path).initialize()
    legacy_path = tmp_path / "waitlist.json"

    assert import_legacy_waitlist(db_path, legacy_path) == 0
    assert WaitlistStore(db_path).entries() == []


async def test_import_legacy_audit_log_migrates_and_renames_original(tmp_path):
    db_path = tmp_path / "planner_state.db"
    await PlannerStateStore(db_path).initialize()
    legacy_path = tmp_path / "audit_log.json"
    legacy_path.write_text(
        '{"timestamp": "2026-01-01T00:00:00+00:00", "action": "A", '
        '"attendee_token_hash": "deadbeef"}\n'
        '{"timestamp": "2026-01-01T00:00:01+00:00", "action": "B", '
        '"attendee_token_hash": "deadbeef"}\n'
    )

    imported = import_legacy_audit_log(db_path, legacy_path)

    assert imported == 2
    connection = sqlite3.connect(db_path)
    rows = connection.execute("SELECT action FROM audit_events ORDER BY id").fetchall()
    connection.close()
    assert [r[0] for r in rows] == ["A", "B"]
    assert not legacy_path.exists()
    assert (tmp_path / "audit_log.json.migrated").exists()


def test_backup_and_drop_unused_tables_preserves_rows_before_dropping(tmp_path):
    db_path = tmp_path / "planner_state.db"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE demo_scenarios (scenario_id TEXT, event_id TEXT)")
    connection.execute("INSERT INTO demo_scenarios VALUES ('s1', 'e1')")
    connection.execute("CREATE TABLE monitor_checkpoints (event_id TEXT)")
    connection.commit()
    connection.close()

    backup_and_drop_unused_tables(db_path)

    backup_path = db_path.with_name(f"{db_path.name}.demo_scenarios.backup.json")
    assert json.loads(backup_path.read_text()) == [{"scenario_id": "s1", "event_id": "e1"}]
    # monitor_checkpoints had no rows: no backup file, nothing to lose.
    assert not db_path.with_name(f"{db_path.name}.monitor_checkpoints.backup.json").exists()


def test_backup_and_drop_unused_tables_is_idempotent(tmp_path):
    db_path = tmp_path / "planner_state.db"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE demo_scenarios (scenario_id TEXT)")
    connection.execute("INSERT INTO demo_scenarios VALUES ('s1')")
    connection.commit()
    connection.close()

    backup_and_drop_unused_tables(db_path)
    backup_path = db_path.with_name(f"{db_path.name}.demo_scenarios.backup.json")
    backup_path.write_text("[]")  # simulate a second run seeing an already-backed-up file

    backup_and_drop_unused_tables(db_path)  # must not overwrite or raise

    assert backup_path.read_text() == "[]"
