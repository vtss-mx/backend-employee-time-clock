"""Voz y video de la verificación del registro facial (decisión del dueño del producto, 2026-10-06).

Tras las fotos válidas, el empleado responde en video tres preguntas sobre sus propios datos; el servidor saca del clip
su audio y unos fotogramas (`audio`), transcribe la voz EN ESTE SERVIDOR con faster-whisper (`engine`; el audio nunca
sale de la plataforma, regla 13 de la raíz) y compara lo dicho con el dato registrado (`matching`).

`backend()` es la pieza que los servicios usan: `inspect` (decodificar el clip) y `transcribe` (voz a texto). Las
pruebas la reemplazan con `use_backend` (como el bucket con `use_storage`), así ningún caso de la suite decodifica
video ni carga el modelo; el motor real tiene las suyas (`tests/test_speech.py`, `tests/test_speech_real.py`).
"""

from typing import Protocol

import numpy as np

from app.speech.audio import ClipInfo, ClipUnreadable, inspect_clip
from app.speech.engine import SpeechUnavailable, Transcript, language_of, speech_engine_status, transcribe


class SpeechBackend(Protocol):
    def inspect(self, data: bytes, *, max_seconds: float, frames: int) -> ClipInfo: ...

    def transcribe(self, samples: np.ndarray, language: str, hotwords: str | None = None) -> Transcript: ...


class RealBackend:
    """PyAV para el clip y faster-whisper para la voz."""

    def inspect(self, data: bytes, *, max_seconds: float, frames: int) -> ClipInfo:
        return inspect_clip(data, max_seconds=max_seconds, frames=frames)

    def transcribe(self, samples: np.ndarray, language: str, hotwords: str | None = None) -> Transcript:
        return transcribe(samples, language, hotwords)


_backend: SpeechBackend = RealBackend()


def backend() -> SpeechBackend:
    return _backend


def use_backend(replacement: SpeechBackend | None) -> None:
    """Reemplaza el motor (pruebas); None vuelve al real."""
    global _backend
    _backend = replacement if replacement is not None else RealBackend()


__all__ = [
    "ClipInfo",
    "ClipUnreadable",
    "RealBackend",
    "SpeechBackend",
    "SpeechUnavailable",
    "Transcript",
    "backend",
    "language_of",
    "speech_engine_status",
    "use_backend",
]
