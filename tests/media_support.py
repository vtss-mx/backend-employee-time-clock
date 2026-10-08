"""Clips de video y audio REALES para probar `app/speech/audio.py`, armados con PyAV como lo haría un navegador.

Chrome y Firefox graban WebM (VP8 + Opus); Safari, MP4 (H.264 + AAC). Cada clip se codifica al vuelo (unos KB) con un
tono o silencio en el audio y un cuadro de color en el video; así la prueba no depende de archivos binarios y usa el
mismo FFmpeg con que el servidor decodifica.
"""

import io
import math
from fractions import Fraction

import av
import numpy as np

SAMPLE_RATE = 48_000
FPS = 10


def _audio_frames(seconds: float, *, tone_hz: float | None) -> list[np.ndarray]:
    """Bloques de 960 muestras (20 ms a 48 kHz, lo que pide Opus): un tono (voz simulada) o silencio."""
    total = int(seconds * SAMPLE_RATE)
    times = np.arange(total) / SAMPLE_RATE
    if tone_hz is None:
        signal = np.zeros(total)
    else:
        # Un tono modulado en amplitud (como sílabas) para que las ventanas de 20 ms suenen distinto.
        signal = 0.5 * np.sin(2 * math.pi * tone_hz * times) * (0.6 + 0.4 * np.sin(2 * math.pi * 3 * times))
    pcm = (signal * 32767).astype(np.int16)
    return [pcm[i : i + 960] for i in range(0, total - total % 960, 960)]


def make_clip(
    *,
    seconds: float = 1.0,
    container: str = "webm",
    tone_hz: float | None = 440.0,
    video: bool = True,
    audio: bool = True,
) -> bytes:
    """Un clip como el de `MediaRecorder`: `webm` (VP8 + Opus) o `mp4` (H.264 + AAC), con o sin pista de video o de
    audio, con un tono (hay voz) o silencio."""
    buffer = io.BytesIO()
    with av.open(buffer, mode="w", format=container) as output:
        video_stream = None
        if video:
            video_stream = output.add_stream("libvpx" if container == "webm" else "libx264", rate=FPS)
            video_stream.width, video_stream.height = 160, 120
            video_stream.pix_fmt = "yuv420p"
        audio_stream = None
        if audio:
            audio_stream = output.add_stream("libopus" if container == "webm" else "aac", rate=SAMPLE_RATE)
            audio_stream.layout = "mono"
        if video_stream is not None:
            picture = np.zeros((120, 160, 3), dtype=np.uint8)
            picture[:, :, 2] = 180  # un cuadro rojizo (no hay rostro: `identity_of` lo dice)
            for index in range(int(seconds * FPS)):
                frame = av.VideoFrame.from_ndarray(picture, format="rgb24")
                frame.pts = index
                frame.time_base = Fraction(1, FPS)
                for packet in video_stream.encode(frame):
                    output.mux(packet)
            for packet in video_stream.encode(None):
                output.mux(packet)
        if audio_stream is not None:
            position = 0
            for block in _audio_frames(seconds, tone_hz=tone_hz):
                frame = av.AudioFrame.from_ndarray(block.reshape(1, -1), format="s16", layout="mono")
                frame.sample_rate = SAMPLE_RATE
                frame.pts = position
                frame.time_base = Fraction(1, SAMPLE_RATE)
                position += block.size
                for packet in audio_stream.encode(frame):
                    output.mux(packet)
            for packet in audio_stream.encode(None):
                output.mux(packet)
    return buffer.getvalue()
