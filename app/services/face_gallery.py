"""Galería facial de una empresa para identificar 1:N (validador de identidad).

Las muestras viven cifradas en la BD (biometrics.face_embeddings). Descifrarlas en cada
identificación sería costoso, así que cada proceso guarda en memoria una matriz normalizada por
empresa y la actualiza solo cuando cambia la "huella" de la galería en la BD (una consulta
agregada barata): la BD sigue siendo la única fuente de verdad y no hay invalidaciones que olvidar.

Con el aprendizaje continuo (face_learning) la galería cambia seguido, una muestra a la vez: al
cambiar la huella se leen solo los ids vigentes y se descifra únicamente lo nuevo (lo que salió se
descarta), en lugar de descifrar otra vez toda la empresa.
"""

import threading
import time
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cached_property

import numpy as np
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.observability import observed
from app.facial_recognition.matcher import FUSION_DIMENSION, MatchRequirement, acceptance, normalize_rows
from app.repositories.face_repository import FaceEmbeddingRepository, GalleryRow
from app.services.face_service import readable_embedding

#: Huella de la galería en la BD: (muestras, id mayor, última actualización de un empleado).
Fingerprint = tuple[int, int, str]
#: Memoria de una fila de la galería, a lo más (el vector más grande, el de la fusión, más su id y su empleado).
ROW_BYTES = FUSION_DIMENSION * 4 + 16


