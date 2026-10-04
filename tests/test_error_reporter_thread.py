"""Hilo que guarda los reportes de error en lotes y el puente del log: nunca se detiene por una falla
ni rompe a quien escribe en el log (un error al REGISTRAR errores no puede tumbar el servicio)."""

import logging
import threading

from app.core.database import SessionLocal
from app.core.error_events import ErrorEvent
from app.models import ErrorReport
from app.services.error_reporter import ErrorLogHandler, ErrorReporter, ErrorReportFlusher

#: Tope de espera de cada prueba con hilos: si el hilo no avanza, la prueba falla en lugar de colgarse.
WAIT_SECONDS = 5


def _event(code: str) -> ErrorEvent:
    return ErrorEvent(source="LOG", severity="ERROR", code=code, message=f"falla {code}")


def _saved_codes() -> set[str]:
    with SessionLocal() as db:
        return {report.code for report in db.query(ErrorReport)}


class _FlakyReporter(ErrorReporter):
    """Cola real cuya primera vuelta falla (p. ej. la BD se cae a la mitad del guardado); avisa cada
    vuelta para que la prueba sepa cuándo el hilo ya pasó por la falla."""

    def __init__(self) -> None:
        super().__init__(capacity=10)
        self.rounds = 0
        self.recovered = threading.Event()

    def flush(self) -> int:
        self.rounds += 1
        if self.rounds == 1:
            raise RuntimeError("BD caída durante el guardado")
        saved = super().flush()
        if saved:
            self.recovered.set()
        return saved


def test_the_flusher_saves_periodically_and_survives_a_failed_round(caplog):
    reporter = _FlakyReporter()
    reporter.report(_event("PERIODICO"))
    flusher = ErrorReportFlusher(0.01, reporter)
    with caplog.at_level(logging.ERROR, logger="app.error_reporting"):
        flusher.start()
        try:
            assert reporter.recovered.wait(WAIT_SECONDS)  # la vuelta siguiente a la falla sí guardó
        finally:
            flusher.stop()
    assert "PERIODICO" in _saved_codes()
    assert any("guardado periódico" in r.getMessage() and r.exc_info for r in caplog.records)
    assert not flusher._thread.is_alive()  # se detuvo al apagar (no quedó colgado)


def test_stopping_saves_what_is_still_pending():
    """Al apagar se guarda lo pendiente aunque el intervalo aún no se cumpla (nada se pierde)."""
    reporter = ErrorReporter(capacity=10)
    flusher = ErrorReportFlusher(3600, reporter)
    flusher.start()
    reporter.report(_event("AL_APAGAR"))
    flusher.stop()
    assert not flusher._thread.is_alive()
    assert "AL_APAGAR" in _saved_codes()
    assert reporter.flush() == 0  # ya no quedó nada en la cola


def test_a_broken_log_record_never_breaks_the_caller(monkeypatch):
    """Un registro mal formado (argumentos que no coinciden con el mensaje) no se convierte en
    reporte ni lanza: se delega al manejo de errores estándar de `logging`."""
    reporter = ErrorReporter(capacity=10)
    handler = ErrorLogHandler(reporter)
    failures: list[logging.LogRecord] = []
    monkeypatch.setattr(handler, "handleError", failures.append)
    record = logging.LogRecord(
        "app.services.demo", logging.ERROR, "/app/app/services/demo.py", 7, "%s y %s", ("uno",), None
    )

    handler.emit(record)

    assert failures == [record]
    assert reporter.flush() == 0  # nada quedó a medias en la cola
