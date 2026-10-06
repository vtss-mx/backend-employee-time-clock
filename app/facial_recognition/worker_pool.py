"""Pool de workers de reconocimiento facial con cola acotada (1 worker por núcleo).

Diseño:
- N workers = núcleos de CPU disponibles para el proceso (respeta límites de Docker/cgroups).
- Cada worker tiene SU PROPIA instancia del motor (YuNet + SFace): no hay un lock global
  que serialice el procesamiento. Cada inferencia usa 1 hilo → 1 solicitud por núcleo.
- Las solicitudes que llegan cuando todos los workers están ocupados esperan en una cola
  FIFO acotada (`max_waiting`). Si la cola está llena se responde de inmediato
  (backpressure / fail-fast) en lugar de acumular peticiones y degradar todo el servicio.
- Una solicitud que espera más de `wait_timeout` segundos se rechaza con un error reintentable.
- Un worker de REPUESTO (`try_acquire`): la petición que ya tiene el suyo puede tomar otro que esté libre en ese
  instante para una parte de su análisis que corre en paralelo (la ráfaga, en otro núcleo). Nunca espera ni se
  adelanta a la fila: si alguien espera turno o no hay uno libre, la petición lo hace con el suyo, como siempre
  (bajo carga todo es secuencial y nadie espera de más).
"""

import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from app.core.observability import observed
from app.core.system import available_cpus  # noqa: F401  (reexportado)

logger = logging.getLogger(__name__)


class QueueFullError(RuntimeError):
    """La cola de espera alcanzó su capacidad máxima."""


class QueueTimeoutError(RuntimeError):
    """La solicitud esperó demasiado sin obtener un worker."""


@dataclass(frozen=True)
class PoolStats:
    workers: int
    busy: int
    waiting: int
    max_waiting: int
    processed: int
    rejected: int


class WorkerPool[T]:
    def __init__(self, factory: Callable[[int], T], size: int, max_waiting: int, wait_timeout: float) -> None:
        self.size = size
        self.max_waiting = max_waiting
        self.wait_timeout = wait_timeout
        # Los recursos se crean una vez (cargar modelos es costoso) y se reutilizan.
        self._idle: list[T] = [factory(i) for i in range(size)]
        self._cond = threading.Condition()
        self._waiting = 0
        self._next_ticket = 0
        self._serving_ticket = 0
        self._expired: set[int] = set()
        self._processed = 0
        self._rejected = 0

    @contextmanager
    def lease(self) -> Iterator[T]:
        """Obtiene un worker en orden de llegada (FIFO) y lo devuelve al terminar. La espera se mide
        (`face.queue_wait`: una cola llena o un tiempo agotado cuentan como falla)."""
        with observed("face.queue_wait"), self._cond:
            if not self._idle and self._waiting >= self.max_waiting:
                self._rejected += 1
                raise QueueFullError("Cola de reconocimiento facial llena")
            ticket = self._next_ticket
            self._next_ticket += 1
            self._waiting += 1
            try:
                # FIFO: solo avanza quien tiene el turno y cuando hay un worker libre.
                ok = self._cond.wait_for(
                    lambda: ticket == self._serving_ticket and bool(self._idle), timeout=self.wait_timeout
                )
                if not ok:
                    self._rejected += 1
                    # Cede el turno para no bloquear a los que vienen detrás.
                    self._skip_ticket(ticket)
                    raise QueueTimeoutError("Tiempo de espera agotado en la cola")
                resource = self._idle.pop()
                self._advance()
            finally:
                self._waiting -= 1
        try:
            yield resource
        finally:
            self.release(resource)

    def try_acquire(self) -> T | None:
        """Un worker libre AHORA para trabajo en paralelo de una petición que ya tiene el suyo, o None. Solo si nadie
        espera turno: un repuesto nunca se adelanta a la fila ni la hace esperar más (se devuelve con `release`)."""
        with self._cond:
            if not self._idle or self._waiting:
                return None
            return self._idle.pop()

    def release(self, resource: T) -> None:
        """Devuelve un worker (de `lease` o de `try_acquire`) y despierta al siguiente de la fila."""
        with self._cond:
            self._idle.append(resource)
            self._processed += 1
            self._cond.notify_all()

    def _skip_ticket(self, ticket: int) -> None:
        # Un ticket que expira se descarta para no detener la fila.
        if ticket == self._serving_ticket:
            self._advance()
        else:
            self._expired.add(ticket)

    def _advance(self) -> None:
        self._serving_ticket += 1
        while self._serving_ticket in self._expired:
            self._expired.discard(self._serving_ticket)
            self._serving_ticket += 1
        self._cond.notify_all()

    def stats(self) -> PoolStats:
        with self._cond:
            return PoolStats(
                workers=self.size,
                busy=self.size - len(self._idle),
                waiting=self._waiting,
                max_waiting=self.max_waiting,
                processed=self._processed,
                rejected=self._rejected,
            )
