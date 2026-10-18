"""SQLite catalog CRUD (SPEC-18 §18.1 split of ingestion/service.py).

Owns the ``sessions`` and ``catalog_meta`` tables. Holds no vector-index or sync state;
``service.py`` composes this with ``vector_index.py`` and ``sync.py``.
"""

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import aiosqlite

from ..models import Session, SessionSummary, utc_now

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id        VARCHAR(64) PRIMARY KEY,
    title             TEXT NOT NULL,
    description       TEXT,
    level             INTEGER CHECK(level IN (100, 200, 300, 400)),
    track             VARCHAR(100),
    venue_id          VARCHAR(50) NOT NULL,
    room_name         VARCHAR(100),
    start_time        TIMESTAMP NOT NULL,
    end_time          TIMESTAMP NOT NULL,
    capacity          INTEGER,
    reserved_count    INTEGER,
    status            VARCHAR(20) CHECK(status IN ('OPEN', 'FULL', 'CANCELLED')),
    speaker_names     TEXT,
    is_hero_speaker   BOOLEAN DEFAULT FALSE,
    seat_availability VARCHAR(20),
    is_reservable     BOOLEAN,
    session_code      VARCHAR(32),
    session_type      VARCHAR(64),
    features_json     TEXT,
    topics_json       TEXT,
    roles_json        TEXT,
    services_json     TEXT,
    venue_source      VARCHAR(16),
    floor             INTEGER,
    zone              VARCHAR(64),
    repeat_group      VARCHAR(32),
    content_hash      CHAR(64),
    updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""
