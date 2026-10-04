"""Comparación de embeddings mediante similitud coseno."""

from collections.abc import Sequence

import numpy as np


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0.0:
        return 0.0
    return float(np.dot(a, b) / denom)


def normalize_rows(vectors: np.ndarray) -> np.ndarray:
    """Vectores de norma 1 (por fila): la similitud coseno queda como un producto punto."""
    norms = np.linalg.norm(vectors, axis=-1, keepdims=True)
    return vectors / np.maximum(norms, 1e-12)


def similarity_matrix(probes: Sequence[np.ndarray], references: Sequence[np.ndarray]) -> np.ndarray:
    """Similitud coseno de cada captura (filas) contra cada muestra (columnas), en una sola operación."""
    left = normalize_rows(np.stack(probes).astype(np.float32))
    right = normalize_rows(np.stack(references).astype(np.float32))
    return left @ right.T


def embedding_to_bytes(vector: np.ndarray) -> bytes:
    return np.asarray(vector, dtype=np.float32).tobytes()


def embedding_from_bytes(data: bytes, dimension: int) -> np.ndarray:
    vector = np.frombuffer(data, dtype=np.float32)
    if vector.size != dimension:
        raise ValueError(f"Dimensión de embedding inesperada: {vector.size} != {dimension}")
    return vector
