"""Semantic vector index backed by SQLite + numpy (SPEC-20; replaces FAISS).

Vectors live in the ``session_vectors`` table owned by ``CatalogStore``, keyed by
``(session_id)`` with a ``model_id``/``text_hash`` pair that decides whether a session needs
re-embedding. A seat-only change never touches ``document_text``, so it never re-embeds —
this fixes SPEC-16 Bug B by construction. Cosine ranking is a plain dot product since every
stored vector is L2-normalized at write time.
"""

import asyncio
import hashlib
import logging
from typing import List, Optional, Sequence, Set

import numpy as np

from ..models import Session
from .catalog_store import CatalogStore
from .ports import Embedder, canonical_model_id

logger = logging.getLogger(__name__)

TOP_K = 50


def document_text(session: Session) -> str:
    return (
        f"Title: {session.title} | Track: {session.track or ''} | Level: {session.level or ''} | "
        f"Description: {session.description or ''} | Speakers: {session.speaker_names or ''}"
    )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class VectorIndex:
    """Owns the embedder lifecycle and semantic ranking over ``session_vectors``."""

    def __init__(
        self,
        catalog: CatalogStore,
        embedding_model: str,
        embedder: Optional[Embedder] = None,
    ) -> None:
        self.catalog = catalog
        self.embedding_model = embedding_model
        self._embedder = embedder
        self._state = "degraded"
        self._matrix: Optional[np.ndarray] = None
        self._ids: List[str] = []
        self._cached_marker = -1
        self._cached_model_id: Optional[str] = None

    @property
    def vector_search_available(self) -> bool:
        """Whether semantic ranking is safe to use right now."""
        return self._state == "available"

    @property
    def vector_search_state(self) -> str:
        """Return ``available``, ``degraded``, or ``rebuilding`` for callers and UI status."""
        return self._state

    @vector_search_state.setter
    def vector_search_state(self, value: str) -> None:
        self._state = value

    @property
    def has_explicit_embedder(self) -> bool:
        return self._embedder is not None

    @property
    def embedder(self) -> Embedder:
        if self._embedder is None:
            # Deferred: constructing fastembed's TextEmbedding loads/downloads the ONNX model.
            from .fastembed_embedder import FastEmbedEmbedder

            self._embedder = FastEmbedEmbedder(self.embedding_model)
        return self._embedder

    async def embed(self, texts: List[str]) -> np.ndarray:
        vectors = await asyncio.to_thread(self.embedder.embed, texts)
        return np.asarray(vectors, dtype=np.float32).reshape(len(texts), -1)

    async def upsert(self, sessions: Sequence[Session]) -> bool:
        """Embed sessions whose document text changed; returns True if anything changed."""
        try:
            model_id = self.embedder.model_id
        except ImportError:
            self._state = "degraded"
            return False

        pending = []
        for session in sessions:
            text = document_text(session)
            text_hash = _sha256(text)
            known = await self.catalog.vector_hash(session.session_id)
            if known != (model_id, text_hash):
                pending.append((session.session_id, text, text_hash))

        self._state = "available"
        if not pending:
            return False

        vectors = await self.embed([text for _, text, _ in pending])
        rows = [
            (session_id, model_id, text_hash, vectors[i].tobytes())
            for i, (session_id, _text, text_hash) in enumerate(pending)
        ]
        await self.catalog.upsert_vectors(rows)
        return True

    async def rebuild(self, sessions: Sequence[Session]) -> None:
        try:
            _ = self.embedder.model_id
        except ImportError:
            self._state = "degraded"
            return
        await self.catalog.clear_vectors()
        self._matrix, self._ids = None, []
        self._cached_marker, self._cached_model_id = -1, None
        await self.upsert(sessions)
        logger.info("Vector index rebuilt with %d sessions", len(sessions))

    async def save(self) -> None:
        """No-op: SQLite rows are already committed by ``upsert``/``rebuild``.

        Kept so ``CatalogSyncer`` doesn't need a seam-specific branch for "nothing to publish."
        """
        return None

    async def load(self) -> bool:
        """Check whether usable vectors already exist for the configured model.

        A pure read: never constructs the embedder, so a missing ``semantic`` extra alone
        doesn't make this raise.
        """
        model_id = canonical_model_id(self.embedding_model)
        if await self.catalog.vectors_max_rowid(model_id) == 0:
            return False
        self._state = "available"
        return True

    async def semantic_ranking(self, query: str, allowed: Set[str]) -> List[str]:
        if not self.vector_search_available or not allowed:
            return []
        try:
            model_id = self.embedder.model_id
        except ImportError:
            self._state = "degraded"
            return []
        await self._refresh_matrix(model_id)
        if self._matrix is None:
            return []
        query_vector = (await self.embed([query]))[0]
        scores = self._matrix @ query_vector
        order = np.argsort(-scores)
        ranking = [self._ids[i] for i in order if self._ids[i] in allowed]
        return ranking[:TOP_K]

    async def _refresh_matrix(self, model_id: str) -> None:
        """Reload the in-memory matrix when the stored vectors changed, checked once per search."""
        marker = await self.catalog.vectors_max_rowid(model_id)
        if (
            self._matrix is not None
            and marker == self._cached_marker
            and model_id == self._cached_model_id
        ):
            return
        rows = await self.catalog.all_vectors(model_id)
        try:
            self._ids = [session_id for session_id, _vector in rows]
            self._matrix = (
                np.stack([np.frombuffer(vector, dtype=np.float32) for _session_id, vector in rows])
                if rows
                else None
            )
        except ValueError:
            # A malformed row (wrong byte length) must never break keyword search too.
            logger.warning("Discarding unreadable vectors for %s; search degraded", model_id)
            self._ids, self._matrix = [], None
        self._cached_marker = marker
        self._cached_model_id = model_id