_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_sessions_level_venue ON sessions(level, venue_id)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_times ON sessions(start_time, end_time)",
)
# SPEC-20: semantic vectors, keyed by session so a seat-only change never re-embeds.
_VECTOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_vectors (
    session_id VARCHAR(64) PRIMARY KEY,
    model_id   VARCHAR(200) NOT NULL,
    text_hash  CHAR(64) NOT NULL,
    vector     BLOB NOT NULL
)
"""
_VECTOR_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_session_vectors_model ON session_vectors(model_id)",
)
# Columns added after the first release; added to older databases on startup.
_ADDED_COLUMNS = {
    "seat_availability": "VARCHAR(20)",
    "is_reservable": "BOOLEAN",
    "session_code": "VARCHAR(32)",
    "session_type": "VARCHAR(64)",
    "features_json": "TEXT",
    "topics_json": "TEXT",
    "roles_json": "TEXT",
    "services_json": "TEXT",
    "venue_source": "VARCHAR(16)",
    "floor": "INTEGER",
    "zone": "VARCHAR(64)",
    "repeat_group": "VARCHAR(32)",
    "content_hash": "CHAR(64)",
}
_COLUMNS = (
    "session_id",
    "title",
    "description",
    "level",
    "track",
    "venue_id",
    "room_name",
    "start_time",
    "end_time",
    "capacity",
    "reserved_count",
    "status",
    "speaker_names",
    "is_hero_speaker",
    "seat_availability",
    "is_reservable",
    "session_code",
    "session_type",
    "features_json",
    "topics_json",
    "roles_json",
    "services_json",
    "venue_source",
    "floor",
    "zone",
    "repeat_group",
    "content_hash",
    "updated_at",
)
# Built from the constant column list only.
_UPSERT_SQL = (
    f"INSERT OR REPLACE INTO sessions ({', '.join(_COLUMNS)}) "  # noqa: S608
    f"VALUES ({', '.join('?' for _ in _COLUMNS)})"
)

_JSON_LIST_COLUMNS = {
    "features_json": "features",
    "topics_json": "topics",
    "roles_json": "roles",
    "services_json": "services",
}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def content_hash(session: Session) -> str:
    return _sha256(session.model_dump_json(exclude={"updated_at", "start_local", "end_local"}))


def session_from_row(row: Dict[str, Any]) -> Session:
    # NULL columns (rows written before a column existed) fall back to the model defaults.
    fields = {key: row[key] for key in Session.model_fields if key in row and row[key] is not None}
    fields["is_hero_speaker"] = bool(row.get("is_hero_speaker"))
    if row.get("is_reservable") is not None:
        fields["is_reservable"] = bool(row["is_reservable"])
    for column, field in _JSON_LIST_COLUMNS.items():
        raw = row.get(column)
        if raw:
            fields[field] = json.loads(raw)
    return Session(**fields)


def _row(session: Session, updated_at: datetime) -> Tuple[Any, ...]:
    return (
        session.session_id,
        session.title,
        session.description,
        session.level,
        session.track,
        session.venue_id,
        session.room_name,
        session.start_time.isoformat(),
        session.end_time.isoformat(),
        session.capacity,
        session.reserved_count,
        session.status,
        session.speaker_names,
        session.is_hero_speaker,
        session.seat_availability,
        session.is_reservable,
        session.session_code,
        session.session_type,
        json.dumps(session.features),
        json.dumps(session.topics),
        json.dumps(session.roles),
        json.dumps(session.services),
        session.venue_source,
        session.floor,
        session.zone,
        session.repeat_group,
        content_hash(session),
        updated_at.isoformat(),
    )


class CatalogStore:
    """Owns the ``sessions``/``catalog_meta`` tables; no vector-index or sync knowledge."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def ensure_schema(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(_SCHEMA)
            await db.execute(
                "CREATE TABLE IF NOT EXISTS catalog_meta "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            await db.execute(_VECTOR_SCHEMA)
            cursor = await db.execute("PRAGMA table_info(sessions)")
            existing = {row[1] for row in await cursor.fetchall()}
            for column, column_type in _ADDED_COLUMNS.items():
                if column not in existing:
                    await db.execute(f"ALTER TABLE sessions ADD COLUMN {column} {column_type}")
            for statement in _INDEXES + _VECTOR_INDEXES:
                await db.execute(statement)
            await db.commit()

    async def set_meta(self, key: str, value: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO catalog_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            await db.commit()

    async def get_meta(self, key: str) -> Any:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT value FROM catalog_meta WHERE key = ?", (key,))
            row = await cursor.fetchone()
        return row[0] if row else None

    async def replace_all(self, sessions: Sequence[Session]) -> None:
        """Delete every row, then insert ``sessions`` (used by a full sync)."""
        now = utc_now()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM sessions")
            await db.executemany(_UPSERT_SQL, [_row(s, now) for s in sessions])
            await db.commit()

    async def upsert_changed(self, sessions: Sequence[Session]) -> List[Session]:
        """Upsert rows whose content changed; returns the changed sessions."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT session_id, content_hash FROM sessions")
            known = {row[0]: row[1] for row in await cursor.fetchall()}
            changed = [s for s in sessions if known.get(s.session_id) != content_hash(s)]
            if changed:
                now = utc_now()
                await db.executemany(_UPSERT_SQL, [_row(s, now) for s in changed])
                await db.commit()
        return changed

    async def mark_canceled(self, session_ids: Sequence[str]) -> None:
        if not session_ids:
            return
        now = utc_now().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.executemany(
                "UPDATE sessions SET status = 'CANCELLED', content_hash = NULL, updated_at = ? "
                "WHERE session_id = ?",
                [(now, sid) for sid in session_ids],
            )
            await db.commit()

    async def uncanceled_session_ids(self) -> List[str]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT session_id FROM sessions WHERE status IS NOT 'CANCELLED'"
            )
            return [row[0] for row in await cursor.fetchall()]

    async def get_local(self, session_id: str) -> Any:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
            row = await cursor.fetchone()
        return session_from_row(dict(row)) if row else None

    async def repeat_group_counts(self, groups: Sequence[str]) -> Dict[str, int]:
        if not groups:
            return {}
        placeholders = ", ".join("?" for _ in groups)
        sql = (
            "SELECT repeat_group, COUNT(*) FROM sessions "  # noqa: S608 - placeholders only
            f"WHERE status != 'CANCELLED' AND repeat_group IN ({placeholders}) "
            "GROUP BY repeat_group"
        )
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(sql, tuple(groups))
            rows = await cursor.fetchall()
        return {str(group): int(count) for group, count in rows}

    async def list_local(self, limit: int = 250) -> List[Session]:
        if limit < 1:
            raise ValueError("limit must be positive")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM sessions ORDER BY session_id LIMIT ?", (limit,)
            )
            rows = await cursor.fetchall()
        return [session_from_row(dict(row)) for row in rows]

    async def load_all(self) -> List[Session]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM sessions ORDER BY session_id")
            return [session_from_row(dict(row)) for row in await cursor.fetchall()]

    async def query_open(self, where: List[str], params: List[Any]) -> Dict[str, Dict[str, Any]]:
        """Rows matching ``where`` (already anchored to ``status = 'OPEN'``), by session ID."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            sql = f"SELECT * FROM sessions WHERE {' AND '.join(where)}"  # noqa: S608
            cursor = await db.execute(sql, params)
            return {row["session_id"]: dict(row) for row in await cursor.fetchall()}

    async def catalog_revision(self) -> str:
        """Fingerprint what the vector index depends on: embedded text and cancellation.

        Seat bands and counts are deliberately excluded; they change often and never
        affect vectors.
        """
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT session_id, title, track, level, description, speaker_names, "
                "status = 'CANCELLED' FROM sessions ORDER BY session_id"
            )
            rows = await cursor.fetchall()
        payload = json.dumps(rows, ensure_ascii=True, separators=(",", ":"))
        return _sha256(payload)

    # ------------------------------------------------------------------ vectors (SPEC-20)

    async def vector_hash(self, session_id: str) -> Any:
        """Return ``(model_id, text_hash)`` for a session's stored vector, or ``None``."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT model_id, text_hash FROM session_vectors WHERE session_id = ?",
                (session_id,),
            )
            row = await cursor.fetchone()
        return (row[0], row[1]) if row else None

    async def upsert_vectors(self, rows: Sequence[Tuple[str, str, str, bytes]]) -> None:
        """Insert or replace vector rows. ``INSERT OR REPLACE`` reassigns the rowid on an
        update, which ``vectors_max_rowid`` relies on as a cheap staleness signal."""
        if not rows:
            return
        async with aiosqlite.connect(self.db_path) as db:
            await db.executemany(
                "INSERT OR REPLACE INTO session_vectors (session_id, model_id, text_hash, vector) "
                "VALUES (?, ?, ?, ?)",
                rows,
            )
            await db.commit()

    async def clear_vectors(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM session_vectors")
            await db.commit()

    async def vectors_max_rowid(self, model_id: str) -> int:
        """Highest rowid among vectors for ``model_id``; 0 when there are none.

        Bumps on every insert or replace, so callers can detect "something changed" with one
        cheap query instead of re-reading every vector on each search.
        """
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT COALESCE(MAX(rowid), 0) FROM session_vectors WHERE model_id = ?",
                (model_id,),
            )
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def all_vectors(self, model_id: str) -> List[Tuple[str, bytes]]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT session_id, vector FROM session_vectors WHERE model_id = ? "
                "ORDER BY session_id",
                (model_id,),
            )
            rows = await cursor.fetchall()
        return [(row[0], row[1]) for row in rows]

    @staticmethod
    def summary_from_row(row: Dict[str, Any], score: float):
        fields = {key: row[key] for key in SessionSummary.model_fields if key in row}
        fields["is_hero_speaker"] = bool(row["is_hero_speaker"])
        return SessionSummary(**fields, relevance_score=score)
