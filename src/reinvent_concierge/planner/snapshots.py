"""Durable schedule snapshot persistence."""

import json
from collections.abc import Sequence
from typing import List, Optional, cast

from ..models import AttendeeSchedule
from .models import ScheduleSnapshot
from .state_store import PlannerStateStore


class ScheduleSnapshotService:
    """Persists canonical schedule snapshots with bounded event history."""

    def __init__(self, store: PlannerStateStore) -> None:
        self._store = store

    async def capture(self, event_id: str, schedule: AttendeeSchedule) -> ScheduleSnapshot:
        snapshot = ScheduleSnapshot.from_schedule(event_id, schedule)
        payload = snapshot.model_dump(mode="json")

        async with self._store._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            await connection.execute(
                """
                INSERT INTO schedule_snapshots (
                    snapshot_id,
                    event_id,
                    captured_at,
                    booked_session_ids_json,
                    favorite_session_ids_json,
                    personal_time_json,
                    fingerprint
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["snapshot_id"],
                    payload["event_id"],
                    payload["captured_at"],
                    json.dumps(payload["booked_session_ids"], separators=(",", ":")),
                    json.dumps(payload["favorite_session_ids"], separators=(",", ":")),
                    json.dumps(payload["personal_time"], separators=(",", ":")),
                    payload["fingerprint"],
                ),
            )
            await connection.execute(
                """
                DELETE FROM schedule_snapshots
                WHERE snapshot_id IN (
                    SELECT snapshot_id
                    FROM schedule_snapshots
                    WHERE event_id = ?
                    ORDER BY rowid DESC
                    LIMIT -1 OFFSET 50
                )
                """,
                (event_id,),
            )
            await connection.commit()

        return snapshot

    async def latest(self, event_id: str) -> Optional[ScheduleSnapshot]:
        snapshots = await self.recent(event_id, limit=1)
        return snapshots[0] if snapshots else None

    async def recent(self, event_id: str, limit: int = 50) -> List[ScheduleSnapshot]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")

        async with self._store._connect() as connection:
            rows = await (
                await connection.execute(
                    """
                    SELECT
                        snapshot_id,
                        event_id,
                        captured_at,
                        booked_session_ids_json,
                        favorite_session_ids_json,
                        personal_time_json,
                        fingerprint
                    FROM schedule_snapshots
                    WHERE event_id = ?
                    ORDER BY rowid DESC
                    LIMIT ?
                    """,
                    (event_id, min(limit, 50)),
                )
            ).fetchall()

        return [self._snapshot_from_row(cast(Sequence[object], row)) for row in rows]

    @staticmethod
    def _snapshot_from_row(row: Sequence[object]) -> ScheduleSnapshot:
        return ScheduleSnapshot.model_validate(
            {
                "snapshot_id": row[0],
                "event_id": row[1],
                "captured_at": row[2],
                "booked_session_ids": _json_column(row[3]),
                "favorite_session_ids": _json_column(row[4]),
                "personal_time": _json_column(row[5]),
                "fingerprint": row[6],
            }
        )


def _json_column(value: object) -> object:
    if isinstance(value, (str, bytes, bytearray)):
        return json.loads(value)
    raise RuntimeError("JSON column is not text or bytes")
