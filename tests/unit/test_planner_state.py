"""Planner database configuration and migration behavior."""

import asyncio
import json
import multiprocessing
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import aiosqlite
import pytest

from reinvent_concierge.config import APP_HOME, Config
from reinvent_concierge.models import AttendeeSchedule
from reinvent_concierge.planner.apply_journal import ApplyJournalStore, InvalidStateTransition
from reinvent_concierge.planner.migrations import MIGRATIONS, Migration
from reinvent_concierge.planner.models import ScheduleSnapshot
from reinvent_concierge.planner.snapshots import ScheduleSnapshotService
from reinvent_concierge.planner.state_store import PlannerStateStore
from tests.factories import make_session


def _initialize_planner_store(path: str) -> None:
    asyncio.run(PlannerStateStore(Path(path)).initialize())


def test_planner_db_path_defaults_expands_and_isolated(config, monkeypatch, tmp_path):
    assert Config().planner_db_path == APP_HOME / "planner_state.db"
    assert config.planner_db_path == tmp_path / "planner_state.db"

    monkeypatch.setenv("PLANNER_DB_PATH", "~/custom-planner.db")

    assert Config().planner_db_path == Path.home() / "custom-planner.db"


def test_planner_db_path_environment_takes_precedence_over_file(monkeypatch, tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"PLANNER_DB_PATH": "~/from-file.db"}))
    expected = tmp_path / "from-env.db"
    monkeypatch.setenv("PLANNER_DB_PATH", str(expected))

    assert Config.from_env_or_file(config_file).planner_db_path == expected


def test_migration_checksum_is_deterministic_and_covers_identity():
    migration = Migration(version=1, name="base", statements=("SELECT 1",))

    assert (
        migration.checksum == Migration(version=1, name="base", statements=("SELECT 1",)).checksum
    )
    assert len(migration.checksum) == 64
    assert migration.checksum != Migration(2, "base", ("SELECT 1",)).checksum
    assert migration.checksum != Migration(1, "renamed", ("SELECT 1",)).checksum
    assert migration.checksum != Migration(1, "base", ("SELECT 2",)).checksum


def test_connection_context_is_private(config):
    store = PlannerStateStore(config.planner_db_path)

    assert not hasattr(store, "connection")
    assert hasattr(store, "_connect")


async def test_connect_rolls_back_exceptions_and_requires_caller_commit(config):
    store = PlannerStateStore(config.planner_db_path)
    await store.initialize()
    insert = """
        INSERT INTO preference_profiles
        (profile_id, event_id, profile_json, created_at, updated_at)
        VALUES (?, 'event', '{}', 'created', 'updated')
    """

    with pytest.raises(RuntimeError, match="stop"):
        async with store._connect() as connection:
            await connection.execute(insert, ("rolled-back",))
            raise RuntimeError("stop")

    async with store._connect() as connection:
        await connection.execute(insert, ("uncommitted",))

    async with store._connect() as connection:
        await connection.execute(insert, ("committed",))
        await connection.commit()

    with sqlite3.connect(config.planner_db_path) as connection:
        profile_ids = {
            row[0] for row in connection.execute("SELECT profile_id FROM preference_profiles")
        }

    assert profile_ids == {"committed"}


