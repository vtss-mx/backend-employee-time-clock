"""Voz a texto en el servidor: faster-whisper (Whisper en CTranslate2, int8, CPU) cargado una vez por proceso.

Como el motor facial (`app/facial_recognition`): carga perezosa con candado sin espera (una petición que llega mientras
otro hilo carga el modelo responde 503 al instante) y pausa tras una falla (`FACE_ENGINE_RETRY_SECONDS`: no se
reintenta en cada petición). El modelo se comparte entre los hilos (CTranslate2 atiende transcripciones en paralelo con
`num_workers`; cada una usa `SPEECH_CPU_THREADS` hilos): el paralelismo real lo dan los workers faciales, uno por
núcleo, porque la ruta que transcribe ya tiene reservado el suyo.

La transcripción corre con el idioma de la petición (nunca se adivina: la pregunta se hizo en ese idioma) y sin
contexto previo ni marcas de tiempo: respuestas cortas de una o dos frases.
"""

import logging
import threading
import time
from dataclasses import dataclass
from statistics import fmean
from typing import Any

import numpy as np

from app.core.config import settings
from app.core.observability import observed
from app.core.system import available_cpus
from app.speech.model_store import ensure_speech_model

logger = logging.getLogger(__name__)


class SpeechUnavailable(RuntimeError):
    """El modelo de voz no está cargado ni se pudo cargar; el resto de la API sigue operando."""


@dataclass(frozen=True)
class Transcript:
    """Lo que se oyó: el texto y cuánto confía el modelo (promedio del logaritmo de la probabilidad de sus palabras y
    probabilidad de que no hubiera voz)."""

    text: str
    avg_logprob: float
    no_speech_prob: float


def language_of(locale: str) -> str:
    """El código de idioma de Whisper para un idioma de la API (`es-MX` → `es`, `pt-BR` → `pt`)."""
    return locale.split("-")[0].lower()


class _Holder:
    def __init__(self) -> None:
        self._model: Any | None = None
        self._lock = threading.Lock()
        self._last_failure = 0.0
        self._last_error: str | None = None

    def model(self) -> Any:
        if self._model is not None:
            return self._model
        if not self._lock.acquire(blocking=False):
            raise SpeechUnavailable("El modelo de voz se está iniciando")
        try:
            return self._load()
        finally:
            self._lock.release()

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        if self._last_failure and time.monotonic() - self._last_failure < settings.FACE_ENGINE_RETRY_SECONDS:
            raise SpeechUnavailable(self._last_error or "Modelo de voz no disponible")
        try:
            self._model = self._build()
            self._last_error = None
            logger.info("Modelo de voz listo: whisper %s (int8, CPU)", settings.SPEECH_MODEL_SIZE)
            return self._model
        except Exception as exc:
            self._last_failure = time.monotonic()
            self._last_error = f"{exc.__class__.__name__}: {exc}"
            logger.exception("No se pudo cargar el modelo de voz")
            raise SpeechUnavailable(self._last_error) from exc

    @staticmethod
    def _build() -> Any:
        from faster_whisper import WhisperModel

        path = ensure_speech_model(
            settings.SPEECH_MODELS_DIR, settings.SPEECH_MODEL_SIZE, download=settings.SPEECH_MODELS_AUTO_DOWNLOAD
        )
        workers = settings.FACE_WORKERS or max(1, available_cpus() // settings.API_WORKERS)
        return WhisperModel(
            str(path),
            device="cpu",
            compute_type="int8",
            cpu_threads=settings.SPEECH_CPU_THREADS,
            num_workers=workers,
            local_files_only=True,
        )

    def status(self) -> dict[str, Any]:
        if self._model is not None:
            return {"status": "ok", "error": None, "model": settings.SPEECH_MODEL_SIZE}
        return {"status": "unavailable" if self._last_error else "not_loaded", "error": self._last_error}

    def reset(self) -> None:
        """Olvida el modelo y la última falla (pruebas)."""
        self._model = None
        self._last_failure = 0.0
        self._last_error = None


_holder = _Holder()


@observed("speech.transcribe")
def transcribe(samples: np.ndarray, language: str, hotwords: str | None = None) -> Transcript:
    """Transcribe el audio (mono, 16 kHz, -1..1) en `language` (código de Whisper), con `hotwords` como vocabulario
    sugerido (el dato registrado de una pregunta de texto: mejora los nombres propios sin regalar la respuesta).
    `SpeechUnavailable` si el modelo no está disponible; cualquier otra falla del motor sigue su camino (quien llama la
    vuelve 503)."""
    model = _holder.model()
    segments, _ = model.transcribe(
        samples,
        language=language,
        beam_size=1,
        vad_filter=False,
        condition_on_previous_text=False,
        without_timestamps=True,
        hotwords=hotwords,
    )
    heard = list(segments)
    if not heard:
        return Transcript(text="", avg_logprob=-10.0, no_speech_prob=1.0)
    return Transcript(
        text=" ".join(segment.text.strip() for segment in heard).strip(),
        avg_logprob=round(fmean(float(segment.avg_logprob) for segment in heard), 4),
        no_speech_prob=round(max(float(segment.no_speech_prob) for segment in heard), 4),
    )


def speech_engine_status() -> dict[str, Any]:
    return _holder.status()


def reset_engine() -> None:
    _holder.reset()
