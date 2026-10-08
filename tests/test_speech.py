"""El paquete de voz (`app/speech`) sin el modelo: decodificar clips reales con PyAV, reconocer el formato por su
contenido, medir voz y silencio, muestrear fotogramas, los números dichos con palabras en los siete idiomas, la
comparación tolerante de nombres, fechas y códigos, y el motor con su interruptor (sin modelo responde «no
disponible» y no reintenta en cada petición)."""

import logging
import time
from datetime import date

import numpy as np
import pytest

from app.core.config import settings
from app.models import VoiceQuestion
from app.speech import RealBackend, SpeechUnavailable, backend, engine, language_of, use_backend
from app.speech.audio import ClipUnreadable, container_of, inspect_clip, loudness
from app.speech.matching import (
    compare,
    match_code,
    match_date,
    match_day,
    match_month,
    match_name,
    match_sum,
    match_year,
    normalize,
)
from app.speech.numerals import split_compound, to_digits
from tests.media_support import make_clip

# ---------------------------------------------------------------- clips reales


def test_webm_and_mp4_clips_are_recognized_by_content_and_decoded():
    for container, expected in (("webm", "video/webm"), ("mp4", "video/mp4")):
        clip = make_clip(seconds=1.0, container=container)
        assert container_of(clip) == expected
        info = inspect_clip(clip, max_seconds=5.0, frames=3)
        assert info.content_type == expected and not info.truncated
        assert 0.8 <= info.seconds <= 1.3, info.seconds
        assert info.samples.dtype == np.float32 and info.samples.ndim == 1
        assert info.rms_dbfs > -20 and info.speech_ratio > 0.5
        assert len(info.frames) == 3 and all(frame.startswith(b"\xff\xd8") for frame in info.frames)


def test_silence_measures_as_inaudible_and_audio_only_has_no_frames():
    quiet = inspect_clip(make_clip(tone_hz=None, video=False), max_seconds=5.0, frames=3)
    assert quiet.rms_dbfs < -60 and quiet.speech_ratio == 0.0 and quiet.frames == ()
    assert inspect_clip(make_clip(video=False), max_seconds=5.0, frames=0).frames == ()


def test_a_long_clip_is_cut_and_reported_as_truncated():
    info = inspect_clip(make_clip(seconds=2.0), max_seconds=0.5, frames=2)
    assert info.truncated and info.seconds <= 0.5
    assert len(info.frames) == 2


def test_unreadable_clips_have_their_error():
    with pytest.raises(ClipUnreadable):
        inspect_clip(b"not media", max_seconds=5.0, frames=1)
    with pytest.raises(ClipUnreadable):  # sin audio
        inspect_clip(make_clip(audio=False), max_seconds=5.0, frames=1)
    with pytest.raises(ClipUnreadable):  # cabecera de WebM y basura detrás
        inspect_clip(b"\x1a\x45\xdf\xa3" + b"\x00" * 200, max_seconds=5.0, frames=1)
    assert container_of(b"OggS....") == "audio/ogg" and container_of(b"RIFF....WAVE") == "audio/wav"
    assert container_of(b"") is None


def test_loudness_of_an_empty_signal():
    assert loudness(np.zeros(0, dtype=np.float32)) == (-120.0, 0.0)
    rms, voiced = loudness(np.full(16_000, 0.5, dtype=np.float32))
    assert -7 < rms < -5 and voiced == 1.0


def test_a_frame_that_cannot_be_encoded_is_unreadable(monkeypatch):
    import cv2

    monkeypatch.setattr(cv2, "imencode", lambda *args, **kwargs: (False, None))
    with pytest.raises(ClipUnreadable):
        inspect_clip(make_clip(), max_seconds=5.0, frames=1)


# ---------------------------------------------------------------- números con palabras


