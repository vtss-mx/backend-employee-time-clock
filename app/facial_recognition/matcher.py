"""Comparación de embeddings mediante similitud coseno."""

import logging
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from app.core.crypto import try_decrypt

logger = logging.getLogger(__name__)

#: La fusión guarda [√w·SFace (128), √(1-w)·FaceNet (512)]: cada parte se compara por separado
#: renormalizándola (sin volver a registrar a nadie).
SFACE_DIMENSION = 128
FUSION_DIMENSION = SFACE_DIMENSION + 512


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


@dataclass(frozen=True)
class MatchRequirement:
    """Lo que debe alcanzar cada (captura, muestra) para contar como la misma persona."""

    #: Similitud de la fusión (o del único modelo).
    fused: float
    #: Similitud mínima de SFace y de FaceNet por separado (solo con vectores de la fusión).
    floors: tuple[float, float] | None = None


def acceptance(
    probes: Sequence[np.ndarray], references: Sequence[np.ndarray], requirement: MatchRequirement
) -> tuple[np.ndarray, np.ndarray]:
    """(similitud de la fusión, coincidencia aceptada) de cada captura contra cada muestra.

    Con vectores de la fusión, además de la similitud combinada CADA modelo debe alcanzar su piso:
    una imagen fabricada para engañar a uno no basta."""
    fused = similarity_matrix(probes, references)
    accepted = fused >= requirement.fused
    if requirement.floors is not None and fused.size and np.asarray(probes[0]).shape[-1] == FUSION_DIMENSION:
        left, right = np.stack(probes).astype(np.float32), np.stack(references).astype(np.float32)
        for part, floor in zip(
            (slice(None, SFACE_DIMENSION), slice(SFACE_DIMENSION, None)), requirement.floors, strict=True
        ):
            accepted &= normalize_rows(left[:, part]) @ normalize_rows(right[:, part]).T >= floor
    return fused, accepted


def embedding_to_bytes(vector: np.ndarray) -> bytes:
    return np.asarray(vector, dtype=np.float32).tobytes()


def embedding_from_bytes(data: bytes, dimension: int) -> np.ndarray:
    vector = np.frombuffer(data, dtype=np.float32)
    if vector.size != dimension:
        raise ValueError(f"Dimensión de embedding inesperada: {vector.size} != {dimension}")
    return vector


def readable_embedding(sample_id: int, encrypted: bytes, dimension: int) -> np.ndarray | None:
    """El vector de una muestra (descifrado y deserializado), o None (y un registro en el log) si es ilegible. Vive en
    esta capa (reconocimiento, sin servicios ni repositorios) para que el servicio Y el repositorio lo usen sin un ciclo
    de importación servicio ↔ repositorio."""
    data = try_decrypt(encrypted)
    vector = None
    if data is not None:
        try:
            vector = embedding_from_bytes(data, dimension)
        except ValueError:
            vector = None
    if vector is None:
        logger.error("Muestra facial %s ilegible (dañada o de otra llave de cifrado): se omite", sample_id)
    return vector
