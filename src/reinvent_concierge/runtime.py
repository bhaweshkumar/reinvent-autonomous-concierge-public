"""Single-process runtime scheduling for catalog and seat-monitoring work."""

import asyncio
import errno
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Protocol
from uuid import uuid4

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on platforms without fcntl
    fcntl = None  # type: ignore[assignment]

from .ingestion import IngestionService
from .planner.state_store import PlannerStateStore
from .snapper import SeatSnapperWorker

logger = logging.getLogger(__name__)


class RuntimeLeaseError(RuntimeError):
    """Raised when another local runtime already owns the event-scoped lease."""


class RuntimeJob(Protocol):
    name: str
    interval_seconds: float

    async def run_once(self) -> None: ...


class CatalogSyncJob:
    name = "catalog-sync"

    def __init__(self, ingestion: IngestionService, interval_seconds: float) -> None:
        self.ingestion = ingestion
        self.interval_seconds = interval_seconds

    async def run_once(self) -> None:
        await self.ingestion.incremental_sync()


class SeatSnapperJob:
    name = "seat-snapper"

    def __init__(self, worker: SeatSnapperWorker, interval_seconds: float) -> None:
        self.worker = worker
        self.interval_seconds = interval_seconds

    async def run_once(self) -> None:
        await self.worker.run_cycle()


class RuntimeCoordinator:
    """Own an event lease and independently schedule registered local jobs."""

    def __init__(
        self,
        store: PlannerStateStore,
        event_id: str,
        jobs: Optional[List[RuntimeJob]] = None,
        *,
        lease_seconds: float = 30.0,
    ) -> None:
        if not event_id:
            raise ValueError("event_id must not be empty")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.store = store
        self.event_id = event_id
        self.lease_seconds = lease_seconds
        self._jobs = list(jobs or [])
        self._owner_id = uuid4().hex
        self._stop_requested = asyncio.Event()
        self._job_tasks: List[asyncio.Task[None]] = []
        self._lease_task: Optional[asyncio.Task[None]] = None
        self._lock_fd: Optional[int] = None
        self._sqlite_lease = False
        self._started = False
        self._released = False

    @property
    def jobs(self) -> tuple[RuntimeJob, ...]:
        return tuple(self._jobs)

    async def start(self) -> None:
        if self._started:
            return
        for job in self._jobs:
            if not job.name or job.interval_seconds <= 0:
                raise ValueError("runtime jobs need a name and positive interval_seconds")
        self._stop_requested.clear()
        self._released = False
        await self._acquire_lease()
        self._started = True
        self._job_tasks = [asyncio.create_task(self._run_job(job)) for job in self._jobs]
        if self._sqlite_lease:
            self._lease_task = asyncio.create_task(self._renew_sqlite_lease())
        logger.info("Runtime coordinator started for event %s", self.event_id)

    def stop(self) -> None:
        """Request shutdown; ``aclose`` waits for an active job cycle to finish."""
        self._stop_requested.set()

    async def wait(self) -> None:
        """Wait for stop and then release the lease after every active cycle finishes."""
        await self._stop_requested.wait()
        tasks = list(self._job_tasks)
        if self._lease_task is not None:
            tasks.append(self._lease_task)
        if tasks:
            await asyncio.gather(*tasks)
        await self._release_lease()
        self._started = False
        logger.info("Runtime coordinator stopped for event %s", self.event_id)

    async def aclose(self) -> None:
        self.stop()
        if self._started:
            await self.wait()
        else:
            await self._release_lease()

    async def _run_job(self, job: RuntimeJob) -> None:
        while not self._stop_requested.is_set():
            correlation_id = uuid4().hex
            try:
                logger.info("Runtime job %s started (%s)", job.name, correlation_id)
                await job.run_once()
                logger.info("Runtime job %s completed (%s)", job.name, correlation_id)
            except Exception:
                logger.exception("Runtime job %s failed (%s)", job.name, correlation_id)
            if self._stop_requested.is_set():
                break
            try:
                await asyncio.wait_for(self._stop_requested.wait(), timeout=job.interval_seconds)
            except asyncio.TimeoutError:
                pass

    @property
    def _lock_path(self) -> Path:
        return self.store.path.with_name(f"{self.store.path.name}.{self.event_id}.runtime.lock")

    async def _acquire_lease(self) -> None:
        if fcntl is None:
            await self._acquire_sqlite_lease()
            return
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(fd)
            if error.errno in (errno.EACCES, errno.EAGAIN):
                raise RuntimeLeaseError(
                    f"Runtime already running for event '{self.event_id}'"
                ) from error
            raise
        self._lock_fd = fd

    async def _acquire_sqlite_lease(self) -> None:
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=self.lease_seconds)
        async with self.store._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await connection.execute(
                        "SELECT owner_id, expires_at FROM runtime_leases WHERE event_id = ?",
                        (self.event_id,),
                    )
                ).fetchone()
                if row is not None and datetime.fromisoformat(row[1]) > now:
                    raise RuntimeLeaseError(f"Runtime already running for event '{self.event_id}'")
                await connection.execute(
                    """
                    INSERT INTO runtime_leases (event_id, owner_id, expires_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(event_id) DO UPDATE SET
                        owner_id = excluded.owner_id, expires_at = excluded.expires_at
                    """,
                    (self.event_id, self._owner_id, expires_at.isoformat()),
                )
            except Exception:
                await connection.rollback()
                raise
            await connection.commit()
        self._sqlite_lease = True

    async def _renew_sqlite_lease(self) -> None:
        while not self._stop_requested.is_set():
            try:
                await asyncio.wait_for(self._stop_requested.wait(), timeout=self.lease_seconds / 2)
                return
            except asyncio.TimeoutError:
                pass
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=self.lease_seconds)
            async with self.store._connect() as connection:
                cursor = await connection.execute(
                    """
                    UPDATE runtime_leases SET expires_at = ?
                    WHERE event_id = ? AND owner_id = ?
                    """,
                    (expires_at.isoformat(), self.event_id, self._owner_id),
                )
                await connection.commit()
            if cursor.rowcount != 1:
                logger.error("Runtime SQLite lease was lost for event %s", self.event_id)
                self.stop()
                return

    async def _release_lease(self) -> None:
        if self._released:
            return
        self._released = True
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None
        if self._sqlite_lease:
            async with self.store._connect() as connection:
                await connection.execute(
                    "DELETE FROM runtime_leases WHERE event_id = ? AND owner_id = ?",
                    (self.event_id, self._owner_id),
                )
                await connection.commit()
            self._sqlite_lease = False
