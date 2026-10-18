"""Test data factories shared by unit and integration tests."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np

from reinvent_concierge.booking import ChangeSetApprovals, ScheduleReader
from reinvent_concierge.booking.service import ScheduleWriteService
from reinvent_concierge.config import Config
from reinvent_concierge.models import AttendeeSchedule, Event, PersonalTimeBlock, Session
from reinvent_concierge.planner import ApplyJournalStore, PlannerStateStore, ScheduleSnapshotService

API_BASE = "https://api.test/v1"
TOKEN_URL = "https://oauth.test/oauth2/token"
EVENT_ID = "reinvent2026"
TOKEN = "tok-secret-7f3a9c"
REFRESH_TOKEN = "refresh-secret-11b2"
UTC = timezone.utc
DAY = datetime(2026, 12, 1, tzinfo=UTC)


def at(hour: int, minute: int = 0) -> datetime:
    return DAY.replace(hour=hour, minute=minute)


def make_session(
    session_id: str = "SEC301",
    venue: str = "Venetian",
    start: Optional[datetime] = None,
    minutes: int = 60,
    **overrides: Any,
) -> Session:
    start = start or at(18)
    fields: Dict[str, Any] = {
        "session_id": session_id,
        "title": f"Session {session_id}",
        "level": 300,
        "venue_id": venue,
        "start_time": start,
        "end_time": start + timedelta(minutes=minutes),
        "status": "OPEN",
    }
    fields.update(overrides)
    return Session(**fields)


def make_schedule(
    *booked: Session, personal_time: Iterable[PersonalTimeBlock] = ()
) -> AttendeeSchedule:
    return AttendeeSchedule(booked=list(booked), personal_time=list(personal_time))


def wire_session(
    session_id: str = "SEC301",
    venue: str = "Venetian",
    date: str = "2026-12-01",
    time: str = "10:00",
    length: str = "60",
    **overrides: Any,
) -> Dict[str, Any]:
    """A session as the AWS Events API returns it (times are Las Vegas local)."""
    payload: Dict[str, Any] = {
        "sessionId": session_id,
        "title": f"Session {session_id}",
        "abstract": "An abstract",
        "level": "300 - Advanced",
        "venue": venue,
        "room": "Room 1",
        "sessionTime": {"date": date, "time": time, "length": length},
        "seatAvailability": "available",
        "isReservable": True,
        "speakers": [{"name": "Ada Lovelace"}],
        "tracks": ["AI/ML"],
    }
    payload.update(overrides)
    return payload


def wire_schedule(
    reserved: Iterable[str] = (),
    favorites: Iterable[str] = (),
    personal_time: Iterable[Dict[str, Any]] = (),
) -> Dict[str, Any]:
    return {
        "schedule": {
            "reserved": list(reserved),
            "favorites": list(favorites),
            "personalTime": list(personal_time),
        }
    }


def wire_bulk(successful: Iterable[str] = (), failed: Iterable[Dict[str, Any]] = ()):
    return {"result": {"successful": list(successful), "failed": list(failed)}}


def event(event_id: str = EVENT_ID) -> Event:
    return Event(event_id=event_id, name="AWS re:Invent 2026")


async def build_approvals(
    config: Config, client: Any, resolver: Any = None
) -> Tuple[ChangeSetApprovals, ScheduleReader]:
    """Real approval-gated write path on the config's planner DB, wired as app_context does."""
    store = PlannerStateStore(config.planner_db_path)
    await store.initialize()
    reader = ScheduleReader(client, resolver)
    journal = ApplyJournalStore(store)
    writer = ScheduleWriteService(journal, client, reader)
    return ChangeSetApprovals(journal, ScheduleSnapshotService(store), reader, writer), reader


def read_audit(db_path: Path) -> List[Dict[str, Any]]:
    """Every ``audit_events`` row, oldest first (SPEC-18 §18.3: audit moved into SQLite)."""
    if not db_path.exists():
        return []
    connection = sqlite3.connect(db_path)
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT * FROM audit_events ORDER BY id ASC").fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        connection.close()
    entries = []
    for row in rows:
        raw = dict(row)
        arguments_json = raw.pop("arguments_json")
        result_json = raw.pop("result_json")
        raw["arguments"] = json.loads(arguments_json) if arguments_json else None
        raw["result"] = json.loads(result_json) if result_json else None
        entries.append(raw)
    return entries


def audit_dump_text(db_path: Path) -> str:
    """Every audit row rendered as text, for "token never appears" substring checks."""
    return json.dumps(read_audit(db_path))


class FakeEmbedder:
    """Deterministic embedder: one axis per known keyword, one for everything else.

    ``model_id`` matches what ``FastEmbedEmbedder`` would derive from the default config
    value, so ``VectorIndex.load()``'s pure (no-construction) model_id check still lines
    up with vectors this fake wrote.
    """

    VOCAB = ("bedrock", "generative", "security", "serverless")
    model_id = "sentence-transformers/all-MiniLM-L6-v2"

    def embed(self, texts):
        rows: List[List[float]] = []
        for text in texts:
            lowered = text.lower()
            row = [float(word in lowered) for word in self.VOCAB]
            row.append(0.1)
            rows.append(row)
        vectors = np.asarray(rows, dtype=np.float32)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return vectors / norms
