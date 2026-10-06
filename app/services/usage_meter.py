"""Medidor de consumo: cuánto usa cada empresa y cada usuario la plataforma (base para monitorear y
para fijar precios), SIN trabajo de base de datos dentro de las peticiones.

- `record` (lo llama el middleware del traceId al terminar cada petición, y el canal WebSocket por
  mensaje) solo suma en memoria, bajo un candado, en tres granos: empresa/día, empresa/día/ruta y
  empresa/día/usuario. Unos microsegundos; nunca bloquea, nunca lanza, nunca toca la BD.
- Un hilo (`UsageFlusher`) guarda lo acumulado cada USAGE_FLUSH_SECONDS con UPSERT atómicos que
  SUMAN (`ON CONFLICT ... DO UPDATE SET requests = requests + EXCLUDED.requests ...`): varias
  instancias escriben las mismas filas sin perder ni duplicar. Las filas van en orden de llave (dos
  instancias no se interbloquean) y cada grano en su transacción.
- **Memoria acotada**: a lo más USAGE_METER_MAX_KEYS llaves entre lotes; una ruta o un usuario nuevo
  que ya no cabe se suma a `OTHER` / usuario 0 de su empresa (se conserva el total, solo se pierde el
  desglose y se cuenta en `folded`). El grano por empresa siempre cabe: lo acotan las empresas reales
  (el id sale de la sesión, nunca del cliente).
- **Nada se pierde si la BD falla**: lo que no se pudo guardar vuelve al acumulador (con el mismo tope)
  y se guarda en la siguiente vuelta; la falla queda registrada para el ADMIN (log de error →
  `ops.error_reports`). Al apagar se guarda lo pendiente.

El día es el del negocio (hora del Centro) en que terminó la petición.
"""

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, fields
from datetime import date
from typing import Any

from app.core.clock import business_day_bounds, business_today
from app.core.config import settings
from app.core.database import platform_session
from app.core.observability import observed
from app.models import UsageDaily, UsageRoute, UsageUser
from app.repositories.usage_repository import UsageRepository

logger = logging.getLogger("app.usage")

#: Ruta de lo que no corresponde a ninguna ruta de la API (o ya no cupo en memoria).
OTHER_ROUTE = "OTHER"
#: Empresa de lo que no es de una empresa (ADMIN, inicio de sesión, salud) y usuario anónimo.
NO_COMPANY = 0
ANONYMOUS = 0


@dataclass(slots=True)
class Tally:
    """Contadores de un grano (los mismos que las columnas de `UsageCounters`)."""

    requests: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    duration_ms: float = 0.0
    max_ms: float = 0.0
    server_errors: int = 0
    client_errors: int = 0

    def add(self, other: Tally) -> None:
        self.requests += other.requests
        self.bytes_in += other.bytes_in
        self.bytes_out += other.bytes_out
        self.duration_ms += other.duration_ms
        self.max_ms = max(self.max_ms, other.max_ms)
        self.server_errors += other.server_errors
        self.client_errors += other.client_errors

    def row(self) -> dict[str, int]:
        """Columnas de la fila (el tiempo en ms enteros, redondeado)."""
        values = {f.name: getattr(self, f.name) for f in fields(self)}
        values["duration_ms"] = round(self.duration_ms)
        values["max_ms"] = round(self.max_ms)
        return values


def tally_of(bytes_in: int, bytes_out: int, duration_ms: float, status: int) -> Tally:
    """Una petición (o un mensaje del canal) como contadores."""
    return Tally(
        requests=1,
        bytes_in=max(bytes_in, 0),
        bytes_out=max(bytes_out, 0),
        duration_ms=max(duration_ms, 0.0),
        max_ms=max(duration_ms, 0.0),
        server_errors=1 if status >= 500 else 0,
        client_errors=1 if 400 <= status < 500 else 0,
    )


type DailyKey = tuple[int, date]
type RouteKey = tuple[int, date, str]
type UserKey = tuple[int, date, int]


@dataclass
class Batch:
    """Lo acumulado entre dos guardados."""

    daily: dict[DailyKey, Tally]
    routes: dict[RouteKey, Tally]
    users: dict[UserKey, Tally]

    def __len__(self) -> int:
        return len(self.daily) + len(self.routes) + len(self.users)


def _empty() -> Batch:
    return Batch({}, {}, {})


class _BusinessDay:
    """El día del negocio sin calcular la zona horaria en cada petición: se recalcula al cambiar de día."""

    def __init__(self) -> None:
        self._day = date.min
        self._ends_at = 0.0

    def today(self) -> date:
        now = time.time()
        if now >= self._ends_at:
            self._day = business_today()
            self._ends_at = business_day_bounds(self._day)[1].timestamp()
        return self._day


