"""Hybrid search ranking functions (SPEC-18 §18.1 split of ingestion/service.py).

Pure and side-effect free: no SQLite, no vector math. ``service.py`` combines these with
``catalog_store.py`` (SQL filtering) and ``vector_index.py`` (semantic ranking).
"""

from typing import Any, Dict, Iterable, Sequence

RRF_K = 60
MAX_SEARCH_LIMIT = 20
_KEYWORD_WEIGHTS = (("title", 3.0), ("track", 2.0), ("speaker_names", 1.5), ("description", 1.0))


def reciprocal_rank_fusion(rankings: Iterable[Sequence[str]], k: int = RRF_K) -> Dict[str, float]:
    """``score(d) = Σ 1 / (k + rank_i(d))`` over every ranking containing ``d`` (1-based)."""
    scores: Dict[str, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return scores


def keyword_score(row: Dict[str, Any], terms: Sequence[str]) -> float:
    """Weighted count of query terms appearing in title, track, speakers and abstract."""
    score = 0.0
    for column, weight in _KEYWORD_WEIGHTS:
        text = (row.get(column) or "").lower()
        score += weight * sum(1 for term in terms if term in text)
    return score


def like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"
