"""Galería facial de una empresa para identificar 1:N (validador de identidad).

Las muestras viven cifradas en la BD (biometrics.face_embeddings). Descifrarlas en cada
identificación sería costoso, así que cada proceso guarda en memoria una matriz normalizada por
empresa y la reconstruye solo cuando cambia la "huella" de la galería en la BD (una consulta
agregada barata): la BD sigue siendo la única fuente de verdad y no hay invalidaciones que olvidar.
"""

import threading
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.crypto import decrypt_bytes
from app.facial_recognition.matcher import embedding_from_bytes
from app.repositories.face_repository import FaceEmbeddingRepository


@dataclass(frozen=True)
class Gallery:
    fingerprint: tuple[int, int, str]
    #: Empleado de cada fila de la matriz.
    labels: np.ndarray
    #: Muestras normalizadas (filas): la similitud coseno es un producto punto.
    matrix: np.ndarray

    @property
    def size(self) -> int:
        return int(self.labels.shape[0])


@dataclass(frozen=True)
class Identification:
    """Resultado de comparar las capturas contra toda la galería."""

    employee_id: int | None
    #: Similitud de cada captura con la persona identificada (o con la mejor candidata).
    similarities: tuple[float, ...]
    reason: str | None = None


def _normalize(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=-1, keepdims=True)
    return vectors / np.maximum(norms, 1e-12)


def identify(gallery: Gallery, probes: Sequence[np.ndarray], *, required: float, margin: float) -> Identification:
    """Quién es, por consenso entre capturas.

    Cada captura debe señalar a la MISMA persona, alcanzar la similitud requerida y superar con
    margen a la segunda persona más parecida (si dos personas se parecen, no se adivina).
    """
    if gallery.size == 0:
        return Identification(None, (), "EMPTY_GALLERY")
    people, rows = np.unique(gallery.labels, return_inverse=True)
    winners: list[int] = []
    best: list[float] = []
    gaps: list[float] = []
    for probe in probes:
        scores = gallery.matrix @ _normalize(np.asarray(probe, dtype=np.float32))
        per_person = np.full(people.shape[0], -1.0, dtype=np.float32)
        np.maximum.at(per_person, rows, scores)
        order = np.argsort(per_person)[::-1]
        winners.append(int(people[order[0]]))
        best.append(float(per_person[order[0]]))
        gaps.append(float(per_person[order[0]] - per_person[order[1]]) if people.shape[0] > 1 else 1.0)
    similarities = tuple(round(s, 4) for s in best)
    if len(set(winners)) != 1:
        return Identification(None, similarities, "INCONSISTENT_MATCH")
    if min(best) < required:
        return Identification(None, similarities, "NO_MATCH")
    if min(gaps) < margin:
        return Identification(None, similarities, "AMBIGUOUS_MATCH")
    return Identification(winners[0], similarities)


def duplicate_of(gallery: Gallery, probes: Sequence[np.ndarray], *, exclude: int, required: float) -> int | None:
    """Otro empleado de la galería con ESTE rostro (todas las capturas lo señalan con la confianza
    exigida), o None. Detecta a una misma persona registrándose en dos cuentas."""
    others = gallery.labels != exclude
    if not others.any():
        return None
    filtered = Gallery(gallery.fingerprint, gallery.labels[others], gallery.matrix[others])
    return identify(filtered, probes, required=required, margin=0.0).employee_id


class FaceGalleryCache:
    """Galerías por empresa en memoria (LRU), validadas contra la huella de la BD en cada uso."""

    def __init__(self, max_companies: int) -> None:
        self.max_companies = max_companies
        self._items: OrderedDict[tuple[int, str], Gallery] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, db: Session, company_id: int, model_name: str) -> Gallery:
        repo = FaceEmbeddingRepository(db)
        key = (company_id, model_name)
        fingerprint = repo.gallery_fingerprint(company_id, model_name)
        with self._lock:
            cached = self._items.get(key)
            if cached is not None and cached.fingerprint == fingerprint:
                self._items.move_to_end(key)
                return cached
        gallery = self._build(repo.gallery_rows(company_id, model_name), fingerprint)
        with self._lock:
            self._items[key] = gallery
            self._items.move_to_end(key)
            while len(self._items) > self.max_companies:
                self._items.popitem(last=False)
        return gallery

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    @staticmethod
    def _build(rows: list[tuple[int, bytes, int]], fingerprint: tuple[int, int, str]) -> Gallery:
        if not rows:
            return Gallery(fingerprint, np.empty(0, dtype=np.int64), np.empty((0, 0), dtype=np.float32))
        labels = np.fromiter((employee_id for employee_id, _, _ in rows), dtype=np.int64, count=len(rows))
        vectors = np.stack([embedding_from_bytes(decrypt_bytes(data), dim) for _, data, dim in rows]).astype(np.float32)
        return Gallery(fingerprint, labels, _normalize(vectors))


face_galleries = FaceGalleryCache(settings.FACE_GALLERY_CACHE_COMPANIES)
