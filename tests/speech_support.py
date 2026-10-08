"""Motor de voz simulado para las pruebas (el real tiene las suyas: `test_speech.py` y `test_speech_real.py`).

Un clip es texto: `b"clip:<persona>|<lo que dijo>|opción=valor,..."`. La persona es la de sus fotogramas (el motor
facial falso los lee como `b"face:<persona>"`; `noface` = sin rostro); lo dicho es lo que «transcribe»; las opciones
cambian lo medido: `seconds` (duración), `rms` (dBFS), `ratio` (ventanas con voz), `frames` (cuántos fotogramas),
`truncated=1` (el clip traía más de lo que se decodificó), `mp4=1` (formato), `nospeech` (probabilidad de sin voz),
`logprob` (confianza), `frame=crash|broken` (el motor facial falla con el fotograma). Lo que no empieza con `clip:`
no es un video (`ClipUnreadable`). `down` hace que transcribir falle como si el modelo no estuviera; `crash`, como una
falla inesperada del motor.
"""

import numpy as np

from app.speech import ClipInfo, ClipUnreadable, SpeechUnavailable, Transcript
from app.speech.audio import SAMPLE_RATE


def voice_clip(person: str, said: str, **options: object) -> bytes:
    """El clip simulado de `person` diciendo `said` (con las opciones del módulo)."""
    extra = ",".join(f"{key}={value}" for key, value in options.items())
    return f"clip:{person}|{said}|{extra}".encode()


class FakeSpeech:
    def __init__(self) -> None:
        self.down = False
        self.crash = False
        #: Lo último que se "oyó" (el texto viaja del clip a la transcripción por aquí) y los idiomas pedidos.
        self.said: str = ""
        self.languages: list[str] = []
        #: El vocabulario sugerido que llegó con cada transcripción (None en fechas y números).
        self.hotwords: list[str | None] = []

    def inspect(self, data: bytes, *, max_seconds: float, frames: int) -> ClipInfo:
        text = data.decode(errors="ignore")
        if not text.startswith("clip:"):
            raise ClipUnreadable("no es un clip simulado")
        person, _, rest = text[5:].partition("|")
        said, _, extra = rest.partition("|")
        options = dict(item.split("=", 1) for item in extra.split(",") if item)
        self.said = said
        self._unclear = (
            ("nospeech", float(options.get("nospeech", "0.1"))),
            ("logprob", float(options.get("logprob", "-0.3"))),
        )
        seconds = min(float(options.get("seconds", "2.0")), max_seconds)
        frame = options.get("frame")
        picture = (frame or f"face:{person}").encode()
        count = int(options.get("frames", str(frames)))
        return ClipInfo(
            content_type="video/mp4" if options.get("mp4") == "1" else "video/webm",
            samples=np.zeros(int(seconds * SAMPLE_RATE), dtype=np.float32),
            seconds=seconds,
            truncated=options.get("truncated") == "1",
            rms_dbfs=float(options.get("rms", "-20")),
            speech_ratio=float(options.get("ratio", "0.5")),
            frames=tuple(picture for _ in range(count)),
        )

    def transcribe(self, samples: np.ndarray, language: str, hotwords: str | None = None) -> Transcript:
        self.languages.append(language)
        self.hotwords.append(hotwords)
        if self.down:
            raise SpeechUnavailable("simulado: sin modelo")
        if self.crash:
            raise RuntimeError("simulado: el motor se cayó")
        (_, no_speech), (_, logprob) = self._unclear
        return Transcript(text=self.said, avg_logprob=logprob, no_speech_prob=no_speech)
