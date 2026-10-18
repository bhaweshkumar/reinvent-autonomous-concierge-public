"""Hosted read-only demo (SPEC-21): simulated schedule, allowlisted anonymous catalog reads.

Nothing in this package can hold an AWS Events credential or attempt a write against the
real API — see ``simulated_schedule.py`` and ``catalog_client.py``.
"""

from .catalog_client import DemoCatalogAllowlistError, DemoCatalogClient
from .simulated_schedule import SimulatedScheduleClient

__all__ = [
    "DemoCatalogAllowlistError",
    "DemoCatalogClient",
    "SimulatedScheduleClient",
]