async def test_initialize_creates_v1_schema_and_configures_sqlite(config):
    store = PlannerStateStore(config.planner_db_path)

    await store.initialize()

    async with store._connect() as connection:
        tables = {
            row[0]
            async for row in await connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        foreign_keys = (await (await connection.execute("PRAGMA foreign_keys")).fetchone())[0]
        busy_timeout = (await (await connection.execute("PRAGMA busy_timeout")).fetchone())[0]
        journal_mode = (await (await connection.execute("PRAGMA journal_mode")).fetchone())[0]
        foreign_key_rows = await (
            await connection.execute("PRAGMA foreign_key_list(apply_operations)")
        ).fetchall()

    # demo_scenarios/monitor_checkpoints are dropped and waitlist_entries/audit_events
    # added by migrations 5-6 (SPEC-18 §18.3); sqlite_sequence backs AUTOINCREMENT.
    assert tables == {
        "schema_migrations",
        "preference_profiles",
        "schedule_snapshots",
        "itinerary_plans",
        "apply_runs",
        "apply_operations",
        "quota_events",
        "quota_blocks",
        "runtime_leases",
        "waitlist_entries",
        "audit_events",
        "sqlite_sequence",
    }
    assert foreign_keys == 1
    assert busy_timeout == 5000
    assert journal_mode == "wal"
    assert any(
        row[2] == "apply_runs" and row[3] == "apply_run_id" and row[4] == "run_id"
        for row in foreign_key_rows
    )
    assert config.planner_db_path.stat().st_mode & 0o777 == 0o600
    assert config.planner_db_path.parent.stat().st_mode & 0o777 == 0o700


async def test_initialize_preserves_preexisting_parent_mode(tmp_path):
    parent = tmp_path / "custom"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    path = parent / "planner.db"

    await PlannerStateStore(path).initialize()

    assert parent.stat().st_mode & 0o777 == 0o755
    assert path.stat().st_mode & 0o777 == 0o600


async def test_initialize_records_migration_once_and_is_idempotent(config):
    store = PlannerStateStore(config.planner_db_path)

    await store.initialize()
    await store.initialize()

    async with aiosqlite.connect(config.planner_db_path) as connection:
        rows = await (
            await connection.execute(
                "SELECT version, name, checksum, applied_at FROM schema_migrations ORDER BY version"
            )
        ).fetchall()

    assert [(row[0], row[1]) for row in rows] == [
        (migration.version, migration.name) for migration in MIGRATIONS
    ]
    for row, migration in zip(rows, MIGRATIONS, strict=True):
        assert row[2] == migration.checksum
        assert row[3].endswith("Z")


@pytest.mark.parametrize(
    "changed_migration",
    [
        Migration(1, "renamed_base_schema", MIGRATIONS[0].statements),
        Migration(1, MIGRATIONS[0].name, (*MIGRATIONS[0].statements, "SELECT 1")),
    ],
)
async def test_initialize_rejects_identity_drift_before_new_migrations(config, changed_migration):
    await PlannerStateStore(config.planner_db_path).initialize()
    pending = Migration(
        MIGRATIONS[-1].version + 1,
        "pending",
        ("CREATE TABLE must_not_exist (id TEXT)",),
    )

    with pytest.raises(RuntimeError, match="migration identity drift at version 1"):
        await PlannerStateStore(
            config.planner_db_path,
            migrations=(changed_migration, *MIGRATIONS[1:], pending),
        ).initialize()

    with sqlite3.connect(config.planner_db_path) as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'must_not_exist'"
        ).fetchone()

    assert table is None


async def test_initialize_backfills_checksum_for_matching_legacy_migration(config):
    with sqlite3.connect(config.planner_db_path) as connection:
        connection.execute(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO schema_migrations VALUES (1, 'base_schema', '2026-09-30T00:00:00Z')"
        )

    await PlannerStateStore(config.planner_db_path).initialize()

    with sqlite3.connect(config.planner_db_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(schema_migrations)")}
        checksum = connection.execute(
            "SELECT checksum FROM schema_migrations WHERE version = 1"
        ).fetchone()[0]

    assert "checksum" in columns
    assert checksum == MIGRATIONS[0].checksum


async def test_initialize_rejects_mismatched_legacy_migration_before_backfill(config):
    with sqlite3.connect(config.planner_db_path) as connection:
        connection.execute(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO schema_migrations VALUES (1, 'renamed', '2026-09-30T00:00:00Z')"
        )

    with pytest.raises(RuntimeError, match="migration identity drift at version 1"):
        await PlannerStateStore(config.planner_db_path).initialize()

    with sqlite3.connect(config.planner_db_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(schema_migrations)")}

    assert "checksum" not in columns


@pytest.mark.parametrize(
    ("versions", "message"),
    [
        ((1, 1), "unique"),
        ((1, 3), "contiguous"),
        ((2, 1), "contiguous"),
        ((0,), "positive"),
    ],
)
async def test_initialize_validates_migration_versions_before_db_work(config, versions, message):
    migrations = tuple(
        Migration(version=version, name=f"migration_{index}", statements=())
        for index, version in enumerate(versions)
    )

    with pytest.raises(ValueError, match=message):
        await PlannerStateStore(config.planner_db_path, migrations=migrations).initialize()

    assert not config.planner_db_path.exists()


async def test_concurrent_initialize_applies_migration_once(config):
    await asyncio.gather(
        *(PlannerStateStore(config.planner_db_path).initialize() for _ in range(10))
    )

    with sqlite3.connect(config.planner_db_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]

    assert count == len(MIGRATIONS)


def test_concurrent_process_initialize_applies_migration_once(tmp_path):
    path = tmp_path / "planner.db"
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=_initialize_planner_store, args=(str(path),)) for _ in range(4)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
    for process in processes:
        if process.is_alive():
            process.terminate()
            process.join()

    assert [process.exitcode for process in processes] == [0, 0, 0, 0]
    with sqlite3.connect(path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]

    assert count == len(MIGRATIONS)


async def test_failing_migration_rolls_back_schema_and_version(config):
    failing_version = MIGRATIONS[-1].version + 1
    failing_migration = Migration(
        version=failing_version,
        name="failing_migration",
        statements=(
            "CREATE TABLE rolled_back (id TEXT PRIMARY KEY)",
            "THIS IS NOT VALID SQL",
        ),
    )
    store = PlannerStateStore(config.planner_db_path, migrations=(*MIGRATIONS, failing_migration))

    with pytest.raises(sqlite3.OperationalError):
        await store.initialize()

    async with aiosqlite.connect(config.planner_db_path) as connection:
        table = await (
            await connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'rolled_back'"
            )
        ).fetchone()
        version = await (
            await connection.execute(
                "SELECT version FROM schema_migrations WHERE version = ?", (failing_version,)
            )
        ).fetchone()

    assert table is None
    assert version is None
    assert config.planner_db_path.stat().st_mode & 0o777 == 0o600


