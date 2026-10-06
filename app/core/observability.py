"""Observadores de rendimiento del backend: qué tarda cada petición dentro de la base y cada función clave.

Todo termina en los acumuladores en memoria de `app/core/perf_meter.py` (se guardan en lotes, nunca una escritura
por petición):

- **Tiempo de BD por petición**: el middleware del traceId pone un `RequestTimer` en `request_timer_var` y los
  eventos del motor (`before/after_cursor_execute`, registrados una vez para cualquier motor de SQLAlchemy) le suman el
  tiempo y el número de sentencias. Es un objeto mutable a propósito: los hilos del threadpool reciben una copia del
  contexto con el MISMO objeto. Fuera de una petición (mantenimiento, hilos de guardado) no hay temporizador y los
  eventos no hacen nada más que una lectura de la variable.
- **Funciones clave**: `observed("face.detect")` mide como decorador o como `with`: tiempo (histograma) y si
  terminó con excepción (`errors`). Se usa en el motor facial (detectar, alinear, embedding, anti-spoofing,
  accesorios, destello, pasos), la búsqueda en la galería, la prueba de vida, la cobranza, el mantenimiento, el
  bucket, la espera de una conexión de la base (`TimedQueuePool`) y los guardados en lote. Agregar una: un nombre
  `área.acción` en minúsculas y estable (el nombre es la llave de sus filas: uno nuevo es una serie nueva).
- **Espera de una conexión** (`db.acquire`): el pool del motor de PostgreSQL es `TimedQueuePool`, que mide lo que
  tarda en entregar una conexión (esperar una libre del pool o abrir una nueva hacia PgBouncer/PostgreSQL).

Costo medido (README, "Observabilidad de rendimiento"): unos microsegundos por petición y por sentencia, cero
consultas.
"""

import functools
import logging
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Final, Self

from sqlalchemy import Connection, Engine, event
from sqlalchemy.pool import ConnectionPoolEntry, QueuePool

from app.core.perf_meter import perf_meter

logger = logging.getLogger("app.performance")
#: Tipo de las mediciones de funciones (columna `kind`).
FUNCTION_KIND: Final = "FUNCTION"
#: Llave en `Connection.info` con el inicio de la sentencia en curso.
_STARTED: Final = "perf_statement_started"


@dataclass(slots=True)
class RequestTimer:
    """Tiempo (ms) y sentencias de la base de UNA petición."""

    db_ms: float = 0.0
    db_queries: int = 0


request_timer_var: ContextVar[RequestTimer | None] = ContextVar("request_timer", default=None)


@event.listens_for(Engine, "before_cursor_execute")
def _statement_started(conn: Connection, *_: Any) -> None:
    if request_timer_var.get() is not None:
        conn.info[_STARTED] = time.perf_counter()


@event.listens_for(Engine, "after_cursor_execute")
def _statement_finished(conn: Connection, *_: Any) -> None:
    started = conn.info.pop(_STARTED, None)
    timer = request_timer_var.get()
    if started is None or timer is None:
        return
    timer.db_ms += (time.perf_counter() - started) * 1000
    timer.db_queries += 1


class observed:
    """Mide una función clave: decorador (`@observed("billing.issue_charges")`) o bloque (`with observed(...)`).

    Cada uso del bloque es un objeto nuevo y el decorador crea uno por llamada: es seguro entre hilos. Medir nunca
    cambia el resultado: una excepción se cuenta como falla y sigue su camino."""

    __slots__ = ("_started", "name")

    def __init__(self, name: str) -> None:
        self.name = name
        self._started = 0.0

    def __enter__(self) -> Self:
        self._started = time.perf_counter()
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        elapsed = (time.perf_counter() - self._started) * 1000
        try:
            perf_meter.record(FUNCTION_KIND, self.name, elapsed, error=exc_type is not None)
        except Exception:  # medir jamás rompe lo medido (p. ej. la espera de una conexión de la base)
            logger.exception("No se pudo medir %s", self.name)

    def __call__[**P, R](self, function: Callable[P, R]) -> Callable[P, R]:
        name = self.name

        @functools.wraps(function)
        def measured(*args: P.args, **kwargs: P.kwargs) -> R:
            with observed(name):
                return function(*args, **kwargs)

        return measured


class TimedQueuePool(QueuePool):
    """El pool de siempre (`QueuePool`) que además mide cuánto tarda en entregar una conexión (`db.acquire`): esperar
    una libre cuando todas están ocupadas o abrir una nueva. Es la señal de un pool chico o de PgBouncer saturado."""

    def _do_get(self) -> ConnectionPoolEntry:
        with observed("db.acquire"):
            return super()._do_get()
