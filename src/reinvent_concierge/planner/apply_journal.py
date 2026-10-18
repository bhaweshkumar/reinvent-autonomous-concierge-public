"""Durable state transitions for schedule apply operations."""

import json
from collections.abc import Callable, Sequence
from typing import Dict, List, Optional, Set, cast
from uuid import UUID

import aiosqlite
from pydantic import JsonValue

from ..models import AttendeeSchedule, utc_now
from .models import (
    ApplyOperation,
    ApplyOperationState,
    ApplyRun,
    ApplyRunState,
    _reject_sensitive_mapping_keys,
)
from .state_store import PlannerStateStore


class InvalidStateTransition(RuntimeError):
    """Raised when a journal transition is illegal or loses a compare-and-set race."""


_RUN_TRANSITIONS: Dict[str, Set[str]] = {
    "PREVIEWED": {"APPROVED", "STALE", "FAILED"},
    "APPROVED": {"APPLYING", "STALE", "FAILED"},
    "APPLYING": {"VERIFYING", "PARTIAL", "COMPENSATING", "FAILED"},
    "VERIFYING": {"APPLIED", "PARTIAL", "COMPENSATING", "FAILED"},
    "PARTIAL": {"COMPENSATING", "FAILED"},
    "COMPENSATING": {"COMPENSATED", "COMPENSATION_INCOMPLETE", "FAILED"},
    "APPLIED": set(),
    "STALE": set(),
    "COMPENSATED": set(),
    "COMPENSATION_INCOMPLETE": set(),
    "FAILED": set(),
}
_TERMINAL_OPERATION_STATES = {
    "CONFIRMED",
    "REFUSED",
    "FAILED",
    "COMPENSATED",
    "COMPENSATION_FAILED",
}
OperationResolver = Callable[[ApplyOperation, AttendeeSchedule], ApplyOperationState]


