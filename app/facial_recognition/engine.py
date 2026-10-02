"""Contrato del motor de reconocimiento facial.

Cualquier implementación (OpenCV/SFace, InsightFace/ArcFace, etc.) debe cumplir
esta interfaz. El resto de la aplicación solo depende de ella.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np

Point = tuple[float, float]


@dataclass(frozen=True)
class FaceLandmarks:
    """5 puntos faciales en coordenadas de la imagen original (sin espejo)."""

    eye_a: Point
    eye_b: Point
    nose: Point
    mouth_a: Point
    mouth_b: Point


@dataclass(frozen=True)
class DetectedFace:
    x: int
    y: int
    width: int
    height: int
    score: float
    landmarks: FaceLandmarks
    # Datos crudos del detector (bbox + landmarks) que el motor necesita para alinear.
    raw: np.ndarray


class FaceEngine(ABC):
    #: Identificador del modelo de embeddings; se guarda junto a cada vector.
    model_name: str
    #: Dimensión del embedding generado.
    embedding_dim: int

    @abstractmethod
    def detect(self, image_bgr: np.ndarray, min_score: float) -> list[DetectedFace]:
        """Detecta rostros con puntuación >= min_score, ordenados de mayor a menor."""

    @abstractmethod
    def align(self, image_bgr: np.ndarray, face: DetectedFace) -> np.ndarray:
        """Devuelve el recorte del rostro alineado (BGR) usado para extraer el embedding."""

    @abstractmethod
    def embed(self, aligned_face_bgr: np.ndarray) -> np.ndarray:
        """Genera el embedding L2-normalizado (float32, shape=(embedding_dim,))."""

    def represent(self, image_bgr: np.ndarray, face: DetectedFace, aligned_face_bgr: np.ndarray) -> np.ndarray:
        """Embedding final del rostro. Los motores que combinan modelos lo sobrescriben."""
        return self.embed(aligned_face_bgr)