class UsageMeter:
    """Acumulador en memoria (acotado) que se guarda en lotes. Seguro entre hilos."""

    def __init__(self, max_keys: int, *, enabled: bool = True) -> None:
        self.max_keys = max_keys
        self.enabled = enabled
        self._lock = threading.Lock()
        self._batch = _empty()
        self._clock = _BusinessDay()
        #: Rutas o usuarios que se sumaron a OTHER / usuario 0 por falta de lugar (desde el arranque).
        self.folded = 0

    def record(
        self,
        *,
        company_id: int | None,
        user_id: int | None,
        route: str,
        bytes_in: int,
        bytes_out: int,
        duration_ms: float,
        status: int,
        day: date | None = None,
        requests: int = 1,
    ) -> None:
        """Suma una petición. Solo memoria: nunca toca la BD ni lanza (lo llama cada petición). `requests=0`
        suma solo datos (una respuesta del canal en vivo)."""
        if not self.enabled:
            return
        company = company_id or NO_COMPANY
        when = day or self._clock.today()
        tally = tally_of(bytes_in, bytes_out, duration_ms, status)
        tally.requests = requests
        with self._lock:
            self._merge(self._batch, company, when, route[:160], user_id or ANONYMOUS, tally)

    def _merge(self, batch: Batch, company: int, day: date, route: str, user: int, tally: Tally) -> None:
        """Suma `tally` en los tres granos (con el candado tomado), respetando el tope de llaves."""
        full = len(batch) >= self.max_keys
        _add(batch.daily, (company, day), tally)
        route_key = (company, day, route)
        if full and route_key not in batch.routes:
            route_key, self.folded = (company, day, OTHER_ROUTE), self.folded + 1
        _add(batch.routes, route_key, tally)
        user_key = (company, day, user)
        if full and user_key not in batch.users:
            user_key, self.folded = (company, day, ANONYMOUS), self.folded + 1
        _add(batch.users, user_key, tally)

    def pending(self) -> int:
        """Llaves acumuladas sin guardar (estado del servidor y pruebas)."""
        with self._lock:
            return len(self._batch)

    def clear(self) -> None:
        """Descarta lo pendiente sin guardarlo (pruebas: cada una empieza vacía)."""
        with self._lock:
            self._batch = _empty()
            self.folded = 0

    def flush(self) -> int:
        """Guarda lo acumulado; devuelve cuántas filas se sumaron. Un grano que no se pudo guardar vuelve
        al acumulador (se reintenta en la siguiente vuelta) y la falla se registra."""
        with self._lock:
            batch, self._batch = self._batch, _empty()
        if not batch:
            return 0
        saved = 0
        grains: tuple[tuple[Any, dict[Any, Tally], Callable[[Any], dict[str, Any]]], ...] = (
            (UsageDaily, batch.daily, lambda k: {"company_id": k[0], "day": k[1]}),
            (UsageRoute, batch.routes, lambda k: {"company_id": k[0], "day": k[1], "route": k[2]}),
            (UsageUser, batch.users, lambda k: {"company_id": k[0], "day": k[1], "user_id": k[2]}),
        )
        failed = _empty()
        for model, tallies, columns in grains:
            if not tallies:
                continue
            rows = [{**columns(key), **tallies[key].row()} for key in sorted(tallies)]
            try:
                with platform_session() as db:
                    UsageRepository(db).add_counts(model, rows)
                    db.commit()
                saved += len(rows)
            except Exception:
                logger.exception(
                    "No se pudo guardar el consumo de %s (se reintenta en la siguiente vuelta)", model.__tablename__
                )
                getattr(failed, _GRAIN_FIELD[model]).update(tallies)
        if failed:
            self._restore(failed)
        return saved

    def _restore(self, failed: Batch) -> None:
        """Lo que no se guardó vuelve al acumulador, sumado a lo que llegó mientras tanto y con el mismo
        tope (una ruta o un usuario que ya no cabe va a OTHER / usuario 0 de su empresa)."""
        with self._lock:
            batch = self._batch
            for key, tally in failed.daily.items():
                _add(batch.daily, key, tally)
            for (company, day, route), tally in failed.routes.items():
                fits = len(batch) < self.max_keys or (company, day, route) in batch.routes
                _add(batch.routes, (company, day, route if fits else OTHER_ROUTE), tally)
            for (company, day, user), tally in failed.users.items():
                fits = len(batch) < self.max_keys or (company, day, user) in batch.users
                _add(batch.users, (company, day, user if fits else ANONYMOUS), tally)


_GRAIN_FIELD = {UsageDaily: "daily", UsageRoute: "routes", UsageUser: "users"}


def _add[K](grain: dict[K, Tally], key: K, tally: Tally) -> None:
    current = grain.get(key)
    if current is None:
        grain[key] = Tally()
        current = grain[key]
    current.add(tally)


usage_meter = UsageMeter(settings.USAGE_METER_MAX_KEYS, enabled=settings.USAGE_METER_ENABLED)


class UsageFlusher:
    """Hilo que guarda el consumo cada `interval` segundos (y lo pendiente al apagar). Nunca muere: una
    vuelta que falla se registra y la siguiente sigue."""

    def __init__(self, interval: float, meter: UsageMeter = usage_meter) -> None:
        self.interval = interval
        self.meter = meter
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="usage-meter", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self.meter.flush()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                with observed("usage.flush"):
                    self.meter.flush()
            except Exception:
                logger.exception("Falló el guardado periódico del consumo")