@pytest.mark.parametrize(
    ("lang", "text", "expected"),
    [
        ("es", "quince de mayo de mil novecientos noventa", ["15", "de", "mayo", "de", "1990"]),
        ("es", "siete mil cuatrocientos doce", ["7412"]),
        ("es", "treinta y uno veintiuno", ["31", "21"]),
        ("es", "primero de enero", ["1", "de", "enero"]),
        ("en", "may fifteenth nineteen ninety", ["may", "15", "1990"]),
        ("en", "seven thousand four hundred twelve", ["7412"]),
        ("en", "one hundred and five", ["105"]),
        ("en", "twenty twenty five", ["2025"]),
        ("pt", "quinze de maio de mil novecentos e noventa", ["15", "de", "maio", "de", "1990"]),
        ("pt", "sete mil quatrocentos e doze", ["7412"]),
        ("fr", "le quinze mai mille neuf cent quatre vingt dix", ["le", "15", "mai", "1990"]),
        ("fr", "sept mille quatre cent douze", ["7412"]),
        ("fr", "soixante et onze", ["71"]),
        ("fr", "quatre vingts", ["80"]),
        ("de", "funfzehnter mai neunzehnhundertneunzig", ["15", "mai", "1990"]),
        ("de", "siebentausendvierhundertzwolf", ["7412"]),
        ("de", "einundzwanzig", ["21"]),
        ("it", "quindici maggio millenovecentonovanta", ["15", "maggio", "1990"]),
        ("it", "settemilaquattrocentododici", ["7412"]),
        ("it", "ventuno", ["21"]),
        ("es", "sin numeros aqui", ["sin", "numeros", "aqui"]),
    ],
)
def test_number_words_become_digits(lang, text, expected):
    assert to_digits(normalize(text), lang) == expected


def test_an_unknown_language_changes_nothing():
    assert to_digits(["siete", "mil"], "ja") == ["siete", "mil"]


def test_compound_splitting_rejects_what_is_not_a_number():
    assert split_compound("millenovecento", "it") == ["mille", "novecento"]
    assert split_compound("casa", "it") is None and split_compound("mille", "it") is None


# ---------------------------------------------------------------- comparación


def test_names_tolerate_accents_missing_surnames_and_company_suffixes():
    assert match_name("Ana María Ruiz Pérez", "ana maria riz perez").ok
    assert match_name("Ana María Ruiz Pérez", "Ana Maria Ruiz").ok  # sin el segundo apellido
    assert not match_name("Ana María Ruiz Pérez", "Pedro Páramo").ok
    assert match_name("Panificadora del Norte, S.A. de C.V.", "panificadora del norte").ok
    assert match_name("Recursos Humanos", "recursos humanos.").ok
    assert not match_name("Recursos Humanos", "").ok
    assert match_name("Ana", "ana").ok and not match_name("", "ana").ok
    # Una palabra larga contenida en una oída cuenta («hermosillo» dentro de «hermosilloplant»).
    assert match_name("Planta Hermosillo", "planta hermosilloplant").ok


def test_dates_in_any_spoken_form_per_language():
    birthday = date(1990, 5, 15)
    assert match_date(birthday, "15 de mayo de 1990", "es-MX").ok
    assert match_date(birthday, "quince de mayo de mil novecientos noventa", "es-MX").ok
    assert match_date(birthday, "15/05/1990", "es-MX").ok
    assert match_date(birthday, "May 15th, 1990.", "en-US").ok
    assert match_date(birthday, "the fifteenth of may nineteen ninety", "en-US").ok
    assert match_date(birthday, "15 de maio de 1990", "pt-BR").ok
    assert match_date(birthday, "le 15 mai 1990", "fr-FR").ok
    assert match_date(birthday, "15. Mai 1990", "de-DE").ok
    assert match_date(birthday, "15 maggio 1990", "it-IT").ok
    assert match_date(birthday, "quince de mayo del noventa", "es-ES").ok  # año en dos cifras
    assert match_date(birthday, "quince de setiembre de 1990", "es-MX").score < 1
    assert not match_date(birthday, "15 de mayo", "es-MX").ok  # sin año
    assert not match_date(birthday, "mayo de 1990", "es-MX").ok  # sin día
    assert match_date(date(1990, 5, 5), "5 de mayo de 1990", "es-MX").ok  # día y mes iguales: dos cifras


