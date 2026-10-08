"""Lo que se saca de un clip de video grabado por el navegador: su audio para la transcripción y unos fotogramas.

`MediaRecorder` entrega WebM (VP8/VP9 + Opus) en Chrome, Edge y Firefox y MP4 (H.264 + AAC) en Safari; el formato se
reconoce por su CONTENIDO (nunca por lo que declare el cliente) y se decodifica con PyAV (FFmpeg incluido en la rueda,
sin instalar nada en el sistema): es la misma biblioteca con que faster-whisper lee audio, así que no agrega una
dependencia nueva (regla 6 de la raíz).

Todo acotado: se decodifican como mucho `max_seconds` de audio (lo demás se recorta y se informa) y los fotogramas se
conservan en una reserva pequeña que se adelgaza al crecer (un clip de 20 s nunca ocupa cientos de MB). Solo CPU, sin
transacción abierta (quien llama cierra la suya antes).
"""

import io
import logging
from dataclasses import dataclass
from typing import Any

import av
import cv2
import numpy as np

logger = logging.getLogger(__name__)

#: Frecuencia de muestreo que espera Whisper.
SAMPLE_RATE = 16_000
#: Ventana con que se mide la presencia de voz (20 ms) y qué tan alta debe sonar una ventana para contar como voz.
_WINDOW = SAMPLE_RATE // 50
_VOICED_DBFS = -38.0
#: Fotogramas que se conservan como mucho mientras se decodifica (se adelgazan a la mitad al llenarse).
_RESERVOIR = 24
#: Calidad JPEG de los fotogramas que se entregan al motor facial.
_JPEG_QUALITY = 90
_EBML = b"\x1a\x45\xdf\xa3"


class ClipUnreadable(Exception):
    """El archivo no es un video o audio que se pueda leer (formato, archivo truncado, sin pista de audio)."""


@dataclass(frozen=True)
class ClipInfo:
    """Lo que se midió del clip (sin guardar nada): su formato, su audio y unos fotogramas en JPEG."""

    content_type: str
    #: Audio mono a 16 kHz en punto flotante (-1..1), recortado a `max_seconds`.
    samples: np.ndarray
    #: Duración del audio decodificado (s) y si el clip traía más de lo que se decodificó.
    seconds: float
    truncated: bool
    #: Sonoridad media (dBFS) y fracción de ventanas de 20 ms con voz.
    rms_dbfs: float
    speech_ratio: float
    #: Fotogramas repartidos a lo largo del clip (JPEG), para comprobar el rostro.
    frames: tuple[bytes, ...]


def container_of(data: bytes) -> str | None:
    """El tipo de contenido por los primeros bytes: WebM (Matroska), MP4 (`ftyp`), Ogg o WAV; None si no es ninguno."""
    if data[:4] == _EBML:
        return "video/webm"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "video/mp4"
    if data[:4] == b"OggS":
        return "audio/ogg"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    return None


def loudness(samples: np.ndarray) -> tuple[float, float]:
    """(sonoridad media en dBFS, fracción de ventanas de 20 ms con voz) de un audio en punto flotante."""
    if samples.size == 0:
        return -120.0, 0.0
    rms = float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))
    windows = samples[: samples.size - samples.size % _WINDOW].reshape(-1, _WINDOW)
    voiced = 0.0
    if len(windows):
        energy = np.sqrt(np.mean(np.square(windows, dtype=np.float64), axis=1))
        voiced = float(np.mean(_dbfs(energy) > _VOICED_DBFS))
    return _dbfs(np.array(rms)).item(), voiced


def _dbfs(value: np.ndarray) -> np.ndarray:
    return 20.0 * np.log10(np.maximum(value, 1e-6))


def inspect_clip(data: bytes, *, max_seconds: float, frames: int) -> ClipInfo:
    """Decodifica el clip: su audio (hasta `max_seconds`) y `frames` fotogramas repartidos. `ClipUnreadable` si no es
    un archivo de video o audio legible o no trae audio."""
    content_type = container_of(data)
    if content_type is None:
        raise ClipUnreadable("el archivo no es WebM, MP4, Ogg ni WAV")
    try:
        with av.open(io.BytesIO(data), mode="r") as container:
            samples, truncated = _audio(container, max_seconds)
            pictures = _frames(container, frames, max_seconds) if frames > 0 else ()
    except ClipUnreadable:
        raise
    except Exception as exc:  # FFmpeg no lo pudo leer: archivo truncado o un formato que no es el que dice
        raise ClipUnreadable(f"{type(exc).__name__}: {exc}") from exc
    rms, voiced = loudness(samples)
    return ClipInfo(
        content_type=content_type,
        samples=samples,
        seconds=round(samples.size / SAMPLE_RATE, 3),
        truncated=truncated,
        rms_dbfs=round(rms, 2),
        speech_ratio=round(voiced, 3),
        frames=tuple(pictures),
    )


def _audio(container: Any, max_seconds: float) -> tuple[np.ndarray, bool]:
    """El audio mono a 16 kHz, decodificado hasta `max_seconds` (y si había más)."""
    stream = next((s for s in container.streams if s.type == "audio"), None)
    if stream is None:
        raise ClipUnreadable("el clip no trae audio")
    resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
    limit = int(max_seconds * SAMPLE_RATE)
    chunks: list[np.ndarray] = []
    total = 0
    truncated = False
    for frame in container.decode(stream):
        for resampled in resampler.resample(frame):
            chunk = resampled.to_ndarray().reshape(-1)
            chunks.append(chunk)
            total += chunk.size
        if total > limit:
            truncated = True
            break
    for resampled in resampler.resample(None):  # lo que el remuestreador aún tenía
        chunks.append(resampled.to_ndarray().reshape(-1))
    joined = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.int16)
    return joined[:limit].astype(np.float32) / 32768.0, truncated


def _frames(container: Any, count: int, max_seconds: float) -> list[bytes]:
    """`count` fotogramas repartidos a lo largo del clip, en JPEG. Sin pista de video, ninguno."""
    stream = next((s for s in container.streams if s.type == "video"), None)
    if stream is None:
        return []
    stream.thread_count = 1  # un worker por núcleo: cada petición usa un hilo
    container.seek(0)
    kept: list[np.ndarray] = []
    for frame in container.decode(stream):
        if frame.time is not None and frame.time > max_seconds:
            break
        kept.append(frame.to_ndarray(format="bgr24"))
        if len(kept) > _RESERVOIR:
            kept = kept[::2]
    if not kept:
        return []
    chosen = sorted({round(k * (len(kept) - 1) / max(count, 1)) for k in range(1, count + 1)})
    return [_jpeg(kept[index]) for index in chosen]


def _jpeg(picture: np.ndarray) -> bytes:
    ok, encoded = cv2.imencode(".jpg", picture, [cv2.IMWRITE_JPEG_QUALITY, _JPEG_QUALITY])
    if not ok:
        raise ClipUnreadable("no se pudo codificar un fotograma")
    return encoded.tobytes()
