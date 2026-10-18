"""Catalog ingestion and hybrid search (SPEC-03)."""

from .ports import Embedder
from .service import IngestionService, reciprocal_rank_fusion

__all__ = ["Embedder", "IngestionService", "reciprocal_rank_fusion"]