def test_the_month_of_birth_by_name_or_number_in_each_language():
    birthday = date(1990, 5, 15)
    assert match_month(birthday, "mayo", "es-MX").ok and match_month(birthday, "en mayo", "es-MX").score == 1.0
    assert match_month(birthday, "cinco", "es-MX").ok and match_month(birthday, "el mes cinco", "es-MX").ok
    assert match_month(birthday, "May", "en-US").ok and match_month(birthday, "mai", "fr-FR").ok
    assert match_month(birthday, "maggio", "it-IT").ok and match_month(birthday, "Mai", "de-DE").ok
    assert match_month(birthday, "maio", "pt-BR").ok
    assert not match_month(birthday, "marzo", "es-MX").ok  # otro mes
    assert not match_month(birthday, "tres", "es-MX").ok  # otro número
    assert not match_month(birthday, "cero", "es-MX").ok  # un número fuera de 1-12 no es un mes
    assert not match_month(birthday, "no sé", "es-MX").ok  # sin mes


def test_the_year_of_birth_full_or_last_two_digits():
    birthday = date(1990, 5, 15)
    assert match_year(birthday, "mil novecientos noventa", "es-MX").ok
    assert match_year(birthday, "1990", "es-MX").ok
    assert match_year(birthday, "noventa", "es-MX").ok  # dos últimas cifras
    assert match_year(birthday, "nineteen ninety", "en-US").ok
    assert not match_year(birthday, "1991", "es-MX").ok
    assert not match_year(birthday, "mil", "es-MX").ok


def test_the_day_of_birth_with_words_or_digits():
    birthday = date(1990, 5, 15)
    assert match_day(birthday, "quince", "es-MX").ok and match_day(birthday, "el 15", "es-MX").ok
    assert match_day(birthday, "the fifteenth", "en-US").ok
    assert not match_day(birthday, "dieciséis", "es-MX").ok
    assert not match_day(birthday, "no me acuerdo", "es-MX").ok


def test_the_arithmetic_sum_needs_the_total_not_the_addends():
    assert match_sum(11, "once", "es-MX").ok and match_sum(11, "11", "es-MX").score == 1.0
    assert match_sum(11, "son once", "es-MX").ok and match_sum(11, "eleven", "en-US").ok
    assert match_sum(11, "elf", "de-DE").ok and match_sum(11, "onze", "fr-FR").ok
    # Decir solo los sumandos (7 y 4) NO basta: hay que dar el resultado (11).
    assert not match_sum(11, "siete y cuatro", "es-MX").ok
    assert not match_sum(11, "doce", "es-MX").ok and not match_sum(11, "no sé", "es-MX").ok


def test_employee_codes_need_the_exact_digits_and_similar_letters():
    assert match_code("EMP-7412", "e m p siete cuatro uno dos", "es-MX").ok
    assert match_code("EMP-7412", "emp 7.412", "es-MX").ok
    assert match_code("7412", "siete mil cuatrocientos doce", "es-MX").ok
    assert not match_code("7412", "siete mil cuatrocientos dos", "es-MX").ok
    assert not match_code("EMP-7412", "xyz 7412", "es-MX").ok
    assert match_code("ABC", "a b c", "en-US").ok and match_code("ABC", "abc", "en-US").ok  # deletreado o junto
    assert match_code("Acme", "acme", "en-US").ok and not match_code("ABC", "xyz", "en-US").ok
    assert match_code("A-7", "siete", "es-MX").score == 0.7  # cifras bien, letra ausente: no pasa
    assert not match_code("A-7", "siete", "es-MX").ok


