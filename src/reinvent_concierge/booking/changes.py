"""Change models shared by planning and the booking write path (SPEC-17)."""

from datetime import datetime
from enum import Enum
from typing import Optional, Tuple
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from ..planner.models import ApplyOperation, ApplyRunState


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ChangeKind(str, Enum):
    FAVORITE = "FAVORITE"
    UNFAVORITE = "UNFAVORITE"
    RESERVE = "RESERVE"
    SWAP = "SWAP"
    CANCEL = "CANCEL"
    CREATE_TRANSIT = "CREATE_TRANSIT"
    REMOVE_TRANSIT = "REMOVE_TRANSIT"


DESTRUCTIVE_KINDS = frozenset({ChangeKind.SWAP, ChangeKind.CANCEL, ChangeKind.REMOVE_TRANSIT})


class PlanChange(_Model):
    kind: ChangeKind
    session_id: Optional[str] = None
    blocking_session_ids: Tuple[str, ...] = ()
    personal_time_id: Optional[str] = None
    title: Optional[str] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    location: Optional[str] = None
    description: Optional[str] = None
    risk: str


class ApplyResult(_Model):
    apply_run_id: UUID
    state: ApplyRunState
    confirmed: Tuple[str, ...] = ()
    refused: Tuple[str, ...] = ()
    unexpected: Tuple[str, ...] = ()
    compensation: Tuple[str, ...] = ()
    manual_actions: Tuple[str, ...] = ()
    operations: Tuple[ApplyOperation, ...] = ()
    deferred: Tuple[str, ...] = ()
    refusal_codes: Tuple[Tuple[str, str], ...] = ()
