"""Comparación de embeddings mediante similitud coseno."""

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class MatchResult:
    matched: bool
    similarity: float
    threshold: float


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0.0:
        return 0.0
    return float(np.dot(a, b) / denom)


def best_match(probe: np.ndarray, references: Sequence[np.ndarray], threshold: float) -> MatchResult:
    """Compara el embedding capturado contra todas las muestras registradas y toma la mejor."""
    if not references:
        return MatchResult(matched=False, similarity=0.0, threshold=threshold)
    best = max(cosine_similarity(probe, ref) for ref in references)
    return MatchResult(matched=best >= threshold, similarity=round(best, 4), threshold=threshold)


def embedding_to_bytes(vector: np.ndarray) -> bytes:
    return np.asarray(vector, dtype=np.float32).tobytes()


def embedding_from_bytes(data: bytes, dimension: int) -> np.ndarray:
    vector = np.frombuffer(data, dtype=np.float32)
    if vector.size != dimension:
        raise ValueError(f"Dimensión de embedding inesperada: {vector.size} != {dimension}")
    return vector
