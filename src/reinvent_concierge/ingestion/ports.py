"""Ports for the ingestion package (SPEC-20): the embedding abstraction.

Kept separate from ``vector_index.py`` so the port has no dependency on any particular
embedding library, mirroring ``api/ports.py``.
"""

from typing import Protocol, Sequence

import numpy as np


class Embedder(Protocol):
    """Turns text into L2-normalized float32 vectors.

    Implementations may block (model inference); callers run them in a thread (see
    ``VectorIndex.embed``).
    """

    model_id: str

    def embed(self, texts: Sequence[str]) -> np.ndarray: ...


def canonical_model_id(name: str) -> str:
    """fastembed needs a full Hugging Face repo id; the config knob keeps the short name."""
    return name if "/" in name else f"sentence-transformers/{name}"