def test_compare_dispatches_by_question():
    assert compare(VoiceQuestion.BIRTH_DATE, "1990-05-15", "15 de mayo de 1990", "es-MX").ok
    assert compare(VoiceQuestion.EMPLOYEE_NUMBER, "7412", "7412", "es-MX").ok
    assert compare(VoiceQuestion.COMPANY_NAME, "Acme", "acme", "es-MX").ok
    # Repertorio ampliado 2026-10-07: nombre y apellidos son nombres; el mes, el año y el día salen de la fecha ISO; la
    # suma lleva su total ya calculado.
    assert compare(VoiceQuestion.FIRST_NAME, "Ana María", "ana maria", "es-MX").ok
    assert compare(VoiceQuestion.SURNAMES, "Ruiz Pérez", "ruiz perez", "es-MX").ok
    assert compare(VoiceQuestion.FIRST_SURNAME, "Ruiz", "ruiz", "es-MX").ok
    assert compare(VoiceQuestion.SECOND_SURNAME, "Pérez", "perez", "es-MX").ok
    assert compare(VoiceQuestion.BIRTH_MONTH, "1990-05-15", "mayo", "es-MX").ok
    assert compare(VoiceQuestion.BIRTH_YEAR, "1990-05-15", "noventa", "es-MX").ok
    assert compare(VoiceQuestion.BIRTH_DAY, "1990-05-15", "quince", "es-MX").ok
    assert compare(VoiceQuestion.ARITHMETIC_SUM, "11", "once", "es-MX").ok
    assert not compare(VoiceQuestion.ARITHMETIC_SUM, "11", "diez", "es-MX").ok
    assert language_of("pt-BR") == "pt" and language_of("en-US") == "en"


# ---------------------------------------------------------------- el motor y su interruptor


def test_without_the_model_the_engine_is_unavailable_and_pauses_before_retrying(monkeypatch, tmp_path):
    engine.reset_engine()
    monkeypatch.setattr(settings, "SPEECH_MODELS_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "SPEECH_MODELS_AUTO_DOWNLOAD", False)
    with pytest.raises(SpeechUnavailable):
        engine.transcribe(np.zeros(1600, dtype=np.float32), "es")
    status = engine.speech_engine_status()
    assert status["status"] == "unavailable" and "RuntimeError" in str(status["error"])
    # Dentro de la pausa, falla rápido sin volver a intentar cargar.
    calls = []
    monkeypatch.setattr(engine, "ensure_speech_model", lambda *a, **k: calls.append(1))
    with pytest.raises(SpeechUnavailable):
        engine.transcribe(np.zeros(1600, dtype=np.float32), "es")
    assert calls == []
    engine.reset_engine()
    assert engine.speech_engine_status()["status"] == "not_loaded"


def test_a_concurrent_load_fails_fast_instead_of_blocking(monkeypatch):
    engine.reset_engine()
    holder = engine._holder
    assert holder._lock.acquire(blocking=False)
    try:
        with pytest.raises(SpeechUnavailable, match="iniciando"):
            holder.model()
    finally:
        holder._lock.release()


def test_transcription_joins_segments_and_reports_confidence(monkeypatch):
    class Segment:
        def __init__(self, text, logprob, no_speech):
            self.text, self.avg_logprob, self.no_speech_prob = text, logprob, no_speech

    seen = {}

    class Model:
        def transcribe(self, samples, **kwargs):
            assert kwargs["language"] == "es" and kwargs["beam_size"] == 1
            seen["hotwords"] = kwargs["hotwords"]
            return iter([Segment(" Hola ", -0.2, 0.1), Segment("mundo", -0.4, 0.3)]), None

    engine.reset_engine()
    monkeypatch.setattr(engine._holder, "_model", Model())
    heard = engine.transcribe(np.zeros(1600, dtype=np.float32), "es", "Panificadora del Norte")
    assert heard.text == "Hola mundo" and heard.avg_logprob == -0.3 and heard.no_speech_prob == 0.3
    assert seen["hotwords"] == "Panificadora del Norte"
    engine.transcribe(np.zeros(1600, dtype=np.float32), "es")
    assert seen["hotwords"] is None  # fechas y números: sin vocabulario sugerido
    assert engine.speech_engine_status() == {"status": "ok", "error": None, "model": settings.SPEECH_MODEL_SIZE}

    class Silent:
        def transcribe(self, samples, **kwargs):
            return iter([]), None

    monkeypatch.setattr(engine._holder, "_model", Silent())
    assert engine.transcribe(np.zeros(1600, dtype=np.float32), "es").text == ""
    engine.reset_engine()


def test_the_model_is_built_with_the_configured_size_and_threads(monkeypatch, tmp_path):
    captured = {}

    class WhisperModel:
        def __init__(self, path, **kwargs):
            captured["path"] = path
            captured.update(kwargs)

    import sys
    import types

    fake = types.ModuleType("faster_whisper")
    fake.WhisperModel = WhisperModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)
    monkeypatch.setattr(engine, "ensure_speech_model", lambda directory, size, download: tmp_path / size)
    monkeypatch.setattr(settings, "FACE_WORKERS", 3)
    engine.reset_engine()
    assert isinstance(engine._holder.model(), WhisperModel)
    assert (
        captured["path"].endswith(settings.SPEECH_MODEL_SIZE) and captured["cpu_threads"] == settings.SPEECH_CPU_THREADS
    )
    assert captured["num_workers"] == 3 and captured["compute_type"] == "int8" and captured["local_files_only"]
    engine.reset_engine()


