"""Append-only audit trail shared by the MCP agent and the seat snapper.

Rows live in the planner SQLite database's ``audit_events`` table (SPEC-18 §18.3):
one store for user state instead of a hand-rolled JSON-lines file. ``concierge audit
export`` writes JSON-lines from this table on demand for inspection.
"""

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .models import AuditLogEntry

logger = logging.getLogger(__name__)

DRY_RUN_PREFIX = "[DRY_RUN] "

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp          TEXT NOT NULL,
    action             TEXT NOT NULL,
    session_id         TEXT,
    status             TEXT,
    attendee_token_hash TEXT NOT NULL,
    details            TEXT,
    arguments_json     TEXT,
    result_json        TEXT
)
"""
_INDEX = "CREATE INDEX IF NOT EXISTS audit_events_timestamp_idx ON audit_events(timestamp)"


class AuditLogger:
    """Writes one row per call; stores sha256(token), never the token itself.

    Takes a ``token_hash_provider`` callable rather than a ``CredentialManager`` so
    this module never has to import credential handling (SPEC-18 §18.2 layering fix).
    """

    def __init__(self, path: Path, token_hash_provider: Callable[[], str]):
        self.path = Path(path).expanduser()
        self.token_hash_provider = token_hash_provider

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute(_SCHEMA)
        connection.execute(_INDEX)
        return connection

    def record(
        self,
        action: str,
        *,
        session_id: Optional[str] = None,
        status: Optional[str] = None,
        details: Optional[str] = None,
        arguments: Optional[Dict[str, Any]] = None,
        result: Optional[Any] = None,
        dry_run: bool = False,
    ) -> AuditLogEntry:
        """Append an entry. Failures are logged, never raised, so auditing cannot undo a
        write that already happened upstream."""
        entry = AuditLogEntry(
            action=f"{DRY_RUN_PREFIX}{action}" if dry_run else action,
            session_id=session_id,
            status=status,
            details=details,
            arguments=arguments,
            result=result,
            attendee_token_hash=self.token_hash_provider(),
        )
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO audit_events "
                    "(timestamp, action, session_id, status, attendee_token_hash, "
                    "details, arguments_json, result_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        entry.timestamp.isoformat(),
                        entry.action,
                        entry.session_id,
                        entry.status,
                        entry.attendee_token_hash,
                        entry.details,
                        json.dumps(entry.arguments) if entry.arguments is not None else None,
                        json.dumps(entry.result, default=str) if entry.result is not None else None,
                    ),
                )
        except sqlite3.Error:
            logger.exception("Failed to write audit log entry for %s", entry.action)
        return entry

    def import_entries(self, entries: list[AuditLogEntry]) -> int:
        """Bulk-import entries verbatim (one-time legacy JSON import, SPEC-18 §18.3)."""
        with self._connect() as connection:
            connection.executemany(
                "INSERT INTO audit_events "
                "(timestamp, action, session_id, status, attendee_token_hash, "
                "details, arguments_json, result_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        entry.timestamp.isoformat(),
                        entry.action,
                        entry.session_id,
                        entry.status,
                        entry.attendee_token_hash,
                        entry.details,
                        json.dumps(entry.arguments) if entry.arguments is not None else None,
                        json.dumps(entry.result, default=str) if entry.result is not None else None,
                    )
                    for entry in entries
                ],
            )
            connection.commit()
        return len(entries)

    def recent_entries(self, limit: int = 500) -> list[Dict[str, Any]]:
        """Newest-first audit rows, for the UI's recent-outcomes panel (SPEC-18 §18.4)."""
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            {
                "timestamp": row["timestamp"],
                "action": row["action"],
                "session_id": row["session_id"],
                "status": row["status"],
                "details": row["details"],
            }
            for row in rows
        ]

    def export_json_lines(self) -> str:
        """Render every row as JSON-lines, oldest first (``concierge audit export``)."""
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        lines = []
        for row in rows:
            entry = AuditLogEntry(
                timestamp=row["timestamp"],
                action=row["action"],
                session_id=row["session_id"],
                status=row["status"],
                attendee_token_hash=row["attendee_token_hash"],
                details=row["details"],
                arguments=json.loads(row["arguments_json"]) if row["arguments_json"] else None,
                result=json.loads(row["result_json"]) if row["result_json"] else None,
            )
            lines.append(entry.model_dump_json(exclude_none=True))
        return "\n".join(lines) + ("\n" if lines else "")
