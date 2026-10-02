"""Anti-spoofing pasivo: distingue un rostro real frente a la cámara de una foto impresa, una
pantalla o un video reproducido.

Modelos MiniFASNetV2 (recorte 2.7x) y MiniFASNetV1SE (recorte 4.0x) de Minivision
(Silent-Face-Anti-Spoofing, Apache 2.0), exportados a ONNX. Se promedian sus probabilidades,
igual que en la implementación de referencia. Complementa el reto de giro de cabeza: el reto
detecta fotos estáticas; este modelo detecta texturas, bordes y reflejos de pantallas/papel.
"""

from pathlib import Path

import cv2
import numpy as np

from app.facial_recognition.engine import DetectedFace
from app.facial_recognition.model_store import ANTISPOOF_V1SE_MODEL, ANTISPOOF_V2_MODEL, ensure_models

_INPUT_SIZE = 80
_REAL_CLASS = 1


def _scaled_crop(image_bgr: np.ndarray, face: DetectedFace, scale: float) -> np.ndarray:
    """Recorte centrado en el rostro, `scale` veces su tamaño, desplazado para no salir de la imagen."""
    h, w = image_bgr.shape[:2]
    scale = min((h - 1) / max(face.height, 1), (w - 1) / max(face.width, 1), scale)
    new_w, new_h = face.width * scale, face.height * scale
    cx, cy = face.x + face.width / 2, face.y + face.height / 2
    left, top = cx - new_w / 2, cy - new_h / 2
    left = min(max(0.0, left), w - 1 - new_w)
    top = min(max(0.0, top), h - 1 - new_h)
    crop = image_bgr[int(top) : int(top + new_h) + 1, int(left) : int(left + new_w) + 1]
    # Valores BGR sin normalizar: así se entrenó el modelo.
    return cv2.resize(crop, (_INPUT_SIZE, _INPUT_SIZE)).astype(np.float32).transpose(2, 0, 1)[None]


class MiniFasAntiSpoof:
    def __init__(self, models_dir: str | Path, auto_download: bool = True) -> None:
        import onnxruntime as ort

        paths = ensure_models(models_dir, download=auto_download, models=(ANTISPOOF_V2_MODEL, ANTISPOOF_V1SE_MODEL))
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        self._models = [
            (ort.InferenceSession(str(paths[m.filename]), options, providers=["CPUExecutionProvider"]), scale)
            for m, scale in ((ANTISPOOF_V2_MODEL, 2.7), (ANTISPOOF_V1SE_MODEL, 4.0))
        ]

    def real_probability(self, image_bgr: np.ndarray, face: DetectedFace) -> float:
        """Probabilidad (0-1) de que sea un rostro real presente frente a la cámara."""
        total = np.zeros(3, dtype=np.float64)
        for session, scale in self._models:
            logits = session.run(None, {"input": _scaled_crop(image_bgr, face, scale)})[0][0].astype(np.float64)
            exp = np.exp(logits - logits.max())
            total += exp / exp.sum()
        return round(float(total[_REAL_CLASS] / len(self._models)), 4)
