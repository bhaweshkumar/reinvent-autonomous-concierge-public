"""Ordered schema migrations for durable planner state."""

import hashlib
import json
from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: Tuple[str, ...]

    @property
    def checksum(self) -> str:
        identity = json.dumps(
            [self.version, self.name, self.statements],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()


MIGRATIONS: Tuple[Migration, ...] = (
    Migration(
        version=1,
        name="base_schema",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                checksum TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE preference_profiles (
                profile_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,
                profile_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE schedule_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                booked_session_ids_json TEXT NOT NULL,
                favorite_session_ids_json TEXT NOT NULL,
                personal_time_json TEXT NOT NULL,
                fingerprint TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE itinerary_plans (
                plan_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,
                profile_id TEXT NOT NULL,
                source_snapshot_fingerprint TEXT NOT NULL,
                plan_set_id TEXT NOT NULL,
                strategy TEXT NOT NULL,
                plan_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE apply_runs (
                run_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,
                profile_id TEXT NOT NULL,
                plan_id TEXT,
                snapshot_fingerprint TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                error TEXT
            )
            """,
            """
            CREATE TABLE apply_operations (
                operation_id TEXT PRIMARY KEY,
                apply_run_id TEXT NOT NULL,
                operation_order INTEGER NOT NULL,
                operation_type TEXT NOT NULL,
                request_json TEXT NOT NULL,
                result_json TEXT,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                error TEXT,
                FOREIGN KEY (apply_run_id) REFERENCES apply_runs(run_id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE monitor_checkpoints (
                event_id TEXT PRIMARY KEY,
                schedule_fingerprint TEXT,
                catalog_updated_at TEXT,
                checked_at TEXT NOT NULL,
                checkpoint_json TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE demo_scenarios (
                scenario_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,
                scenario_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX preference_profiles_event_updated_idx
            ON preference_profiles(event_id, updated_at)
            """,
            """
            CREATE INDEX schedule_snapshots_event_captured_idx
            ON schedule_snapshots(event_id, captured_at DESC)
            """,
            """
            CREATE INDEX itinerary_plans_event_created_idx
            ON itinerary_plans(event_id, created_at DESC)
            """,
            """
            CREATE INDEX apply_runs_event_created_idx
            ON apply_runs(event_id, created_at DESC)
            """,
            """
            CREATE UNIQUE INDEX apply_operations_run_order_idx
            ON apply_operations(apply_run_id, operation_order)
            """,
            """
            CREATE INDEX monitor_checkpoints_checked_idx
            ON monitor_checkpoints(checked_at)
            """,
            """
            CREATE INDEX demo_scenarios_event_created_idx
            ON demo_scenarios(event_id, created_at DESC)
            """,
        ),
    ),
    Migration(
        version=2,
        name="operation_quota_state",
        statements=(
            """
            CREATE TABLE quota_events (
                event_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                consumed_at TEXT NOT NULL,
                units INTEGER NOT NULL CHECK (units > 0)
            )
            """,
            """
            CREATE TABLE quota_blocks (
                event_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                blocked_until TEXT NOT NULL,
                PRIMARY KEY (event_id, operation)
            )
            """,
            """
            CREATE INDEX quota_events_scope_consumed_idx
            ON quota_events(event_id, operation, consumed_at)
            """,
        ),
    ),
    Migration(
        version=3,
        name="runtime_leases",
        statements=(
            """
            CREATE TABLE runtime_leases (
                event_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
            """,
        ),
    ),
    Migration(
        version=4,
        name="apply_run_preview_payload",
        statements=(
            """
            ALTER TABLE apply_runs ADD COLUMN preview_json TEXT
            """,
        ),
    ),
    Migration(
        version=5,
        name="waitlist_and_audit_tables",
        statements=(
            """
            CREATE TABLE waitlist_entries (
                session_id    TEXT PRIMARY KEY,
                priority      INTEGER NOT NULL,
                added_at      TEXT NOT NULL,
                last_checked  TEXT,
                status        TEXT NOT NULL,
                enabled       INTEGER NOT NULL,
                title         TEXT,
                venue_id      TEXT
            )
            """,
            """
            CREATE TABLE audit_events (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp           TEXT NOT NULL,
                action              TEXT NOT NULL,
                session_id          TEXT,
                status              TEXT,
                attendee_token_hash TEXT NOT NULL,
                details             TEXT,
                arguments_json      TEXT,
                result_json         TEXT
            )
            """,
            """
            CREATE INDEX audit_events_timestamp_idx ON audit_events(timestamp)
            """,
        ),
    ),
    Migration(
        version=6,
        name="drop_unused_demo_and_monitor_tables",
        statements=(
            # Rows are backed up to JSON next to the database file before this runs
            # (reinvent_concierge.legacy_migration.backup_and_drop_unused_tables).
            "DROP TABLE IF EXISTS demo_scenarios",
            "DROP TABLE IF EXISTS monitor_checkpoints",
        ),
    ),
)
