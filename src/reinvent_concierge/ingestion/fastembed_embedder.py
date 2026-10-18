"""Default ``Embedder`` (SPEC-20): fastembed (ONNX) replaces sentence-transformers/PyTorch."""

from typing import Sequence

import numpy as np

from .ports import canonical_model_id


class FastEmbedEmbedder:
    """Wraps fastembed's ``TextEmbedding`` behind the neutral ``Embedder`` port.

    Raises ``ImportError`` at construction when the ``semantic`` extra isn't installed;
    callers degrade search to keyword-only on that error (see ``VectorIndex``).
    """

    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        from fastembed import TextEmbedding

        full_name = canonical_model_id(model_name)
        self._model = TextEmbedding(model_name=full_name)
        self.model_id = full_name

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        vectors = np.asarray(list(self._model.embed(list(texts))), dtype=np.float32)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return vectors / norms
