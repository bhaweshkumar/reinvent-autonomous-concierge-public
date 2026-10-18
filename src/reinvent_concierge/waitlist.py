"""Waitlist shared by the MCP agent, the UI and the SeatSnapperWorker.

Rows live in the planner SQLite database's ``waitlist_entries`` table (SPEC-18 §18.3),
replacing the former ``waitlist.json`` + lock-file pair. SQLite's own locking keeps
concurrent read-modify-write safe without ``fcntl``, which also closes the Windows
locking gap the old file-based store had.
"""

import logging
import sqlite3
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .models import WaitlistEntry

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS waitlist_entries (
    session_id    TEXT PRIMARY KEY,
    priority      INTEGER NOT NULL,
    added_at      TEXT NOT NULL,
    last_checked  TEXT,
    status        TEXT NOT NULL,
    enabled       INTEGER NOT NULL,
    title         TEXT,
    venue_id      TEXT
)
"""


def _sort_key(entry: WaitlistEntry):
    return (entry.priority, entry.added_at)


def _entry_from_row(row: sqlite3.Row) -> WaitlistEntry:
    return WaitlistEntry(
        session_id=row["session_id"],
        priority=row["priority"],
        added_at=row["added_at"],
        last_checked=row["last_checked"],
        status=row["status"],
        enabled=bool(row["enabled"]),
        title=row["title"],
        venue_id=row["venue_id"],
    )


class WaitlistStore:
    """CRUD over ``waitlist_entries``; entries are returned by priority, then age."""

    def __init__(self, path: Path):
        self.path = Path(path).expanduser()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute(_SCHEMA)
        connection.row_factory = sqlite3.Row
        return connection

    def entries(self) -> List[WaitlistEntry]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM waitlist_entries").fetchall()
        return sorted((_entry_from_row(row) for row in rows), key=_sort_key)

    def add(
        self,
        session_id: str,
        priority: int = 50,
        title: Optional[str] = None,
        venue_id: Optional[str] = None,
    ) -> WaitlistEntry:
        """Add an entry, or re-prioritize and re-enable an existing one."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM waitlist_entries WHERE session_id = ?", (session_id,)
            ).fetchone()
            if row is not None:
                existing = _entry_from_row(row)
                existing.priority = priority
                existing.enabled = True
                existing.title = title or existing.title
                existing.venue_id = venue_id or existing.venue_id
                entry = existing
            else:
                entry = WaitlistEntry(
                    session_id=session_id, priority=priority, title=title, venue_id=venue_id
                )
            connection.execute(
                "INSERT OR REPLACE INTO waitlist_entries "
                "(session_id, priority, added_at, last_checked, status, enabled, title, "
                "venue_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.session_id,
                    entry.priority,
                    entry.added_at.isoformat(),
                    entry.last_checked.isoformat() if entry.last_checked else None,
                    entry.status,
                    int(entry.enabled),
                    entry.title,
                    entry.venue_id,
                ),
            )
            connection.commit()
            return entry

    def update(
        self,
        session_id: str,
        *,
        priority: Optional[int] = None,
        enabled: Optional[bool] = None,
    ) -> Optional[WaitlistEntry]:
        """Change priority and/or the auto-snag toggle; returns None if not queued."""
        if priority is not None and not 1 <= priority <= 100:
            raise ValueError("priority must be between 1 and 100")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM waitlist_entries WHERE session_id = ?", (session_id,)
            ).fetchone()
            if row is None:
                return None
            entry = _entry_from_row(row)
            if priority is not None:
                entry.priority = priority
            if enabled is not None:
                entry.enabled = enabled
            connection.execute(
                "UPDATE waitlist_entries SET priority = ?, enabled = ? WHERE session_id = ?",
                (entry.priority, int(entry.enabled), session_id),
            )
            connection.commit()
            return entry

    def remove(self, session_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM waitlist_entries WHERE session_id = ?", (session_id,)
            )
            connection.commit()
            return cursor.rowcount > 0

    def import_entries(self, entries: Iterable[WaitlistEntry]) -> int:
        """Bulk-import entries verbatim (one-time legacy JSON import, SPEC-18 §18.3).

        Skips any ``session_id`` already present, so a retry after a partial failure
        never double-imports.
        """
        imported = 0
        with self._connect() as connection:
            for entry in entries:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO waitlist_entries "
                    "(session_id, priority, added_at, last_checked, status, enabled, "
                    "title, venue_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        entry.session_id,
                        entry.priority,
                        entry.added_at.isoformat(),
                        entry.last_checked.isoformat() if entry.last_checked else None,
                        entry.status,
                        int(entry.enabled),
                        entry.title,
                        entry.venue_id,
                    ),
                )
                imported += cursor.rowcount
            connection.commit()
        return imported

    def apply_poll_results(self, checked: Dict[str, WaitlistEntry], removed: Iterable[str]) -> None:
        """Merge one poll cycle: delete resolved entries, refresh status on the rest.

        Only snapper-owned fields (status, last_checked) are taken from ``checked``;
        priority/enabled edits made during the cycle are untouched.
        """
        with self._connect() as connection:
            connection.executemany(
                "DELETE FROM waitlist_entries WHERE session_id = ?",
                [(session_id,) for session_id in removed],
            )
            connection.executemany(
                "UPDATE waitlist_entries SET status = ?, last_checked = ? WHERE session_id = ?",
                [
                    (
                        entry.status,
                        entry.last_checked.isoformat() if entry.last_checked else None,
                        session_id,
                    )
                    for session_id, entry in checked.items()
                ],
            )
            connection.commit()
