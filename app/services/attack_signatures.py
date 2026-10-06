"""Lista de bloqueo de artefactos de ataques confirmados (decisión D6 del dueño del producto: solo huellas).

Cuando el ADMIN confirma un caso de fraude, las huellas pHash de sus capturas ("rostro:cuadro") se bloquean en su
empresa (`fraud_case_service`) y, si la misma huella se confirmó en `ATTACK_SIGNATURE_PLATFORM_COMPANIES` empresas,
en toda la plataforma. Cada intento facial compara sus capturas frontales contra la lista (señal KNOWN_ATTACK del
motor de riesgo): una captura coincide si su rostro Y su cuadro quedan a lo más a `FACE_PHASH_MAX_DISTANCE` bits de
una firma (las dos a la vez: una sola huella de 64 bits coincide por azar con demasiadas personas, P2).

La lista vive en la BD (compartida por todas las réplicas) y cada proceso la guarda en memoria
`ATTACK_SIGNATURE_CACHE_SECONDS` (lo que tarda una firma nueva en aplicarse en todas), con tope de
`ATTACK_SIGNATURE_MAX_LOADED` firmas: la comparación es una operación de NumPy sobre todas a la vez (microsegundos).
"""

import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np
from sqlalchemy.orm import Session

from app.core.config import settings
from app.facial_recognition.phash import from_hex, to_hex
from app.repositories.risk_repository import AttackSignatureRepository

#: Único tipo de firma por ahora: las huellas de una captura (la llave del dispositivo llega en la fase 2).
CAPTURE_PHASH = "CAPTURE_PHASH"


def capture_value(face_phash: int, frame_phash: int) -> str:
    """La firma de una captura: "pHash del rostro:pHash del cuadro" en hexadecimal."""
    return f"{to_hex(face_phash)}:{to_hex(frame_phash)}"


@dataclass(frozen=True)
class _Loaded:
    ids: np.ndarray
    faces: np.ndarray
    frames: np.ndarray
    #: Empresa de cada firma (0 = toda la plataforma).
    companies: np.ndarray


def _as_unsigned(values: list[int]) -> np.ndarray:
    return np.array(values, dtype=np.int64).view(np.uint64)


def _load(db: Session) -> _Loaded:
    rows = AttackSignatureRepository(db).active(datetime.now(UTC), settings.ATTACK_SIGNATURE_MAX_LOADED)
    faces, frames = [], []
    for _, value, _ in rows:
        face, frame = value.split(":")
        faces.append(from_hex(face))
        frames.append(from_hex(frame))
    return _Loaded(
        ids=np.array([row[0] for row in rows], dtype=np.int64),
        faces=_as_unsigned(faces),
        frames=_as_unsigned(frames),
        companies=np.array([row[2] or 0 for row in rows], dtype=np.int64),
    )


class SignatureCache:
    """Las firmas vigentes en memoria del proceso por unos segundos (validadas contra la BD al vencer)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value: tuple[float, _Loaded] | None = None

    def get(self, db: Session) -> _Loaded:
        now = time.monotonic()
        with self._lock:
            if self._value is not None and now - self._value[0] < settings.ATTACK_SIGNATURE_CACHE_SECONDS:
                return self._value[1]
        loaded = _load(db)
        with self._lock:
            self._value = (now, loaded)
        return loaded

    def clear(self) -> None:
        with self._lock:
            self._value = None


signature_cache = SignatureCache()


def matches(db: Session, company_id: int, captures: Sequence[tuple[int, int]]) -> list[int]:
    """Las firmas (de la empresa o de la plataforma) con que coincide alguna captura (rostro y cuadro cerca)."""
    loaded = signature_cache.get(db)
    if not captures or loaded.ids.size == 0:
        return []
    scope = (loaded.companies == 0) | (loaded.companies == company_id)
    limit = settings.FACE_PHASH_MAX_DISTANCE
    hit = np.zeros(loaded.ids.size, dtype=bool)
    for face, frame in captures:
        near_face = np.bitwise_count(loaded.faces ^ _as_unsigned([face])[0]) <= limit
        near_frame = np.bitwise_count(loaded.frames ^ _as_unsigned([frame])[0]) <= limit
        hit |= near_face & near_frame
    return [int(i) for i in loaded.ids[hit & scope]]
