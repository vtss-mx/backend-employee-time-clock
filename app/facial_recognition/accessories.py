"""Detección de accesorios que ocultan rasgos faciales: lentes, gorra/sombrero y cubrebocas.

Tecnología: CLIP ViT-B/32 (OpenAI, MIT) en modo zero-shot, ejecutado con ONNX Runtime.
La imagen de la cabeza se compara contra centroides de texto precalculados
(`data/clip_prompts.json`, generados con scripts/build_clip_prompts.py):

    p(accesorio) = sigmoid(100 · (cos(img, positivos) − cos(img, negativos)))

Ventajas: no requiere datos de entrenamiento propios, detecta variantes (lentes de sol,
viseras, gorros, cascos) y es fácil de ajustar con umbrales por variable de entorno.
"""

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from app.facial_recognition.engine import DetectedFace
from app.facial_recognition.model_store import CLIP_VISION_MODEL, ensure_models

_PROMPTS_FILE = Path(__file__).parent / "data" / "clip_prompts.json"
_MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
_STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)
_SIZE = 224


@dataclass(frozen=True)
class AccessoryScores:
    glasses: float
    headwear: float
    mask: float


def head_crop(image_rgb: np.ndarray, face: DetectedFace) -> Image.Image:
    """Recorte cuadrado que incluye la parte superior de la cabeza (para ver gorras)."""
    h, w = image_rgb.shape[:2]
    side = 1.9 * max(face.width, face.height)
    cx = face.x + face.width / 2
    cy = face.y + face.height / 2 - 0.15 * face.height
    left, top = int(cx - side / 2), int(cy - side / 2)
    box = (max(0, left), max(0, top), min(w, int(left + side)), min(h, int(top + side)))
    return Image.fromarray(image_rgb).crop(box)


class ClipAccessoryDetector:
    def __init__(self, models_dir: str | Path, auto_download: bool = True) -> None:
        import onnxruntime as ort

        path = ensure_models(models_dir, download=auto_download, models=(CLIP_VISION_MODEL,))
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1  # 1 hilo por inferencia: paralelismo vía workers
        options.inter_op_num_threads = 1
        self._session = ort.InferenceSession(
            str(path[CLIP_VISION_MODEL.filename]), options, providers=["CPUExecutionProvider"]
        )
        data = json.loads(_PROMPTS_FILE.read_text())
        self._scale = float(data["logit_scale"])
        self._centroids = {
            name: np.array([attr["positive"], attr["negative"]], dtype=np.float32)
            for name, attr in data["attributes"].items()
        }

    @staticmethod
    def _preprocess(crop: Image.Image) -> np.ndarray:
        crop = crop.convert("RGB")
        w, h = crop.size
        scale = _SIZE / min(w, h)
        crop = crop.resize((max(_SIZE, round(w * scale)), max(_SIZE, round(h * scale))), Image.Resampling.BICUBIC)
        w, h = crop.size
        left, top = (w - _SIZE) // 2, (h - _SIZE) // 2
        crop = crop.crop((left, top, left + _SIZE, top + _SIZE))
        arr = (np.asarray(crop, dtype=np.float32) / 255.0 - _MEAN) / _STD
        return arr.transpose(2, 0, 1)[None]

    def score(self, image_bgr: np.ndarray, face: DetectedFace) -> AccessoryScores:
        crop = head_crop(np.ascontiguousarray(image_bgr[:, :, ::-1]), face)
        # InferenceSession.run es thread-safe: varios workers la usan en paralelo sin lock.
        emb = self._session.run(["image_embeds"], {"pixel_values": self._preprocess(crop)})[0][0]
        emb = emb / np.linalg.norm(emb)

        def prob(name: str) -> float:
            logits = self._scale * (self._centroids[name] @ emb)
            return float(1.0 / (1.0 + np.exp(-(logits[0] - logits[1]))))

        return AccessoryScores(
            glasses=round(prob("glasses"), 4),
            headwear=round(prob("headwear"), 4),
            mask=round(prob("mask"), 4),
        )