class ApplyJournalStore:
    """Stores durable apply intent and operation outcomes before remote side effects."""

    def __init__(self, store: PlannerStateStore) -> None:
        self._store = store

    async def create_run(
        self,
        event_id: str,
        profile_id: UUID,
        snapshot_fingerprint: str,
        plan_id: Optional[UUID] = None,
    ) -> ApplyRun:
        run = ApplyRun(
            event_id=event_id,
            profile_id=profile_id,
            plan_id=plan_id,
            snapshot_fingerprint=snapshot_fingerprint,
        )
        payload = run.model_dump(mode="json")

        async with self._store._connect() as connection:
            await connection.execute(
                """
                INSERT INTO apply_runs (
                    run_id,
                    event_id,
                    profile_id,
                    plan_id,
                    snapshot_fingerprint,
                    state,
                    created_at,
                    updated_at,
                    error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["run_id"],
                    payload["event_id"],
                    payload["profile_id"],
                    payload["plan_id"],
                    payload["snapshot_fingerprint"],
                    payload["state"],
                    payload["created_at"],
                    payload["updated_at"],
                    payload["error"],
                ),
            )
            await connection.commit()

        return run

    async def get_run(self, run_id: UUID) -> Optional[ApplyRun]:
        async with self._store._connect() as connection:
            row = await (
                await connection.execute(
                    """
                    SELECT run_id, event_id, profile_id, plan_id, snapshot_fingerprint, state,
                           created_at, updated_at, error
                    FROM apply_runs
                    WHERE run_id = ?
                    """,
                    (str(run_id),),
                )
            ).fetchone()

        return self._run_from_row(cast(Sequence[object], row)) if row else None

    async def latest_preview_for_plan(self, plan_id: UUID) -> Optional[ApplyRun]:
        async with self._store._connect() as connection:
            row = await (
                await connection.execute(
                    """
                    SELECT run_id, event_id, profile_id, plan_id, snapshot_fingerprint, state,
                           created_at, updated_at, error
                    FROM apply_runs
                    WHERE plan_id = ? AND state = 'PREVIEWED'
                    ORDER BY rowid DESC
                    LIMIT 1
                    """,
                    (str(plan_id),),
                )
            ).fetchone()
        return self._run_from_row(cast(Sequence[object], row)) if row else None

    async def add_operation(
        self,
        run_id: UUID,
        order: int,
        operation_type: str,
        request: Dict[str, JsonValue],
    ) -> ApplyOperation:
        operation = ApplyOperation(
            apply_run_id=run_id,
            order=order,
            operation_type=operation_type,
            request=request,
        )
        payload = operation.model_dump(mode="json")

        async with self._store._connect() as connection:
            await connection.execute(
                """
                INSERT INTO apply_operations (
                    operation_id,
                    apply_run_id,
                    operation_order,
                    operation_type,
                    request_json,
                    result_json,
                    state,
                    created_at,
                    updated_at,
                    error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["operation_id"],
                    payload["apply_run_id"],
                    payload["order"],
                    payload["operation_type"],
                    json.dumps(payload["request"], sort_keys=True, separators=(",", ":")),
                    None,
                    payload["state"],
                    payload["created_at"],
                    payload["updated_at"],
                    payload["error"],
                ),
            )
            await connection.commit()

        return operation

    async def operations(self, run_id: UUID) -> List[ApplyOperation]:
        async with self._store._connect() as connection:
            rows = await (
                await connection.execute(
                    """
                    SELECT operation_id, apply_run_id, operation_order, operation_type,
                           request_json, result_json, state, created_at, updated_at, error
                    FROM apply_operations
                    WHERE apply_run_id = ?
                    ORDER BY operation_order ASC
                    """,
                    (str(run_id),),
                )
            ).fetchall()
        return [self._operation_from_row(cast(Sequence[object], row)) for row in rows]

    async def save_preview_payload(self, run_id: UUID, payload: Dict[str, JsonValue]) -> None:
        _reject_sensitive_mapping_keys(payload)
        async with self._store._connect() as connection:
            cursor = await connection.execute(
                """
                UPDATE apply_runs
                SET preview_json = ?
                WHERE run_id = ? AND state = 'PREVIEWED'
                """,
                (json.dumps(payload, sort_keys=True, separators=(",", ":")), str(run_id)),
            )
            if cursor.rowcount != 1:
                raise InvalidStateTransition(f"run {run_id} is not previewed")
            await connection.commit()

    async def preview_payload(self, run_id: UUID) -> Optional[Dict[str, JsonValue]]:
        async with self._store._connect() as connection:
            row = await (
                await connection.execute(
                    "SELECT preview_json FROM apply_runs WHERE run_id = ?", (str(run_id),)
                )
            ).fetchone()
        return cast(Dict[str, JsonValue], _json_column(row[0])) if row and row[0] else None

    async def transition_run(
        self,
        run_id: UUID,
        expected: ApplyRunState,
        target: ApplyRunState,
    ) -> ApplyRun:
        if target not in _RUN_TRANSITIONS.get(expected, set()):
            raise InvalidStateTransition(f"cannot transition run from {expected} to {target}")

        updated_at = utc_now().isoformat()
        async with self._store._connect() as connection:
            cursor = await connection.execute(
                """
                UPDATE apply_runs
                SET state = ?, updated_at = ?
                WHERE run_id = ? AND state = ?
                """,
                (target, updated_at, str(run_id), expected),
            )
            if cursor.rowcount != 1:
                raise InvalidStateTransition(f"run {run_id} is not in expected state {expected}")
            row = await (
                await connection.execute(
                    """
                    SELECT run_id, event_id, profile_id, plan_id, snapshot_fingerprint, state,
                           created_at, updated_at, error
                    FROM apply_runs
                    WHERE run_id = ?
                    """,
                    (str(run_id),),
                )
            ).fetchone()
            await connection.commit()

        if row is None:
            raise RuntimeError(f"run {run_id} disappeared after transition")
        return self._run_from_row(cast(Sequence[object], row))

    async def mark_attempting(self, operation_id: UUID) -> ApplyOperation:
        updated_at = utc_now().isoformat()
        async with self._store._connect() as connection:
            cursor = await connection.execute(
                """
                UPDATE apply_operations
                SET state = 'ATTEMPTING', updated_at = ?
                WHERE operation_id = ? AND state = 'PENDING'
                """,
                (updated_at, str(operation_id)),
            )
            if cursor.rowcount != 1:
                raise InvalidStateTransition(f"operation {operation_id} is not pending")
            row = await self._operation_row(connection, operation_id)
            await connection.commit()

        if row is None:
            raise RuntimeError(f"operation {operation_id} disappeared after transition")
        return self._operation_from_row(row)

    async def record_operation_result(
        self,
        operation_id: UUID,
        terminal_state: ApplyOperationState,
        result: Optional[Dict[str, JsonValue]] = None,
        error: Optional[str] = None,
    ) -> ApplyOperation:
        if terminal_state not in _TERMINAL_OPERATION_STATES:
            raise InvalidStateTransition(
                f"operation result state must be terminal, got {terminal_state}"
            )

        updated_at = utc_now()
        async with self._store._connect() as connection:
            row = await self._operation_row(connection, operation_id)
            if row is None or row[6] != "ATTEMPTING":
                raise InvalidStateTransition(f"operation {operation_id} is not attempting")
            current = self._operation_from_row(row)
            operation = ApplyOperation(
                operation_id=current.operation_id,
                apply_run_id=current.apply_run_id,
                order=current.order,
                operation_type=current.operation_type,
                request=current.request,
                result=result,
                state=terminal_state,
                created_at=current.created_at,
                updated_at=updated_at,
                error=error,
            )
            payload = operation.model_dump(mode="json")
            cursor = await connection.execute(
                """
                UPDATE apply_operations
                SET result_json = ?, state = ?, updated_at = ?, error = ?
                WHERE operation_id = ? AND state = 'ATTEMPTING'
                """,
                (
                    (
                        json.dumps(payload["result"], sort_keys=True, separators=(",", ":"))
                        if payload["result"] is not None
                        else None
                    ),
                    payload["state"],
                    payload["updated_at"],
                    payload["error"],
                    str(operation_id),
                ),
            )
            if cursor.rowcount != 1:
                raise InvalidStateTransition(
                    f"operation {operation_id} changed during reconciliation"
                )
            await connection.commit()

        return operation

    async def reconcile_attempting(
        self,
        run_id: UUID,
        schedule: AttendeeSchedule,
        resolver: OperationResolver,
    ) -> List[ApplyOperation]:
        async with self._store._connect() as connection:
            rows = await (
                await connection.execute(
                    """
                    SELECT operation_id, apply_run_id, operation_order, operation_type,
                           request_json, result_json, state, created_at, updated_at, error
                    FROM apply_operations
                    WHERE apply_run_id = ? AND state = 'ATTEMPTING'
                    ORDER BY operation_order ASC
                    """,
                    (str(run_id),),
                )
            ).fetchall()

        reconciled = []
        for row in rows:
            operation = self._operation_from_row(cast(Sequence[object], row))
            terminal_state = resolver(operation, schedule)
            if terminal_state not in _TERMINAL_OPERATION_STATES:
                raise InvalidStateTransition(
                    f"resolver returned non-terminal state {terminal_state}"
                )
            reconciled.append(
                await self.record_operation_result(operation.operation_id, terminal_state)
            )
        return reconciled

    @staticmethod
    async def _operation_row(
        connection: aiosqlite.Connection,
        operation_id: UUID,
    ) -> Optional[Sequence[object]]:
        row = await (
            await connection.execute(
                """
                SELECT operation_id, apply_run_id, operation_order, operation_type,
                       request_json, result_json, state, created_at, updated_at, error
                FROM apply_operations
                WHERE operation_id = ?
                """,
                (str(operation_id),),
            )
        ).fetchone()
        return cast(Sequence[object], row) if row is not None else None

    @staticmethod
    def _run_from_row(row: Sequence[object]) -> ApplyRun:
        return ApplyRun.model_validate(
            {
                "run_id": row[0],
                "event_id": row[1],
                "profile_id": row[2],
                "plan_id": row[3],
                "snapshot_fingerprint": row[4],
                "state": row[5],
                "created_at": row[6],
                "updated_at": row[7],
                "error": row[8],
            }
        )

    @staticmethod
    def _operation_from_row(row: Sequence[object]) -> ApplyOperation:
        return ApplyOperation.model_validate(
            {
                "operation_id": row[0],
                "apply_run_id": row[1],
                "order": row[2],
                "operation_type": row[3],
                "request": _json_column(row[4]),
                "result": _json_column(row[5]) if row[5] is not None else None,
                "state": row[6],
                "created_at": row[7],
                "updated_at": row[8],
                "error": row[9],
            }
        )


def _json_column(value: object) -> object:
    if isinstance(value, (str, bytes, bytearray)):
        return json.loads(value)
    raise RuntimeError("JSON column is not text or bytes")
