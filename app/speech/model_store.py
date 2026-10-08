"""Los modelos de voz a texto (Whisper convertidos a CTranslate2 por Systran, licencia MIT): descarga fija y verificada.

Decisión del dueño del producto (2026-10-06): la voz del empleado se transcribe EN NUESTRO SERVIDOR con un modelo
abierto; el audio nunca sale de la plataforma (regla 13 de la raíz). Los archivos se bajan al construir la imagen
(`Dockerfile`: `python -m app.speech.model_store /app/models/speech`), con su SHA-256 fijo, como los modelos faciales
(`app/facial_recognition/model_store.py`, que hace la descarga y la verificación); nunca desde una petición.

Un modelo es una carpeta con cuatro archivos (`config.json`, `model.bin`, `tokenizer.json`, `vocabulary.txt`), uno por
tamaño: `small` (484 MB, el de omisión: medido el 2026-10-06 sobre voces sintéticas en los siete idiomas, acierta las
fechas y los nombres; ≈ 3 s por respuesta con un hilo, ≈ 1.5 s con dos) y `base` (145 MB, 3 veces más rápido pero
confunde nombres y fechas en español). Se cambia con `SPEECH_MODEL_SIZE`.

Uso manual:  python -m app.speech.model_store [carpeta] [tamaños...]
"""

import logging
import os
import sys
from pathlib import Path

from app.facial_recognition.model_store import ModelFile, ensure_models

logger = logging.getLogger(__name__)

_HUB = "https://huggingface.co/Systran/faster-whisper-{size}/resolve/main/{name}"

#: Los archivos de cada tamaño con su SHA-256 (calculado al descargarlos el 2026-10-06; el tokenizador y el
#: vocabulario son los mismos para todos los tamaños multilingües).
_TOKENIZER_SHA = "fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab"
_VOCABULARY_SHA = "34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913"
_WEIGHTS = {
    "base": (
        "56a6d8110d311f19c8f0471e562832c7527f146b567275bfca59fcf7c184da9a",
        "d01c3014881c9c6f3133c182f3d2887eb6ca1c789a7538c5c007196857a0a6a9",
    ),
    "small": (
        "b55496ac7940a7ae47d2c01eab40edfd8701feec1229d9cce3b40014383fb828",
        "3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671",
    ),
}
#: Tamaños disponibles (descargados, verificados y medidos el 2026-10-06).
SIZES: tuple[str, ...] = tuple(_WEIGHTS)


def model_files(size: str) -> tuple[ModelFile, ...]:
    """Los cuatro archivos de un tamaño, con su URL fija y su SHA-256."""
    if size not in SIZES:
        raise ValueError(f"Tamaño de modelo de voz desconocido: {size} (válidos: {', '.join(SIZES)})")
    config_sha, weights_sha = _WEIGHTS[size]
    return (
        ModelFile("config.json", _HUB.format(size=size, name="config.json"), config_sha),
        ModelFile("model.bin", _HUB.format(size=size, name="model.bin"), weights_sha),
        ModelFile("tokenizer.json", _HUB.format(size=size, name="tokenizer.json"), _TOKENIZER_SHA),
        ModelFile("vocabulary.txt", _HUB.format(size=size, name="vocabulary.txt"), _VOCABULARY_SHA),
    )


def ensure_speech_model(models_dir: str | Path, size: str, download: bool = True) -> Path:
    """La carpeta del modelo de ese tamaño con sus archivos verificados (descargados si faltan y `download`)."""
    directory = Path(models_dir) / size
    ensure_models(directory, download, models=model_files(size))
    return directory


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    target = sys.argv[1] if len(sys.argv) > 1 else os.getenv("SPEECH_MODELS_DIR", "./models/speech")
    for chosen in sys.argv[2:] or ("small",):
        print(f"OK  {chosen} -> {ensure_speech_model(target, chosen)}")
