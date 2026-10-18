"""Shared AWS Events per-operation quota accounting.

The AWS Events limits below are per attendee, per rolling minute.  SQLite makes the
governor safe for the local CLI, UI, MCP server, and workers when they run in separate
processes.  Only operation names and aggregate units are persisted; credentials never
enter planner state.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Protocol, Tuple

from ..api.ports import OperationQuotaGovernor
from .state_store import PlannerStateStore

__all__ = [
    "OPERATION_QUOTAS",
    "UNKNOWN_OPERATION_LIMIT_PER_MINUTE",
    "WINDOW",
    "Clock",
    "InMemoryOperationQuotaGovernor",
    "OperationQuota",
    "OperationQuotaGovernor",
    "SQLiteOperationQuotaGovernor",
    "SystemClock",
]

WINDOW = timedelta(minutes=1)
UNKNOWN_OPERATION_LIMIT_PER_MINUTE = 30


@dataclass(frozen=True)
class OperationQuota:
    """Documented quota and accounting unit for one AWS Events operation."""

    limit_per_minute: int
    unit: str


# AWS Events operation quotas. Request-based operations consume one unit per request;
# bulk reservation and favorite operations consume one unit per distinct session ID.
OPERATION_QUOTAS: Dict[str, OperationQuota] = {
    "GetSession": OperationQuota(120, "request"),
    "ListSessions": OperationQuota(120, "request"),
    "GetSchedule": OperationQuota(60, "request"),
    "ReserveSessions": OperationQuota(30, "session ID"),
    "CancelReservation": OperationQuota(30, "request"),
    "AssociateFavorites": OperationQuota(30, "session ID"),
    "DisassociateFavorite": OperationQuota(30, "request"),
    "CreatePersonalTime": OperationQuota(30, "request"),
    "UpdatePersonalTime": OperationQuota(30, "request"),
    "DeletePersonalTime": OperationQuota(30, "request"),
}


class Clock(Protocol):
    """Clock abstraction used to make rolling-window quota behavior deterministic."""

    def now(self) -> datetime: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


def _validate_units(units: int) -> None:
    if isinstance(units, bool) or not isinstance(units, int) or units <= 0:
        raise ValueError("quota units must be a positive integer")


def _validate_reserve(interactive_reserve_units: int) -> None:
    if (
        isinstance(interactive_reserve_units, bool)
        or not isinstance(interactive_reserve_units, int)
        or interactive_reserve_units < 0
    ):
        raise ValueError("interactive reserve must be a non-negative integer")


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return _normalize_datetime(value).isoformat()


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


class _QuotaGovernorBase:
    def __init__(
        self,
        event_id: str,
        *,
        quotas: Dict[str, OperationQuota] = OPERATION_QUOTAS,
        unknown_limit_per_minute: int = UNKNOWN_OPERATION_LIMIT_PER_MINUTE,
        interactive_reserve_units: int = 0,
        clock: Optional[Clock] = None,
    ) -> None:
        if not event_id:
            raise ValueError("event_id must not be empty")
        if unknown_limit_per_minute <= 0:
            raise ValueError("unknown operation limit must be positive")
        _validate_reserve(interactive_reserve_units)
        self.event_id = event_id
        self.quotas = quotas
        self.unknown_limit_per_minute = unknown_limit_per_minute
        self.interactive_reserve_units = interactive_reserve_units
        self.clock = clock or SystemClock()

    def _limit(self, operation: str, background: bool) -> int:
        limit = self.quotas.get(
            operation, OperationQuota(UNKNOWN_OPERATION_LIMIT_PER_MINUTE, "request")
        ).limit_per_minute
        if operation not in self.quotas:
            limit = self.unknown_limit_per_minute
        return max(0, limit - self.interactive_reserve_units) if background else limit

    def _validate_request(self, operation: str, units: int) -> None:
        if not operation:
            raise ValueError("operation must not be empty")
        _validate_units(units)

    async def available_at(
        self, operation: str, units: int = 1, background: bool = False
    ) -> datetime:
        raise NotImplementedError

    async def acquire(
        self,
        operation: str,
        units: int = 1,
        timeout_seconds: float = 60,
        background: bool = False,
    ) -> bool:
        self._validate_request(operation, units)
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        if units > self._limit(operation, background):
            return False

        deadline = _normalize_datetime(self.clock.now()) + timedelta(seconds=timeout_seconds)
        while True:
            now = _normalize_datetime(self.clock.now())
            if await self._try_acquire(operation, units, background, now):
                return True
            if now >= deadline:
                return False
            available = await self.available_at(operation, units, background)
            wait_seconds = max(0.001, (available - now).total_seconds())
            wait_seconds = min(wait_seconds, (deadline - now).total_seconds())
            await self.clock.sleep(wait_seconds)

    async def _try_acquire(
        self, operation: str, units: int, background: bool, now: datetime
    ) -> bool:
        raise NotImplementedError


class SQLiteOperationQuotaGovernor(_QuotaGovernorBase):
    """SQLite-backed quota governor safe across local processes."""

    def __init__(
        self,
        store: PlannerStateStore,
        event_id: str,
        *,
        quotas: Optional[Dict[str, OperationQuota]] = None,
        unknown_limit_per_minute: int = UNKNOWN_OPERATION_LIMIT_PER_MINUTE,
        interactive_reserve_units: int = 0,
        clock: Optional[Clock] = None,
    ) -> None:
        super().__init__(
            event_id,
            quotas=quotas if quotas is not None else OPERATION_QUOTAS,
            unknown_limit_per_minute=unknown_limit_per_minute,
            interactive_reserve_units=interactive_reserve_units,
            clock=clock,
        )
        self.store = store

    async def _try_acquire(
        self, operation: str, units: int, background: bool, now: datetime
    ) -> bool:
        cutoff = _timestamp(now - WINDOW)
        now_value = _timestamp(now)
        limit = self._limit(operation, background)
        async with self.store._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                await connection.execute(
                    """
                    DELETE FROM quota_events
                    WHERE event_id = ? AND operation = ? AND consumed_at <= ?
                    """,
                    (self.event_id, operation, cutoff),
                )
                block = await (
                    await connection.execute(
                        """
                        SELECT blocked_until FROM quota_blocks
                        WHERE event_id = ? AND operation = ?
                        """,
                        (self.event_id, operation),
                    )
                ).fetchone()
                blocked = block is not None and _parse_timestamp(block[0]) > now
                used_row = await (
                    await connection.execute(
                        """
                        SELECT COALESCE(SUM(units), 0) FROM quota_events
                        WHERE event_id = ? AND operation = ?
                        """,
                        (self.event_id, operation),
                    )
                ).fetchone()
                if used_row is None:
                    raise RuntimeError("quota usage query returned no row")
                used = used_row[0]
                if blocked or used + units > limit:
                    await connection.commit()
                    return False
                await connection.execute(
                    """
                    INSERT INTO quota_events (event_id, operation, consumed_at, units)
                    VALUES (?, ?, ?, ?)
                    """,
                    (self.event_id, operation, now_value, units),
                )
            except Exception:
                await connection.rollback()
                raise
            await connection.commit()
        return True

    async def available_at(
        self, operation: str, units: int = 1, background: bool = False
    ) -> datetime:
        self._validate_request(operation, units)
        now = _normalize_datetime(self.clock.now())
        limit = self._limit(operation, background)
        if units > limit:
            return datetime.max.replace(tzinfo=timezone.utc)
        cutoff = _timestamp(now - WINDOW)
        async with self.store._connect() as connection:
            block = await (
                await connection.execute(
                    """
                    SELECT blocked_until FROM quota_blocks
                    WHERE event_id = ? AND operation = ?
                    """,
                    (self.event_id, operation),
                )
            ).fetchone()
            rows = await (
                await connection.execute(
                    """
                    SELECT consumed_at, units FROM quota_events
                    WHERE event_id = ? AND operation = ? AND consumed_at > ?
                    ORDER BY consumed_at ASC
                    """,
                    (self.event_id, operation, cutoff),
                )
            ).fetchall()

        available = _parse_timestamp(block[0]) if block is not None else now
        available = max(now, available)
        used = sum(row[1] for row in rows)
        required_release = max(0, used + units - limit)
        released = 0
        for consumed_at, consumed_units in rows:
            released += consumed_units
            if released >= required_release:
                available = max(available, _parse_timestamp(consumed_at) + WINDOW)
                break
        return available

    async def block_for(self, operation: str, retry_after_seconds: float) -> None:
        self._validate_request(operation, 1)
        if retry_after_seconds < 0:
            raise ValueError("retry_after_seconds must be non-negative")
        until = _normalize_datetime(self.clock.now()) + timedelta(seconds=retry_after_seconds)
        async with self.store._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                existing = await (
                    await connection.execute(
                        """
                        SELECT blocked_until FROM quota_blocks
                        WHERE event_id = ? AND operation = ?
                        """,
                        (self.event_id, operation),
                    )
                ).fetchone()
                if existing is not None:
                    until = max(until, _parse_timestamp(existing[0]))
                await connection.execute(
                    """
                    INSERT INTO quota_blocks (event_id, operation, blocked_until)
                    VALUES (?, ?, ?)
                    ON CONFLICT(event_id, operation) DO UPDATE SET
                        blocked_until = excluded.blocked_until
                    """,
                    (self.event_id, operation, _timestamp(until)),
                )
            except Exception:
                await connection.rollback()
                raise
            await connection.commit()


class InMemoryOperationQuotaGovernor(_QuotaGovernorBase):
    """Deterministic in-process governor for unit tests and isolated adapters."""

    def __init__(
        self,
        event_id: str,
        *,
        quotas: Optional[Dict[str, OperationQuota]] = None,
        unknown_limit_per_minute: int = UNKNOWN_OPERATION_LIMIT_PER_MINUTE,
        interactive_reserve_units: int = 0,
        clock: Optional[Clock] = None,
    ) -> None:
        super().__init__(
            event_id,
            quotas=quotas if quotas is not None else OPERATION_QUOTAS,
            unknown_limit_per_minute=unknown_limit_per_minute,
            interactive_reserve_units=interactive_reserve_units,
            clock=clock,
        )
        self._events: Dict[str, List[Tuple[datetime, int]]] = {}
        self._blocks: Dict[str, datetime] = {}
        self._lock: asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def _try_acquire(
        self, operation: str, units: int, background: bool, now: datetime
    ) -> bool:
        async with self._get_lock():
            events = self._events.get(operation, [])
            events = [(at, count) for at, count in events if at + WINDOW > now]
            self._events[operation] = events
            if self._blocks.get(operation, now) > now:
                return False
            if sum(count for _at, count in events) + units > self._limit(operation, background):
                return False
            events.append((now, units))
            return True

    async def available_at(
        self, operation: str, units: int = 1, background: bool = False
    ) -> datetime:
        self._validate_request(operation, units)
        now = _normalize_datetime(self.clock.now())
        limit = self._limit(operation, background)
        if units > limit:
            return datetime.max.replace(tzinfo=timezone.utc)
        async with self._get_lock():
            events = [
                (at, count) for at, count in self._events.get(operation, []) if at + WINDOW > now
            ]
            available = max(now, self._blocks.get(operation, now))
            required_release = max(0, sum(count for _at, count in events) + units - limit)
            released = 0
            for consumed_at, consumed_units in events:
                released += consumed_units
                if released >= required_release:
                    available = max(available, consumed_at + WINDOW)
                    break
            return available

    async def block_for(self, operation: str, retry_after_seconds: float) -> None:
        self._validate_request(operation, 1)
        if retry_after_seconds < 0:
            raise ValueError("retry_after_seconds must be non-negative")
        until = _normalize_datetime(self.clock.now()) + timedelta(seconds=retry_after_seconds)
        async with self._get_lock():
            self._blocks[operation] = max(self._blocks.get(operation, until), until)