@dataclass(frozen=True)
class Gallery:
    fingerprint: Fingerprint
    #: Muestra (id en la BD) de cada fila de la matriz.
    ids: np.ndarray
    #: Empleado de cada fila de la matriz.
    labels: np.ndarray
    #: Muestras normalizadas (filas): la similitud coseno es un producto punto.
    matrix: np.ndarray

    @property
    def size(self) -> int:
        return int(self.labels.shape[0])

    @property
    def nbytes(self) -> int:
        return int(self.ids.nbytes + self.labels.nbytes + self.matrix.nbytes)

    def rows(self, mask: np.ndarray) -> Gallery:
        """Solo las filas indicadas (máscara o índices), con la misma huella."""
        return Gallery(self.fingerprint, self.ids[mask], self.labels[mask], self.matrix[mask])

    @cached_property
    def by_person(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Las filas agrupadas por empleado (orden, dónde empieza cada grupo y de quién es), una vez por galería: el 1:N
        de cada verificación reduce por persona de un golpe (`reduceat`) en lugar de fila por fila."""
        order = np.argsort(self.labels, kind="stable")
        labels = self.labels[order]
        starts = np.flatnonzero(np.r_[True, labels[1:] != labels[:-1]])
        return order, starts, labels[starts]


@dataclass(frozen=True)
class Identification:
    """Resultado de comparar las capturas contra toda la galería."""

    employee_id: int | None
    #: Similitud de cada captura con la persona identificada (o con la mejor candidata).
    similarities: tuple[float, ...]
    reason: str | None = None
    #: Muestra de la persona identificada que más se pareció (suma a su utilidad).
    sample_id: int | None = None
    #: Menor ventaja (entre capturas) sobre la segunda persona más parecida.
    gap: float = 0.0


def _empty(fingerprint: Fingerprint) -> Gallery:
    return Gallery(
        fingerprint, np.empty(0, dtype=np.int64), np.empty(0, dtype=np.int64), np.empty((0, 0), dtype=np.float32)
    )


@observed("face.identify")
def identify(
    gallery: Gallery, probes: Sequence[np.ndarray], *, required: MatchRequirement, margin: float
) -> Identification:
    """Quién es, por consenso entre capturas.

    Cada captura debe señalar a la MISMA persona, alcanzar la similitud requerida (con la fusión,
    también cada modelo por separado contra alguna muestra de esa persona) y superar con margen a la
    segunda persona más parecida (si dos personas se parecen, no se adivina).
    """
    if gallery.size == 0:
        return Identification(None, (), "EMPTY_GALLERY")
    people, rows = np.unique(gallery.labels, return_inverse=True)
    scores = normalize_rows(np.stack(probes).astype(np.float32)) @ gallery.matrix.T
    winners: list[int] = []
    best: list[float] = []
    gaps: list[float] = []
    for probe_scores in scores:
        per_person = np.full(people.shape[0], -1.0, dtype=np.float32)
        np.maximum.at(per_person, rows, probe_scores)
        order = np.argsort(per_person)[::-1]
        winners.append(int(people[order[0]]))
        best.append(float(per_person[order[0]]))
        gaps.append(float(per_person[order[0]] - per_person[order[1]]) if people.shape[0] > 1 else 1.0)
    similarities = tuple(round(s, 4) for s in best)
    if len(set(winners)) != 1:
        return Identification(None, similarities, "INCONSISTENT_MATCH")
    mine = np.flatnonzero(gallery.labels == winners[0])
    if not acceptance(probes, list(gallery.matrix[mine]), required)[1].any(axis=1).all():
        return Identification(None, similarities, "NO_MATCH")
    if min(gaps) < margin:
        return Identification(None, similarities, "AMBIGUOUS_MATCH")
    closest = mine[int(np.argmax(scores[:, mine].max(axis=0)))]
    return Identification(winners[0], similarities, sample_id=int(gallery.ids[closest]), gap=round(min(gaps), 4))


@observed("face.duplicate_search")
def duplicate_of(
    gallery: Gallery, probes: Sequence[np.ndarray], *, exclude: int, required: MatchRequirement
) -> int | None:
    """Otro empleado de la galería con ESTE rostro (todas las capturas lo señalan con la confianza
    exigida), o None. Detecta a una misma persona registrándose en dos cuentas."""
    others = gallery.labels != exclude
    if not others.any():
        return None
    return identify(gallery.rows(others), probes, required=required, margin=0.0).employee_id


def similar_people(
    gallery: Gallery, probes: Sequence[np.ndarray], *, exclude: int, required: float, top: int
) -> list[tuple[int, float]]:
    """Los `top` empleados (sin `exclude`) a los que se parecen TODAS las capturas con al menos `required` de
    similitud, del más parecido al menos: (empleado, similitud de su captura menos parecida). Es la búsqueda de
    SOSPECHA del registro facial (más sensible que la de aceptación): solo marca para la revisión."""
    others = gallery.rows(gallery.labels != exclude)
    if others.size == 0:
        return []
    people, columns = np.unique(others.labels, return_inverse=True)
    scores = normalize_rows(np.stack(probes).astype(np.float32)) @ others.matrix.T
    best = np.full((scores.shape[0], people.shape[0]), -1.0, dtype=np.float32)
    np.maximum.at(best, (np.arange(scores.shape[0])[:, None], columns[None, :]), scores)
    floor = best.min(axis=0)
    order = [i for i in np.argsort(floor)[::-1] if floor[i] >= required][:top]
    return [(int(people[i]), round(float(floor[i]), 4)) for i in order]


@observed("face.identity_rival")
def rival_of(gallery: Gallery | None, probes: Sequence[np.ndarray], *, exclude: int) -> tuple[int, float] | None:
    """1:N en cada 1:1 (antifraude 1b, señal IDENTITY_MISMATCH): el OTRO empleado al que más se parecen TODAS las
    capturas y la similitud de la menos parecida (la misma medida que `similar_people`). Solo CPU: quien llama lo
    hace sin transacción abierta. None sin galería o sin nadie más."""
    if gallery is None or gallery.size == 0:
        return None
    order, starts, people = gallery.by_person
    scores = normalize_rows(np.stack(probes).astype(np.float32)) @ gallery.matrix.T
    floor = np.maximum.reduceat(scores[:, order], starts, axis=1).min(axis=0)
    floor[people == exclude] = -np.inf
    best = int(np.argmax(floor))
    if not np.isfinite(floor[best]):
        return None
    return int(people[best]), round(float(floor[best]), 4)


def _decode(rows: list[GalleryRow], fingerprint: Fingerprint) -> Gallery:
    """Descifra y normaliza las filas leídas de la BD. Una muestra ilegible se omite (y se registra):
    nunca deja sin identificación a toda la empresa."""
    readable = [
        (sample_id, employee_id, vector)
        for sample_id, employee_id, data, dim in rows
        if (vector := readable_embedding(sample_id, data, dim)) is not None
    ]
    if not readable:
        return _empty(fingerprint)
    ids = np.fromiter((sample_id for sample_id, _, _ in readable), dtype=np.int64, count=len(readable))
    labels = np.fromiter((employee_id for _, employee_id, _ in readable), dtype=np.int64, count=len(readable))
    vectors = np.stack([vector for _, _, vector in readable])
    return Gallery(fingerprint, ids, labels, normalize_rows(vectors.astype(np.float32)))


def _join(kept: Gallery, added: Gallery | None, fingerprint: Fingerprint) -> Gallery:
    if added is None or added.size == 0:
        return Gallery(fingerprint, kept.ids, kept.labels, kept.matrix)
    if kept.size == 0:
        return Gallery(fingerprint, added.ids, added.labels, added.matrix)
    return Gallery(
        fingerprint,
        np.concatenate([kept.ids, added.ids]),
        np.concatenate([kept.labels, added.labels]),
        np.vstack([kept.matrix, added.matrix]),
    )


class FaceGalleryCache:
    """Galerías por empresa en memoria (LRU), validadas contra la huella de la BD en cada uso.

    Se acota por memoria: con muchas empresas pequeñas caben muchas; una muy grande no desplaza a
    todas las demás de golpe (siempre se conserva la que se acaba de usar)."""

    def __init__(self, max_companies: int, max_bytes: int) -> None:
        self.max_companies = max_companies
        self.max_bytes = max_bytes
        self._items: OrderedDict[tuple[int, str], Gallery] = OrderedDict()
        #: Cuándo se validó cada galería contra la BD por última vez (reloj monotónico del proceso).
        self._checked: dict[tuple[int, str], float] = {}
        self._lock = threading.Lock()

    @observed("face.gallery_load")
    def get(self, db: Session, company_id: int, model_name: str) -> Gallery:
        repo = FaceEmbeddingRepository(db)
        key = (company_id, model_name)
        return self._validated(repo, key, repo.gallery_fingerprint(company_id, model_name))

    @observed("face.gallery_recent")
    def recent(self, db: Session, company_id: int, model_name: str, *, max_age: float) -> Gallery | None:
        """La galería para una SEÑAL (1:N en cada 1:1 del empleado): la de memoria tal cual si se validó hace menos de
        `max_age` segundos (cero consultas: un retraso de minutos no cambia quién checa, la persona se compara contra
        sus propias muestras recién leídas); si no, se valida como en `get`. Nunca carga una galería que no cabe en la
        caché (una empresa enorme no se descifra entera dentro de un registro): None, y la señal no se mide."""
        key = (company_id, model_name)
        with self._lock:
            checked = self._checked.get(key)  # se anota y se olvida junto con su galería
            if checked is not None and time.monotonic() - checked < max_age:
                self._items.move_to_end(key)
                return self._items[key]
        repo = FaceEmbeddingRepository(db)
        fingerprint = repo.gallery_fingerprint(company_id, model_name)
        if checked is None and fingerprint[0] * ROW_BYTES > self.max_bytes:
            return None
        return self._validated(repo, key, fingerprint)

    def _validated(self, repo: FaceEmbeddingRepository, key: tuple[int, str], fingerprint: Fingerprint) -> Gallery:
        """La galería que corresponde a la huella de la BD (la de memoria si no cambió; si no, la pone al día)."""
        with self._lock:
            cached = self._items.get(key)
            if cached is not None and cached.fingerprint == fingerprint:
                self._items.move_to_end(key)
                self._checked[key] = time.monotonic()
                return cached
        company_id, model_name = key
        if cached is None:
            gallery = _decode(repo.gallery_rows(company_id, model_name), fingerprint)
        else:
            gallery = self._refresh(repo, cached, key, fingerprint)
        self._store(key, gallery)
        return gallery

    def _store(self, key: tuple[int, str], gallery: Gallery) -> None:
        """Guarda la galería como la más reciente y descarta las menos usadas mientras se pase de
        memoria o de empresas (la recién guardada siempre se queda)."""
        with self._lock:
            self._items[key] = gallery
            self._items.move_to_end(key)
            self._checked[key] = time.monotonic()
            used = sum(g.nbytes for g in self._items.values())
            while len(self._items) > 1 and (len(self._items) > self.max_companies or used > self.max_bytes):
                evicted_key, evicted = self._items.popitem(last=False)
                self._checked.pop(evicted_key, None)
                used -= evicted.nbytes

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._checked.clear()

    @staticmethod
    def _refresh(
        repo: FaceEmbeddingRepository, cached: Gallery, key: tuple[int, str], fingerprint: Fingerprint
    ) -> Gallery:
        """Conserva las filas vigentes ya descifradas y descifra solo las muestras nuevas."""
        company_id, model_name = key
        current = dict(repo.gallery_index(company_id, model_name))
        known = set(cached.ids.tolist())
        new_ids = [sample_id for sample_id in current if sample_id not in known]
        if len(new_ids) > settings.FACE_GALLERY_INCREMENTAL_LIMIT:
            return _decode(repo.gallery_rows(company_id, model_name), fingerprint)
        kept = cached.rows(np.fromiter((int(i) in current for i in cached.ids), dtype=bool, count=cached.size))
        added = _decode(repo.gallery_rows(company_id, model_name, new_ids), fingerprint) if new_ids else None
        return _join(kept, added, fingerprint)


face_galleries = FaceGalleryCache(settings.FACE_GALLERY_CACHE_COMPANIES, settings.FACE_GALLERY_CACHE_MB * 1024 * 1024)
