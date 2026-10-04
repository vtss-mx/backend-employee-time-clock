"""Registro de las FALLAS del sistema en la base de datos (ops.error_reports).

Solo lo que alguien tiene que corregir (decisión del dueño del producto: señal, no ruido), para que
el ADMIN lo vea y le dé seguimiento (pendiente, en proceso, en revisión, solucionado):

- Fallas del servidor en una petición HTTP: excepciones no controladas (500, con su stack trace) y
  fallas controladas de una dependencia (5xx: BD caída o lenta, motor facial, saturación). Las
  anota `error_response` y las reporta el middleware del traceId al terminar la petición, con su
  ruta, quién la hizo y la empresa.
- Errores registrados en el log (`logger.error` / `logger.exception`) en cualquier parte: hilos de
  mantenimiento, motor facial, canal en vivo, procesos de mejor esfuerzo dentro de una petición...
- Fallas del canal WebSocket del lado del servidor.
- Fallas de la aplicación web que reporta el navegador (`POST /api/client-errors`).

Un 4xx (validación, permisos, reglas de negocio, 404, 401, 429...) NO se guarda: es un resultado
normal, se responde con su código estable y queda en el log del proceso. La regla vive aquí
(`report` descarta lo que no es CRITICAL ni ERROR): ningún productor la puede rodear.

Sin frenar a nadie: `report` solo agrega a una cola en memoria acotada (si se llena, se cuenta lo
perdido y eso mismo se registra) y un hilo guarda en lotes cada ERROR_REPORT_FLUSH_SECONDS. Los
iguales se agrupan por huella (`fingerprint`): un error repetido un millón de veces es UNA fila con
su contador, y solo unas cuantas ocurrencias por vuelta guardan el detalle.
"""

import logging
import threading
import traceback
from collections import deque
from typing import Any

from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.error_events import ErrorEvent, is_recorded
from app.core.request_context import request_id_var, request_info_var
from app.repositories.error_report_repository import ErrorReportRepository

#: Logger propio: lo que falle al GUARDAR errores no se vuelve a reportar (sin ciclos).
logger = logging.getLogger("app.error_reporting")
#: Estos ya se reportan por la vía HTTP (con más contexto): no se duplican desde el log.
_REPORTED_ELSEWHERE = ("app.error_reporting", "app.core.exceptions", "app.access")


class ErrorReporter:
    """Cola en memoria (acotada) que se guarda en lotes. Seguro entre hilos."""

    def __init__(self, capacity: int) -> None:
        self._events: deque[ErrorEvent] = deque()
        self._capacity = capacity
        self._lock = threading.Lock()
        self.dropped = 0

    def report(self, event: ErrorEvent) -> None:
        """Nunca bloquea ni lanza: si la cola está llena, solo cuenta lo perdido. Lo que no es una
        falla (gravedad menor que ERROR, p. ej. un 4xx) se descarta: no va a la bandeja del ADMIN."""
        if not is_recorded(event.severity):
            return
        with self._lock:
            if len(self._events) >= self._capacity:
                self.dropped += 1
            else:
                self._events.append(event)

    def clear(self) -> None:
        """Descarta lo pendiente sin guardarlo (pruebas: cada una empieza con la cola vacía)."""
        with self._lock:
            self._events.clear()
            self.dropped = 0

    def flush(self) -> int:
        """Guarda lo acumulado (agrupado por huella). Devuelve cuántos eventos se guardaron.
        Si la BD no está, se registra en el log del proceso y se descartan (no se acumulan sin fin)."""
        with self._lock:
            events = list(self._events)
            self._events.clear()
            dropped, self.dropped = self.dropped, 0
        if dropped:
            events.append(
                ErrorEvent(
                    source="LOG",
                    severity="ERROR",
                    code="ERROR_REPORTS_DROPPED",
                    message=f"Se perdieron {dropped} reportes de error: la cola de registro estaba llena",
                    location="app/services/error_reporter.py",
                )
            )
        if not events:
            return 0
        groups: dict[str, list[ErrorEvent]] = {}
        for event in events:
            groups.setdefault(event.fingerprint, []).append(event)
        saved = 0
        try:
            with SessionLocal() as db:
                repo = ErrorReportRepository(db)
                # En orden de huella (dos procesos no se interbloquean) y cada error en su transacción:
                # uno que no se puede guardar no hace perder a los demás del lote.
                for fingerprint in sorted(groups):
                    saved += self._save(db, repo, fingerprint, groups[fingerprint])
        except Exception:
            logger.exception("No se pudieron guardar %s reportes de error", len(events) - saved)
        return saved

    @staticmethod
    def _save(db: Session, repo: ErrorReportRepository, fingerprint: str, events: list[ErrorEvent]) -> int:
        """Un error en su transacción. Un interbloqueo con otro proceso se reintenta una vez; si la BD
        sigue fallando, se deja de intentar con el resto del lote. Un error que no se puede guardar
        (dato inválido) se omite y se registra."""
        retries = 1
        while True:
            try:
                repo.record(fingerprint, events, keep=settings.ERROR_REPORT_OCCURRENCES_PER_FLUSH)
                db.commit()
                return len(events)
            except OperationalError:
                db.rollback()
                if not retries:
                    raise
                retries -= 1
            except SQLAlchemyError:
                db.rollback()
                logger.exception("No se pudo guardar el reporte de error %s (se omite)", fingerprint[:12])
                return 0


