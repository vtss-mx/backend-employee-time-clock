"""El modelo de voz REAL (faster-whisper de la imagen de Docker) con voces sintéticas de macOS (`say`): la respuesta en
español y en inglés se transcribe y coincide con el dato registrado con las reglas del producto. El resto de la suite
usa un motor simulado; esta prueba comprueba que el modelo de la imagen carga y que una respuesta real pasa. La imagen
de desarrollo expone el modelo en TEST_SPEECH_MODELS_DIR (/opt/models/speech)."""

import os
import time
import wave
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from app.core.config import settings
from app.speech import engine
from app.speech.matching import match_date, match_name

MODELS_DIR = os.environ.get("TEST_SPEECH_MODELS_DIR", "")
pytestmark = pytest.mark.skipif(
    not MODELS_DIR or not (Path(MODELS_DIR) / settings.SPEECH_MODEL_SIZE).exists(),
    reason="Sin el modelo de voz (córrelas en la imagen de desarrollo)",
)
FIXTURES = Path(__file__).parent / "fixtures"


def _pcm(name: str) -> np.ndarray:
    with wave.open(str(FIXTURES / name)) as source:
        assert source.getframerate() == 16_000 and source.getnchannels() == 1
        frames = source.readframes(source.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


@pytest.fixture(autouse=True)
def _real_model(monkeypatch):
    monkeypatch.setattr(settings, "SPEECH_MODELS_DIR", MODELS_DIR)
    monkeypatch.setattr(settings, "SPEECH_MODELS_AUTO_DOWNLOAD", False)
    engine.reset_engine()
    yield
    engine.reset_engine()


def test_a_spanish_name_and_an_english_date_are_understood_and_matched():
    started = time.perf_counter()
    name = engine.transcribe(_pcm("voice_es_mx_name.wav"), "es")
    birthday = engine.transcribe(_pcm("voice_en_us_date.wav"), "en")
    elapsed = time.perf_counter() - started
    assert match_name("Ana María Ruiz Pérez", name.text).ok, name
    assert match_date(date(1990, 5, 15), birthday.text, "en-US").ok, birthday
    assert name.no_speech_prob < settings.SPEECH_MAX_NO_SPEECH_PROB
    assert name.avg_logprob > settings.SPEECH_MIN_AVG_LOGPROB
    assert elapsed < 60, elapsed  # dos respuestas cortas con el modelo small (medido: ≈ 1.5-3 s cada una)
    assert engine.speech_engine_status()["status"] == "ok"
