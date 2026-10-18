"""Persistence for generated itinerary plans (SPEC-18 §18.1 split of itinerary.py)."""

from typing import Optional, Union
from uuid import UUID

from .plan_models import ItineraryPlan, PlanSet
from .state_store import PlannerStateStore


class ItineraryPlanStore:
    """Persists immutable plan JSON in the planner state database."""

    def __init__(self, store: PlannerStateStore) -> None:
        self._store = store

    async def save_plan_set(self, plan_set: PlanSet) -> None:
        async with self._store._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            for plan in plan_set.plans:
                await connection.execute(
                    "INSERT OR REPLACE INTO itinerary_plans "
                    "(plan_id, event_id, profile_id, source_snapshot_fingerprint, "
                    "plan_set_id, strategy, plan_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(plan.plan_id),
                        plan.event_id,
                        str(plan.profile_id),
                        plan.source_snapshot_fingerprint,
                        str(plan.plan_set_id),
                        plan.strategy.value,
                        plan.model_dump_json(),
                        plan.generated_at.isoformat(),
                    ),
                )
            await connection.commit()

    async def get(self, plan_id: Union[UUID, str]) -> Optional[ItineraryPlan]:
        async with self._store._connect() as connection:
            row = await (
                await connection.execute(
                    "SELECT plan_json FROM itinerary_plans WHERE plan_id = ?",
                    (str(plan_id),),
                )
            ).fetchone()
        return ItineraryPlan.model_validate_json(row[0]) if row else None
