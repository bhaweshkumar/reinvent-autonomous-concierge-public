"""Session catalog ingestion and hybrid keyword + semantic search (SPEC-03).

Split per SPEC-18 §18.1 into ``catalog_store`` (SQLite CRUD), ``vector_index`` (semantic
vectors, SQLite + numpy per SPEC-20), ``search`` (pure ranking functions) and ``sync`` (the
orchestration that ties the store and the index together). ``IngestionService`` is the
facade every other module keeps importing.
"""

import logging
from typing import Any, Dict, List, Optional, Set

from ..config import Config
from ..models import Session, SessionSummary
from ..venues import load_registry
from .catalog_store import CatalogStore
from .ports import Embedder
from .search import MAX_SEARCH_LIMIT, keyword_score, like_pattern, reciprocal_rank_fusion
from .sync import CatalogClient, CatalogSyncer
from .vector_index import VectorIndex

logger = logging.getLogger(__name__)


class IngestionService:
    """Syncs the AWS Events catalog into SQLite and serves hybrid keyword + semantic search."""

    def __init__(
        self,
        config: Config,
        rest_client: CatalogClient,
        embedder: Optional[Embedder] = None,
    ):
        self.config = config
        self.client = rest_client
        self.event_id = config.aws_events_event_id
        self.db_path = config.local_db_path

        self._catalog = CatalogStore(self.db_path)
        self._vectors = VectorIndex(self._catalog, config.embedding_model, embedder=embedder)
        self._syncer = CatalogSyncer(rest_client, self._catalog, self._vectors)

    @property
    def vector_search_available(self) -> bool:
        """Whether semantic ranking is safe to use for the current catalog revision."""
        return self._vectors.vector_search_available

    @property
    def vector_search_state(self) -> str:
        """Return ``available``, ``degraded``, or ``rebuilding`` for callers and UI status."""
        return self._vectors.vector_search_state

    @property
    def embedder(self) -> Embedder:
        return self._vectors.embedder

    # ------------------------------------------------------------------ lifecycle

    async def initialize(self) -> None:
        """Create/migrate the schema and check for persisted vectors (or rebuild them)."""
        await self._catalog.ensure_schema()

        if not await self._vectors.load():
            self._vectors.vector_search_state = "degraded"
            # Keep keyword search usable when no embedding model was supplied. Explicit syncs
            # still construct vectors, while injected embedders permit recovery at startup.
            if self._vectors.has_explicit_embedder:
                self._vectors.vector_search_state = "rebuilding"
                await self._vectors.rebuild(await self._catalog.load_all())
                await self._vectors.save()

    async def initial_sync(self) -> int:
        await self.initialize()
        return await self._syncer.initial_sync(self.event_id)

    async def incremental_sync(self) -> int:
        await self.initialize()
        return await self._syncer.incremental_sync(self.event_id)

    async def sync_single(self, session_id: str) -> Optional[Session]:
        return await self._syncer.sync_single(session_id)

    async def upsert_session(self, session: Session) -> None:
        await self._syncer.upsert_session(session)

    async def get_session_local(self, session_id: str) -> Optional[Session]:
        """Look a session up in the local catalog without calling the API."""
        return await self._catalog.get_local(session_id)

    async def repeat_group_counts(self, groups) -> Dict[str, int]:
        """Count non-cancelled offerings per repeat group in the local catalog."""
        return await self._catalog.repeat_group_counts(groups)

    async def list_local_sessions(self, limit: int = 250) -> List[Session]:
        """Return a bounded, stable local catalog view without contacting AWS."""
        return await self._catalog.list_local(limit)

    async def event_timezone(self) -> Optional[str]:
        """IANA zone reported by GetEvent at the last sync, if any."""
        value = await self._catalog.get_meta("event_timezone")
        return str(value) if value else None

    # ------------------------------------------------------------------ search

    async def search(
        self,
        query: str,
        level: Optional[int] = None,
        venue: Optional[str] = None,
        hero_only: bool = False,
        limit: int = 5,
        session_type: Optional[str] = None,
        topic: Optional[str] = None,
    ) -> List[SessionSummary]:
        """Hybrid search over OPEN sessions: SQLite filter + keyword rank + semantic rank,
        merged with Reciprocal Rank Fusion.

        Raises:
            ValueError: If ``limit`` is outside 1-20
        """
        if not 1 <= limit <= MAX_SEARCH_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_SEARCH_LIMIT}")

        where: List[str] = ["status = 'OPEN'"]
        params: List[Any] = []
        if level is not None:
            where.append("level = ?")
            params.append(level)
        if venue:
            canonical = load_registry().canonical(venue)
            if canonical is not None:
                where.append("venue_id = ?")
                params.append(canonical)
            else:
                # Unregistered venues keep their first room segment in `zone` (ROOM_PARSED).
                pattern = like_pattern(venue)
                where.append(
                    "(venue_id LIKE ? ESCAPE '\\' OR zone LIKE ? ESCAPE '\\' "
                    "OR room_name LIKE ? ESCAPE '\\')"
                )
                params.extend([pattern, pattern, pattern])
        if session_type:
            where.append("lower(session_type) = lower(?)")
            params.append(session_type)
        if topic:
            where.append(
                "EXISTS (SELECT 1 FROM json_each(COALESCE(topics_json, '[]')) "
                "WHERE lower(json_each.value) = lower(?))"
            )
            params.append(topic)
        if hero_only:
            where.append("is_hero_speaker = 1")

        rows = await self._catalog.query_open(where, params)

        terms = query.lower().split()
        scored = [(keyword_score(row, terms), sid) for sid, row in rows.items()]
        # Rows with no matching term carry no keyword signal and must not earn RRF credit.
        keyword_ranking = [sid for score, sid in sorted(scored, key=lambda x: -x[0]) if score > 0]
        semantic_ranking = await self._semantic_ranking(query, set(rows))

        fused = reciprocal_rank_fusion([keyword_ranking, semantic_ranking])
        top = sorted(fused.items(), key=lambda item: (-item[1], item[0]))[:limit]
        return [CatalogStore.summary_from_row(rows[sid], score) for sid, score in top]

    async def _semantic_ranking(self, query: str, allowed: Set[str]) -> List[str]:
        return await self._vectors.semantic_ranking(query, allowed)
