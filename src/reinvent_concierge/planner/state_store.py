"""SQLite lifecycle and migrations for durable planner state."""

import asyncio
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Tuple

import aiosqlite

from .migrations import MIGRATIONS, Migration


class PlannerStateStore:
    # demo_scenarios and monitor_checkpoints were dropped by migration 6 (SPEC-18 §18.3):
    # they are deliberately absent from a current database, so they are excluded here.
    _BASE_SCHEMA_TABLES = (
        "preference_profiles",
        "schedule_snapshots",
        "itinerary_plans",
        "apply_runs",
        "apply_operations",
    )

    def __init__(
        self,
        path: Path,
        migrations: Tuple[Migration, ...] = MIGRATIONS,
    ) -> None:
        self.path = path
        self.migrations = migrations

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[aiosqlite.Connection]:
        """Yield a configured connection; callers must commit successful work."""
        async with aiosqlite.connect(self.path) as connection:
            os.chmod(self.path, 0o600)
            await connection.execute("PRAGMA busy_timeout = 5000")
            await connection.execute("PRAGMA foreign_keys = ON")
            try:
                yield connection
            except Exception:
                await connection.rollback()
                raise

    async def initialize(self) -> None:
        versions = tuple(migration.version for migration in self.migrations)
        if any(version <= 0 for version in versions):
            raise ValueError("migration versions must be positive")
        if len(versions) != len(set(versions)):
            raise ValueError("migration versions must be unique")
        if versions != tuple(range(1, len(versions) + 1)):
            raise ValueError("migration versions must be contiguous starting at 1")

        try:
            self.path.parent.mkdir(mode=0o700, parents=True)
        except FileExistsError:
            pass
        else:
            os.chmod(self.path.parent, 0o700)

        async with self._connect() as connection:
            while await self._apply_next_migration(connection, self.migrations):
                pass
            for attempt in range(50):
                try:
                    await connection.execute("PRAGMA journal_mode = WAL")
                except sqlite3.OperationalError as error:
                    if "locked" not in str(error) or attempt == 49:
                        raise
                    await asyncio.sleep(0.01)
                else:
                    break

        os.chmod(self.path, 0o600)

    async def _apply_next_migration(
        self,
        connection: aiosqlite.Connection,
        ordered_migrations: Tuple[Migration, ...],
    ) -> bool:
        await connection.execute("BEGIN IMMEDIATE")
        try:
            configured_by_version = {
                migration.version: migration for migration in ordered_migrations
            }
            configured_versions = set(configured_by_version)
            migration_table = await (
                await connection.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type = 'table' AND name = 'schema_migrations'
                    """
                )
            ).fetchone()
            if migration_table:
                columns = {
                    row[1]
                    for row in await (
                        await connection.execute("PRAGMA table_info(schema_migrations)")
                    ).fetchall()
                }
                if "checksum" not in columns:
                    legacy_rows = await (
                        await connection.execute("SELECT version, name FROM schema_migrations")
                    ).fetchall()
                    legacy_versions = {row[0] for row in legacy_rows}
                    if legacy_versions:
                        highest_applied = max(legacy_versions)
                        for migration in ordered_migrations:
                            if (
                                migration.version <= highest_applied
                                and migration.version not in legacy_versions
                            ):
                                raise RuntimeError(f"missing migration version {migration.version}")
                        unknown_versions = legacy_versions - configured_versions
                        if unknown_versions:
                            raise RuntimeError(f"unknown migration version {min(unknown_versions)}")
                        for version, name in legacy_rows:
                            if name != configured_by_version[version].name:
                                raise RuntimeError(f"migration identity drift at version {version}")
                    await connection.execute(
                        "ALTER TABLE schema_migrations ADD COLUMN checksum TEXT"
                    )
                    for version, _name in legacy_rows:
                        await connection.execute(
                            "UPDATE schema_migrations SET checksum = ? WHERE version = ?",
                            (configured_by_version[version].checksum, version),
                        )
            rows = (
                await (
                    await connection.execute(
                        "SELECT version, name, checksum FROM schema_migrations"
                    )
                ).fetchall()
                if migration_table
                else []
            )
            applied_versions = {row[0] for row in rows}

            if applied_versions:
                highest_applied = max(applied_versions)
                for migration in ordered_migrations:
                    if (
                        migration.version <= highest_applied
                        and migration.version not in applied_versions
                    ):
                        raise RuntimeError(f"missing migration version {migration.version}")
                unknown_versions = applied_versions - configured_versions
                if unknown_versions:
                    raise RuntimeError(f"unknown migration version {min(unknown_versions)}")
                for version, name, checksum in rows:
                    migration = configured_by_version[version]
                    if name != migration.name or checksum != migration.checksum:
                        raise RuntimeError(f"migration identity drift at version {version}")

            if 1 in applied_versions:
                await self._restore_legacy_base_schema(connection, configured_by_version[1])

            pending_migration = next(
                (item for item in ordered_migrations if item.version not in applied_versions),
                None,
            )
            if pending_migration is None:
                await connection.commit()
                return False
            for statement in pending_migration.statements:
                await connection.execute(statement)
            await connection.execute(
                """
                INSERT INTO schema_migrations (version, name, checksum, applied_at)
                VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """,
                (
                    pending_migration.version,
                    pending_migration.name,
                    pending_migration.checksum,
                ),
            )
        except Exception:
            await connection.rollback()
            raise
        await connection.commit()
        return True

    async def _restore_legacy_base_schema(
        self, connection: aiosqlite.Connection, migration: Migration
    ) -> None:
        if migration.name != "base_schema":
            return
        existing_tables = {
            row[0]
            for row in await (
                await connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            ).fetchall()
        }
        if all(table in existing_tables for table in self._BASE_SCHEMA_TABLES):
            return
        for statement in migration.statements:
            await connection.execute(
                statement.replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ", 1)
            )
