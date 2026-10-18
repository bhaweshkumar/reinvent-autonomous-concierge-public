"""Catalog sync orchestration (SPEC-18 §18.1 split of ingestion/service.py).

Combines ``CatalogStore`` (SQL) and ``VectorIndex`` (SQLite + numpy vectors, SPEC-20) for
the operations that touch both: a changed row always decides whether the vector index
needs a full rebuild or a targeted upsert. A cancellation never touches vectors (status
isn't part of the embedded text), fixing SPEC-16 Bug B by construction.
"""

import logging
from typing import AsyncIterator, List, Optional, Protocol, Sequence

from ..exceptions import AWSEventsAPIError, SyncError
from ..models import Event, Session
from ..util.time import is_valid_zone
from .catalog_store import CatalogStore
from .vector_index import VectorIndex

logger = logging.getLogger(__name__)


class CatalogClient(Protocol):
    async def get_event(self, event_id: Optional[str] = None) -> Event: ...

    def list_sessions(self, event_id: Optional[str] = None) -> AsyncIterator[Session]: ...

    async def get_session(self, session_id: str) -> Session: ...


class CatalogSyncer:
    """Syncs the AWS Events catalog into ``CatalogStore`` and ``VectorIndex``."""

    def __init__(self, client: CatalogClient, catalog: CatalogStore, vectors: VectorIndex) -> None:
        self.client = client
        self.catalog = catalog
        self.vectors = vectors

    async def fetch_catalog(self, event_id: str) -> List[Session]:
        # GetEvent (not ListEvents) so ended or registration-only events can be synced too.
        try:
            event = await self.client.get_event(event_id)
        except AWSEventsAPIError as e:
            if e.status_code == 404:
                raise SyncError(f"Event '{event_id}' does not exist") from e
            raise
        if event.timezone:
            if is_valid_zone(event.timezone):
                await self.catalog.set_meta("event_timezone", event.timezone)
            else:
                logger.warning(
                    "GetEvent returned an unknown timezone %r; keeping the configured zone",
                    event.timezone,
                )
        return [session async for session in self.client.list_sessions(event_id)]

    async def initial_sync(self, event_id: str) -> int:
        """Full sync: fetch the whole catalog, then replace local rows and the index.

        The catalog is fetched before local state is touched, so a failed walk leaves
        the previous catalog intact.
        """
        sessions = await self.fetch_catalog(event_id)
        await self.catalog.replace_all(sessions)
        await self.vectors.rebuild(sessions)
        logger.info("Initial sync complete: %d sessions", len(sessions))
        return len(sessions)

    async def incremental_sync(self, event_id: str) -> int:
        """Delta sync: upsert only sessions whose content changed and patch their vectors.

        The API exposes no modification timestamp, so changes are detected by content
        hash; sessions that disappeared from the catalog are marked CANCELLED.
        """
        sessions = await self.fetch_catalog(event_id)
        changed = await self._store(sessions)
        seen = {s.session_id for s in sessions}
        vanished = [sid for sid in await self.catalog.uncanceled_session_ids() if sid not in seen]
        await self._mark_canceled(vanished)
        logger.info("Incremental sync: %d changed, %d canceled", changed, len(vanished))
        return changed + len(vanished)

    async def sync_single(self, session_id: str) -> Optional[Session]:
        """Refresh one session (seat availability) from GetSession.

        Returns:
            The fresh session, or None if the API no longer has it (marked CANCELLED)
        """
        try:
            session = await self.client.get_session(session_id)
        except AWSEventsAPIError as e:
            if e.status_code != 404:
                raise
            await self._mark_canceled([session_id])
            return None
        await self._store([session])
        return session

    async def upsert_session(self, session: Session) -> None:
        """Insert or update one session row and its vector."""
        await self._store([session])

    async def _store(self, sessions: Sequence[Session]) -> int:
        changed = await self.catalog.upsert_changed(sessions)
        if not self.vectors.vector_search_available:
            # Never publish a partial index: without an already-usable index, embed everything.
            await self.vectors.rebuild(await self.catalog.load_all())
        else:
            await self.vectors.upsert(changed)
        return len(changed)

    async def _mark_canceled(self, session_ids: Sequence[str]) -> None:
        if not session_ids:
            return
        await self.catalog.mark_canceled(session_ids)
