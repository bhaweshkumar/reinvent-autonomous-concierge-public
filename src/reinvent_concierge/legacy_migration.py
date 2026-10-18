"""One-time startup migration of pre-SPEC-18 JSON state into the planner database.

Each step is idempotent and resumable (SPEC-18 §18.7 AC3): it only acts while the
legacy file still exists under its original name, does all of its SQLite writes in one
transaction, and renames the original to ``.migrated`` only after that transaction
commits. A crash mid-import therefore leaves the JSON original untouched and the next
startup simply retries the whole file.
"""

import json
import logging
import sqlite3
from pathlib import Path
from typing import List

from .audit import AuditLogger
from .models import AuditLogEntry, WaitlistEntry
from .waitlist import WaitlistStore

logger = logging.getLogger(__name__)


def import_legacy_waitlist(db_path: Path, legacy_path: Path) -> int:
    """Import ``waitlist.json`` into ``waitlist_entries`` once."""
    if not legacy_path.exists():
        return 0
    with open(legacy_path, encoding="utf-8") as f:
        raw = json.load(f)
    entries: List[WaitlistEntry] = []
    for item in raw:
        try:
            entries.append(WaitlistEntry.model_validate(item))
        except ValueError as error:
            logger.warning("Skipping invalid legacy waitlist entry %r: %s", item, error)
    imported = WaitlistStore(db_path).import_entries(entries)
    legacy_path.rename(legacy_path.with_name(legacy_path.name + ".migrated"))
    legacy_path.with_name(legacy_path.name + ".lock").unlink(missing_ok=True)
    logger.info("Imported %d legacy waitlist entries from %s", imported, legacy_path)
    return imported


def import_legacy_audit_log(db_path: Path, legacy_path: Path) -> int:
    """Import ``audit_log.json`` (JSON-lines) into ``audit_events`` once."""
    if not legacy_path.exists():
        return 0
    entries: List[AuditLogEntry] = []
    with open(legacy_path, encoding="utf-8") as f:
        for line_number, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(AuditLogEntry.model_validate_json(line))
            except ValueError as error:
                logger.warning(
                    "Skipping invalid legacy audit log line %d in %s: %s",
                    line_number,
                    legacy_path,
                    error,
                )
    imported = AuditLogger(db_path, token_hash_provider=lambda: "").import_entries(entries)
    legacy_path.rename(legacy_path.with_name(legacy_path.name + ".migrated"))
    logger.info("Imported %d legacy audit log entries from %s", imported, legacy_path)
    return imported


def backup_and_drop_unused_tables(db_path: Path) -> None:
    """Back up and drop ``demo_scenarios``/``monitor_checkpoints`` (migration 6).

    Runs before :meth:`PlannerStateStore.initialize` applies migration 6's ``DROP
    TABLE`` statements, so any rows those tables still hold are preserved as JSON
    next to the database first. A no-op once the tables are gone.
    """
    if not db_path.exists():
        return
    connection = sqlite3.connect(db_path, timeout=5)
    try:
        connection.row_factory = sqlite3.Row
        for table in ("demo_scenarios", "monitor_checkpoints"):
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
            ).fetchone()
            if not exists:
                continue
            backup_path = db_path.with_name(f"{db_path.name}.{table}.backup.json")
            if backup_path.exists():
                continue
            rows = [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]  # noqa: S608
            if rows:
                with open(backup_path, "w", encoding="utf-8") as f:
                    json.dump(rows, f, indent=2)
                logger.info("Backed up %d rows from %s to %s", len(rows), table, backup_path)
    finally:
        connection.close()
