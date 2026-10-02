"""Descarga y verificación (SHA-256) de los modelos ONNX.

- Descargados: YuNet y SFace (OpenCV Model Zoo, Apache 2.0) y CLIP ViT-B/32 (MIT).
- Incluidos en el proyecto (`data/models/`, convertidos con scripts/export_models.py):
  FaceNet-512 VGGFace2 int8 (facenet-pytorch, MIT) y MiniFASNet V2/V1SE (Minivision, Apache 2.0).

Uso manual:  python -m app.facial_recognition.model_store
"""

import hashlib
import logging
import os
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


BUNDLED_DIR = Path(__file__).parent / "data" / "models"


@dataclass(frozen=True)
class ModelFile:
    filename: str
    url: str | None  # None = incluido en el proyecto (data/models)
    sha256: str


_ZOO = "https://github.com/opencv/opencv_zoo/raw/main/models"

DETECTOR_MODEL = ModelFile(
    filename="face_detection_yunet_2023mar.onnx",
    url=f"{_ZOO}/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    sha256="8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
)
RECOGNIZER_MODEL = ModelFile(
    filename="face_recognition_sface_2021dec.onnx",
    url=f"{_ZOO}/face_recognition_sface/face_recognition_sface_2021dec.onnx",
    sha256="0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
)
# CLIP ViT-B/32 (OpenAI, licencia MIT), codificador de imagen exportado a ONNX y
# cuantizado (int8) por Xenova/transformers.js. Se usa para detectar accesorios.
CLIP_VISION_MODEL = ModelFile(
    filename="clip_vit_b32_vision_quantized.onnx",
    url="https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/onnx/vision_model_quantized.onnx",
    sha256="583fd1110a514667812fee7d684952aaf82a99b959760c8d7dca7e0ab9839299",
)
# FaceNet Inception-ResNet-v1 entrenado con VGGFace2 (512-d), exportado a ONNX y cuantizado int8.
# En LFW (6000 pares) la fusión SFace + FaceNet reduce los rechazos erróneos 2.7x (ver README).
FACENET_MODEL = ModelFile(
    filename="facenet512_vggface2_int8.onnx",
    url=None,
    sha256="f04ed2d11af2158951c760cfbaf9b83aafb32c702acac2220ccdf7b680981b20",
)
# Anti-spoofing pasivo (fotos impresas, pantallas, videos): MiniFASNet de Minivision.
ANTISPOOF_V2_MODEL = ModelFile(
    filename="2.7_80x80_MiniFASNetV2.onnx",
    url=None,
    sha256="d3b8349a948aa4195389e01a5565bb11380713312f03ba7cfdc31dcea762d48d",
)
ANTISPOOF_V1SE_MODEL = ModelFile(
    filename="4_0_0_80x80_MiniFASNetV1SE.onnx",
    url=None,
    sha256="90d3434c3e20e1d045f437da30c6d37aead9dc8f2d93db1f490bd28f8da6a9a2",
)
MODELS = (DETECTOR_MODEL, RECOGNIZER_MODEL, CLIP_VISION_MODEL)
BUNDLED_MODELS = (FACENET_MODEL, ANTISPOOF_V2_MODEL, ANTISPOOF_V1SE_MODEL)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_models(
    models_dir: str | Path, download: bool = True, models: tuple[ModelFile, ...] = MODELS
) -> dict[str, Path]:
    """Garantiza que los modelos existan y su hash sea el esperado."""
    directory = Path(models_dir)
    directory.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for model in models:
        if model.url is None:
            paths[model.filename] = _bundled(model)
            continue
        path = directory / model.filename
        if path.exists() and _sha256(path) == model.sha256:
            paths[model.filename] = path
            continue
        if not download:
            raise RuntimeError(
                f"Modelo {model.filename} no encontrado en {directory}. "
                "Ejecuta: python -m app.facial_recognition.model_store"
            )
        logger.info("Descargando modelo %s ...", model.filename)
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            urllib.request.urlretrieve(model.url, tmp_path)  # noqa: S310 (URL fija https)
            actual = _sha256(tmp_path)
            if actual != model.sha256:
                raise RuntimeError(f"Hash inválido para {model.filename}: esperado {model.sha256}, obtenido {actual}")
            os.replace(tmp_path, path)
        finally:
            tmp_path.unlink(missing_ok=True)
        paths[model.filename] = path
    return paths


def _bundled(model: ModelFile) -> Path:
    path = BUNDLED_DIR / model.filename
    if not path.exists() or _sha256(path) != model.sha256:
        raise RuntimeError(f"Modelo incluido {model.filename} ausente o alterado (hash SHA-256 distinto)")
    return path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    target = sys.argv[1] if len(sys.argv) > 1 else os.getenv("FACE_MODELS_DIR", "./models")
    for name, file_path in ensure_models(target).items():
        print(f"OK  {name} -> {file_path}")
