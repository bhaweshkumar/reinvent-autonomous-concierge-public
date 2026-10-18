"""Protocols the REST client depends on without reaching into higher layers."""

from datetime import datetime
from typing import Protocol


class OperationQuotaGovernor(Protocol):
    """Coordinate a single event's AWS operation quotas."""

    async def acquire(
        self,
        operation: str,
        units: int = 1,
        timeout_seconds: float = 60,
        background: bool = False,
    ) -> bool: ...

    async def available_at(
        self, operation: str, units: int = 1, background: bool = False
    ) -> datetime: ...

    async def block_for(self, operation: str, retry_after_seconds: float) -> None: ...