error_reporter = ErrorReporter(settings.ERROR_REPORT_BUFFER)


class ErrorLogHandler(logging.Handler):
    """Todo `logger.error` / `logger.exception` del backend se vuelve un reporte (salvo los que ya
    reporta la vía HTTP). Así ningún error en segundo plano pasa desapercibido."""

    def __init__(self, reporter: ErrorReporter) -> None:
        super().__init__(level=logging.ERROR)
        self.reporter = reporter

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(_REPORTED_ELSEWHERE):
            return
        try:
            exc_type = record.exc_info[0].__name__ if record.exc_info and record.exc_info[0] else None
            detail = "".join(traceback.format_exception(*record.exc_info)) if record.exc_info else None
            self.reporter.report(
                ErrorEvent(
                    source="LOG",
                    severity="CRITICAL",
                    code=record.name[:120],
                    message=record.getMessage()[:1000],
                    location=f"{record.pathname.rsplit('/app/', 1)[-1]}:{record.lineno}"[:255],
                    exception_type=exc_type,
                    detail=detail,
                    trace_id=request_id_var.get(),
                    context=_log_context(record),
                )
            )
        except Exception:  # el registro de errores jamás debe romper a quien escribe en el log
            self.handleError(record)


def _log_context(record: logging.LogRecord) -> dict[str, Any]:
    """Dónde se registró y, si fue dentro de una petición, cuál y de quién."""
    context: dict[str, Any] = {"logger": record.name, "thread": record.threadName, "function": record.funcName}
    info = request_info_var.get()
    if info is not None:
        context["request"] = {"method": info.method, "path": info.path}
        context["user"] = (
            {"id": info.user_id, "email": info.user_email, "role": info.user_role} if info.user_id else None
        )
        context["company_id"] = info.company_id
    return context


def install_log_handler(reporter: ErrorReporter = error_reporter) -> None:
    """Conecta el log del proceso con el registro de errores (una sola vez)."""
    root = logging.getLogger()
    if not any(isinstance(h, ErrorLogHandler) for h in root.handlers):
        root.addHandler(ErrorLogHandler(reporter))


class ErrorReportFlusher:
    """Hilo que guarda los reportes cada `interval` segundos (y lo pendiente al apagar)."""

    def __init__(self, interval: float, reporter: ErrorReporter = error_reporter) -> None:
        self.interval = interval
        self.reporter = reporter
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="error-reports", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self.reporter.flush()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.reporter.flush()
            except Exception:
                logger.exception("Falló el guardado periódico de reportes de error")
