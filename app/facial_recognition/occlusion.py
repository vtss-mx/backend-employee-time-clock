"""Verificación física de oclusión de la parte inferior del rostro (cubrebocas).

CLIP solo "sospecha" un cubrebocas a partir de la apariencia global, y con selfies reales
(luz tenue, ángulo bajo, barba) da falsos positivos. Un cubrebocas, físicamente, tapa la nariz
y las mejillas. Aquí se mide qué fracción de esa zona tiene el MISMO color de piel que la frente
de la misma persona en la misma foto (comparación adaptativa en crominancia YCrCb), por lo que
es robusta a la iluminación y al tono de piel.

Medido sobre 473 rostros (Face-Mask-Detection + retratos reales):
    con cubrebocas  → mediana 0 % de piel visible
    sin cubrebocas  → mediana 95 % (percentil 5: 41 %)
"""

import cv2
import numpy as np

from app.facial_recognition.engine import DetectedFace

# Rango amplio de piel en YCrCb: solo para validar que la referencia (frente) sea piel.
_CR_RANGE = (133.0, 180.0)
_CB_RANGE = (70.0, 130.0)
_MIN_LUMA = 40.0
# Distancia máxima en crominancia (Cr, Cb) a la piel de referencia para contar como piel.
_CHROMA_TOLERANCE = 12.0
_MIN_REFERENCE_SKIN = 0.5


def _skin_like(pixels: np.ndarray) -> np.ndarray:
    y, cr, cb = pixels[:, 0], pixels[:, 1], pixels[:, 2]
    return (cr >= _CR_RANGE[0]) & (cr <= _CR_RANGE[1]) & (cb >= _CB_RANGE[0]) & (cb <= _CB_RANGE[1]) & (y >= _MIN_LUMA)


def _region(ycc: np.ndarray, x0: float, x1: float, y0: float, y1: float) -> np.ndarray | None:
    h, w = ycc.shape[:2]
    left, right = int(max(0, x0)), int(min(w, x1))
    top, bottom = int(max(0, y0)), int(min(h, y1))
    if right - left < 3 or bottom - top < 3:
        return None
    return ycc[top:bottom, left:right].reshape(-1, 3).astype(np.float32)


def lower_face_skin_ratio(image_bgr: np.ndarray, face: DetectedFace) -> float | None:
    """Fracción (0-1) de la banda nariz/mejillas con color de piel de la propia persona.

    None si no se puede medir con confianza (frente cubierta por cabello o gorra, rostro
    fuera del encuadre): en ese caso el llamador debe exigir más evidencia antes de bloquear.
    """
    lm = face.landmarks
    (left_x, left_y), (right_x, right_y) = sorted([lm.eye_a, lm.eye_b])
    eye_dist = max(1.0, right_x - left_x)
    eye_y = (left_y + right_y) / 2
    nose_y = lm.nose[1]
    mouth_y = (lm.mouth_a[1] + lm.mouth_b[1]) / 2
    ycc = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2YCrCb)

    # Lo que tapa un cubrebocas: mejillas a la altura de la nariz hasta el labio superior.
    lower = _region(
        ycc,
        left_x - 0.15 * eye_dist,
        right_x + 0.15 * eye_dist,
        nose_y - 0.10 * eye_dist,
        (nose_y + mouth_y) / 2 + 0.10 * eye_dist,
    )
    # Referencia de piel bajo la misma luz: entrecejo/frente, justo encima de los ojos.
    reference = _region(
        ycc, left_x + 0.10 * eye_dist, right_x - 0.10 * eye_dist, eye_y - 0.55 * eye_dist, eye_y - 0.25 * eye_dist
    )
    if lower is None or reference is None:
        return None
    reference_skin = _skin_like(reference)
    if reference_skin.mean() < _MIN_REFERENCE_SKIN:
        return None
    skin = reference[reference_skin]
    cr, cb = float(np.median(skin[:, 1])), float(np.median(skin[:, 2]))
    distance = np.hypot(lower[:, 1] - cr, lower[:, 2] - cb)
    return round(float((distance < _CHROMA_TOLERANCE).mean()), 4)