def test_the_real_backend_wires_audio_and_engine(monkeypatch):
    real = RealBackend()
    info = real.inspect(make_clip(), max_seconds=5.0, frames=1)
    assert len(info.frames) == 1

    def fake_transcribe(samples, language, hotwords=None):
        return engine.Transcript("ok", -0.1, 0.0)

    monkeypatch.setattr(engine, "transcribe", fake_transcribe)
    monkeypatch.setattr("app.speech.transcribe", fake_transcribe)
    assert real.transcribe(info.samples, "es").text == "ok"
    previous = backend()
    use_backend(None)
    assert isinstance(backend(), RealBackend)
    use_backend(previous)
    assert backend() is previous


def test_the_download_is_a_known_size_only():
    from app.speech.model_store import SIZES, model_files

    assert set(SIZES) == {"base", "small"}
    files = model_files("small")
    assert [f.filename for f in files] == ["config.json", "model.bin", "tokenizer.json", "vocabulary.txt"]
    assert all(f.url and f.url.startswith("https://huggingface.co/Systran/faster-whisper-small/") for f in files)
    with pytest.raises(ValueError, match="desconocido"):
        model_files("tiny")


def test_the_speech_logger_is_quiet(caplog):
    """La medición del tiempo no escribe en la base ni avisa de más: solo un INFO al cargar (no se carga aquí)."""
    with caplog.at_level(logging.INFO, logger="app.speech.engine"):
        assert time.monotonic() > 0
    assert caplog.records == []


def test_loudness_of_less_than_one_window_and_clips_with_many_or_no_frames():
    """Menos de 20 ms de audio no tiene ventanas (sin voz medible); un clip largo reduce su reserva de fotogramas a la
    mitad al pasar de 24 y uno sin fotogramas no entrega ninguno."""
    import io

    import av

    from app.speech import audio
    from tests.media_support import make_clip

    assert audio.loudness(np.zeros(100, dtype=np.float32)) == (-120.0, 0.0)
    long_clip = make_clip(seconds=3.0)  # 30 fotogramas > la reserva de 24
    assert len(audio.inspect_clip(long_clip, max_seconds=12.0, frames=3).frames) == 3
    with av.open(io.BytesIO(long_clip), mode="r") as container:
        assert len(audio._frames(container, 2, 12.0)) == 2
    with av.open(io.BytesIO(long_clip), mode="r") as container:
        assert audio._frames(container, 2, -1.0) == []  # ningún fotograma dentro del tiempo: ninguno


def test_a_loaded_model_is_reused_by_the_loader():
    engine.reset_engine()
    holder = engine._holder
    model = object()
    holder._model = model
    assert holder._load() is model and holder.model() is model
    engine.reset_engine()