async def test_initialize_rejects_missing_earlier_migration(config):
    with sqlite3.connect(config.planner_db_path) as connection:
        connection.execute(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO schema_migrations VALUES (2, 'later', '2026-09-30T00:00:00Z')"
        )

    with pytest.raises(RuntimeError, match="missing migration version 1"):
        await PlannerStateStore(config.planner_db_path).initialize()


async def test_failing_first_migration_rolls_back_migration_table(config):
    failing_first_migration = Migration(
        version=1,
        name="failing_first_migration",
        statements=(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """,
            "CREATE TABLE rolled_back (id TEXT PRIMARY KEY)",
            "THIS IS NOT VALID SQL",
        ),
    )

    with pytest.raises(sqlite3.OperationalError):
        await PlannerStateStore(
            config.planner_db_path,
            migrations=(failing_first_migration,),
        ).initialize()

    with sqlite3.connect(config.planner_db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    assert tables == set()


async def test_initialize_rejects_unknown_applied_migration(config):
    store = PlannerStateStore(config.planner_db_path)
    await store.initialize()
    unknown_version = MIGRATIONS[-1].version + 1
    with sqlite3.connect(config.planner_db_path) as connection:
        connection.execute(
            """
            INSERT INTO schema_migrations (version, name, checksum, applied_at)
            VALUES (?, 'unknown', 'unknown', '2026-09-30T00:00:00Z')
            """,
            (unknown_version,),
        )

    with pytest.raises(RuntimeError, match=f"unknown migration version {unknown_version}"):
        await store.initialize()


async def test_schedule_snapshot_service_round_trips_and_returns_newest_first(config):
    store = PlannerStateStore(config.planner_db_path)
    await store.initialize()
    service = ScheduleSnapshotService(store)

    first = await service.capture("event", AttendeeSchedule(booked=[make_session("first")]))
    second = await service.capture("event", AttendeeSchedule(booked=[make_session("second")]))

    assert await service.latest("event") == second
    assert await service.recent("event") == [second, first]
    assert await service.latest("missing") is None


@pytest.mark.parametrize("limit", [0, -1, True, "1"])
async def test_schedule_snapshot_service_rejects_invalid_limits(config, limit):
    store = PlannerStateStore(config.planner_db_path)
    await store.initialize()

    with pytest.raises(ValueError, match="positive integer"):
        await ScheduleSnapshotService(store).recent("event", limit=limit)


async def test_schedule_snapshot_service_retains_fifty_per_event(config):
    store = PlannerStateStore(config.planner_db_path)
    await store.initialize()
    service = ScheduleSnapshotService(store)

    first = await service.capture("event", AttendeeSchedule(booked=[make_session("session-0")]))
    for index in range(1, 51):
        await service.capture(
            "event",
            AttendeeSchedule(booked=[make_session(f"session-{index}")]),
        )

    retained = await service.recent("event", limit=100)

    assert len(retained) == 50
    assert first not in retained
    assert retained[0].booked_session_ids == ("session-50",)


async def test_apply_journal_persists_operations_and_enforces_constraints(config):
    store = PlannerStateStore(config.planner_db_path)
    await store.initialize()
    journal = ApplyJournalStore(store)
    run = await journal.create_run("event", uuid4(), "snapshot")

    assert await journal.get_run(run.run_id) == run
    operation = await journal.add_operation(run.run_id, 1, "RESERVE", {"session_ids": ["S1"]})
    approved = await journal.transition_run(run.run_id, "PREVIEWED", "APPROVED")
    attempting = await journal.mark_attempting(operation.operation_id)
    confirmed = await journal.record_operation_result(
        operation.operation_id,
        "CONFIRMED",
        result={"reserved": ["S1"]},
    )

    assert approved.state == "APPROVED"
    assert attempting.state == "ATTEMPTING"
    assert confirmed.state == "CONFIRMED"
    assert confirmed.result == {"reserved": ["S1"]}
    with pytest.raises(InvalidStateTransition):
        await journal.transition_run(run.run_id, "PREVIEWED", "APPLYING")
    with pytest.raises(InvalidStateTransition):
        await journal.record_operation_result(operation.operation_id, "CONFIRMED")
    with pytest.raises(sqlite3.IntegrityError):
        await journal.add_operation(run.run_id, 1, "FAVORITE", {})
    with pytest.raises(sqlite3.IntegrityError):
        await journal.add_operation(uuid4(), 1, "RESERVE", {})


async def test_apply_journal_reconciles_only_existing_attempting_operations(config):
    store = PlannerStateStore(config.planner_db_path)
    await store.initialize()
    journal = ApplyJournalStore(store)
    run = await journal.create_run("event", uuid4(), "snapshot")
    pending = await journal.add_operation(run.run_id, 1, "RESERVE", {})
    attempting = await journal.add_operation(run.run_id, 2, "FAVORITE", {})
    await journal.mark_attempting(attempting.operation_id)
    seen = []

    def resolver(operation, schedule):
        seen.append((operation.operation_id, schedule))
        return "CONFIRMED"

    schedule = AttendeeSchedule()
    reconciled = await journal.reconcile_attempting(run.run_id, schedule, resolver)

    assert reconciled[0].operation_id == attempting.operation_id
    assert reconciled[0].state == "CONFIRMED"
    assert seen == [(attempting.operation_id, schedule)]
    assert pending.state == "PENDING"
    assert await journal.reconcile_attempting(run.run_id, schedule, resolver) == []


async def test_snapshot_order_ignores_iso_string_collation(config):
    store = PlannerStateStore(config.planner_db_path)
    await store.initialize()
    service = ScheduleSnapshotService(store)
    earlier = datetime(2026, 12, 1, 10, 0, 0, 0, tzinfo=timezone.utc)
    later_in_same_second = earlier + timedelta(microseconds=1)

    original = ScheduleSnapshot.from_schedule.__func__

    def stamped(captured_at):
        def build(cls, event_id, schedule):
            return original(cls, event_id, schedule).model_copy(update={"captured_at": captured_at})

        return classmethod(build)

    with patch.object(ScheduleSnapshot, "from_schedule", stamped(earlier)):
        await service.capture("event", AttendeeSchedule(booked=[make_session("FIRST")]))
    with patch.object(ScheduleSnapshot, "from_schedule", stamped(later_in_same_second)):
        await service.capture("event", AttendeeSchedule(booked=[make_session("SECOND")]))

    latest = await service.latest("event")
    assert latest is not None and latest.booked_session_ids == ("SECOND",)


async def test_snapshot_retention_with_iso_collation_hazard(config):
    store = PlannerStateStore(config.planner_db_path)
    await store.initialize()
    service = ScheduleSnapshotService(store)
    base = datetime(2026, 12, 1, 10, 0, 0, 0, tzinfo=timezone.utc)

    original = ScheduleSnapshot.from_schedule.__func__

    def stamped(captured_at):
        def build(cls, event_id, schedule):
            return original(cls, event_id, schedule).model_copy(update={"captured_at": captured_at})

        return classmethod(build)

    for i in range(51):
        timestamp = base + timedelta(microseconds=i)
        with patch.object(ScheduleSnapshot, "from_schedule", stamped(timestamp)):
            await service.capture("event", AttendeeSchedule(booked=[make_session(f"session-{i}")]))

    retained = await service.recent("event", limit=100)

    assert len(retained) == 50
    assert retained[0].booked_session_ids == ("session-50",)
    assert not any(s.booked_session_ids == ("session-0",) for s in retained)


async def test_latest_preview_uses_insertion_order_within_one_second(config):
    from reinvent_concierge.planner.models import ApplyRun

    store = PlannerStateStore(config.planner_db_path)
    await store.initialize()
    journal = ApplyJournalStore(store)
    plan_id = uuid4()
    base = datetime(2026, 12, 1, 10, 0, 0, 0, tzinfo=timezone.utc)

    with patch(
        "reinvent_concierge.planner.apply_journal.ApplyRun",
        side_effect=lambda **kw: ApplyRun(**{**kw, "created_at": base, "updated_at": base}),
    ):
        first = await journal.create_run("event", uuid4(), "fp", plan_id)

    later = base + timedelta(microseconds=1)
    with patch(
        "reinvent_concierge.planner.apply_journal.ApplyRun",
        side_effect=lambda **kw: ApplyRun(**{**kw, "created_at": later, "updated_at": later}),
    ):
        second = await journal.create_run("event", uuid4(), "fp", plan_id)

    latest = await journal.latest_preview_for_plan(plan_id)
    assert latest is not None and latest.run_id == second.run_id
    assert latest.run_id != first.run_id
