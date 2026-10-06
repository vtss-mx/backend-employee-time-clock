"""Acumuladores en memoria de la observabilidad de rendimiento: lo que miden los observadores SIN tocar la base.

Patrón de `usage_meter` (regla 15 del `AGENTS.md` raíz: telemetría en lote, nunca una escritura por petición):

- `PerfMeter.record` (lo llaman el middleware del traceId al terminar cada petición, `observed` al salir de cada
  función medida y la ingesta del navegador) solo suma, bajo un candado, en la llave (minuto, tipo, nombre):
  conteo, fallas, tiempo total y máximo, tiempo y consultas de la BD, bytes y el histograma de cubetas fijas
  (`app/core/histogram.py`). Microsegundos; nunca lanza ni toca la BD.
- `SlowRequestLog.record` (regla 18) agrupa por ruta las peticiones que pasaron el umbral: contador, tiempos, el
  último traceId y una muestra del contexto (sin secretos ni cuerpos). Nunca una fila por petición.
- Un hilo (`app/services/perf_store.py`) se lleva lo acumulado (`drain`) y lo guarda en lotes con UPSERT que SUMAN;
  si la BD falla, lo devuelve (`restore`) con el mismo tope y se guarda en la siguiente vuelta.
- **Memoria acotada**: `PERF_METER_MAX_KEYS` llaves y `SLOW_REQUEST_MAX_ROUTES` rutas entre lotes; lo que ya no
  cabe se suma a `OTHER` de su minuto y tipo (se conserva el total y se cuenta en `folded`).

Vive en `app/core` (sin dependencias de la BD) para que cualquier capa pueda medir —el motor facial, el pool de
conexiones, el bucket— sin ciclos de importación.
"""

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.core.histogram import BUCKETS, COLUMNS, bucket_of, percentile

#: Nombre de lo que ya no cupo en memoria (o no corresponde a ninguna ruta conocida).
OTHER = "OTHER"
#: Tope del nombre de una llave (la columna `name` y la ruta de una alerta miden 160).
NAME_LIMIT = 160


@dataclass(slots=True)
class Stat:
    """Contadores de una llave (minuto, tipo, nombre): los mismos que las columnas de `PerfCounters`."""

    count: int = 0
    errors: int = 0
    client_errors: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0
    db_ms: float = 0.0
    db_queries: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    buckets: list[int] = field(default_factory=lambda: [0] * BUCKETS)

    def add(self, other: Stat) -> None:
        self.count += other.count
        self.errors += other.errors
        self.client_errors += other.client_errors
        self.total_ms += other.total_ms
        self.max_ms = max(self.max_ms, other.max_ms)
        self.db_ms += other.db_ms
        self.db_queries += other.db_queries
        self.bytes_in += other.bytes_in
        self.bytes_out += other.bytes_out
        for index, value in enumerate(other.buckets):
            self.buckets[index] += value

    @classmethod
    def of(cls, row: Any) -> Stat:
        """Las sumas de una fila de `ops.perf_*` (o de una consulta que las agrupa), con su histograma."""
        return cls(
            count=int(row.count or 0),
            errors=int(row.errors or 0),
            client_errors=int(row.client_errors or 0),
            total_ms=float(row.total_ms or 0),
            max_ms=float(row.max_ms or 0),
            db_ms=float(row.db_ms or 0),
            db_queries=int(row.db_queries or 0),
            bytes_in=int(row.bytes_in or 0),
            bytes_out=int(row.bytes_out or 0),
            buckets=[int(getattr(row, column) or 0) for column in COLUMNS],
        )

    def quantile(self, q: float) -> float:
        """El percentil `q` de su histograma (interpolado, sin pasar del máximo real)."""
        return percentile(self.buckets, q, self.max_ms)

    @property
    def avg_ms(self) -> float:
        return round(self.total_ms / self.count, 1) if self.count else 0.0

    def row(self) -> dict[str, Any]:
        """Las columnas de su fila (tiempos con 3 decimales: microsegundos)."""
        values: dict[str, Any] = {
            "count": self.count,
            "errors": self.errors,
            "client_errors": self.client_errors,
            "total_ms": round(self.total_ms, 3),
            "max_ms": round(self.max_ms, 3),
            "db_ms": round(self.db_ms, 3),
            "db_queries": self.db_queries,
            "bytes_in": self.bytes_in,
            "bytes_out": self.bytes_out,
        }
        values.update(zip(COLUMNS, self.buckets, strict=True))
        return values


#: (inicio del minuto en segundos UNIX, tipo, nombre).
type MetricKey = tuple[int, str, str]


def minute_of(moment: float) -> int:
    """Inicio del minuto (segundos UNIX, UTC) de un instante."""
    return int(moment) // 60 * 60


class PerfMeter:
    """Acumulador por minuto (acotado) que se guarda en lotes. Seguro entre hilos."""

    def __init__(self, max_keys: int, *, enabled: bool = True) -> None:
        self.max_keys = max_keys
        self.enabled = enabled
        self._lock = threading.Lock()
        self._stats: dict[MetricKey, Stat] = {}
        #: Llaves que se sumaron a OTHER por falta de lugar (desde el arranque).
        self.folded = 0

    def record(
        self,
        kind: str,
        name: str,
        ms: float,
        *,
        error: bool = False,
        client_error: bool = False,
        db_ms: float = 0.0,
        db_queries: int = 0,
        bytes_in: int = 0,
        bytes_out: int = 0,
        at: float | None = None,
    ) -> None:
        """Suma una medición. Solo memoria: nunca toca la BD ni lanza (lo llama cada petición)."""
        if not self.enabled:
            return
        duration = max(ms, 0.0)
        bucket = bucket_of(duration)
        minute = minute_of(time.time() if at is None else at)
        with self._lock:
            key = (minute, kind, name[:NAME_LIMIT])
            stat = self._stats.get(key)
            if stat is None:
                key, stat = self._slot(key)
            stat.count += 1
            stat.errors += error
            stat.client_errors += client_error
            stat.total_ms += duration
            stat.max_ms = max(stat.max_ms, duration)
            stat.db_ms += max(db_ms, 0.0)
            stat.db_queries += max(db_queries, 0)
            stat.bytes_in += max(bytes_in, 0)
            stat.bytes_out += max(bytes_out, 0)
            stat.buckets[bucket] += 1

    def _slot(self, key: MetricKey) -> tuple[MetricKey, Stat]:
        """La llave nueva si cabe; si no, la de OTHER de su minuto y tipo (con el candado tomado)."""
        if len(self._stats) >= self.max_keys:
            self.folded += 1
            key = (key[0], key[1], OTHER)
        return key, self._stats.setdefault(key, Stat())

    def drain(self) -> dict[MetricKey, Stat]:
        """Se lleva lo acumulado (el guardado en lotes) y deja el acumulador vacío."""
        with self._lock:
            stats, self._stats = self._stats, {}
        return stats

    def restore(self, stats: dict[MetricKey, Stat]) -> None:
        """Lo que no se pudo guardar vuelve, sumado a lo que llegó mientras tanto y con el mismo tope."""
        with self._lock:
            for key, stat in stats.items():
                current = self._stats.get(key)
                if current is None:
                    key, current = self._slot(key)
                current.add(stat)

    def pending(self) -> int:
        """Llaves acumuladas sin guardar (estado del servidor y pruebas)."""
        with self._lock:
            return len(self._stats)

    def clear(self) -> None:
        """Descarta lo pendiente sin guardarlo (pruebas: cada una empieza vacía)."""
        with self._lock:
            self._stats = {}
            self.folded = 0


@dataclass(slots=True)
class SlowStat:
    """Las peticiones lentas de una ruta entre dos guardados (una fila de `ops.slow_request_alerts`)."""

    count: int
    total_ms: float
    max_ms: float
    last_ms: float
    first_at: float
    last_at: float
    last_trace_id: str | None
    last_status: int
    threshold_ms: int
    sample: dict[str, Any] | None

    def add(self, other: SlowStat) -> None:
        """Junta dos grupos de la misma ruta: la última ocurrencia (y su muestra) es la más reciente."""
        newer = other.last_at >= self.last_at
        self.count += other.count
        self.total_ms += other.total_ms
        self.max_ms = max(self.max_ms, other.max_ms)
        self.first_at = min(self.first_at, other.first_at)
        if newer:
            self.last_ms, self.last_at, self.last_trace_id = other.last_ms, other.last_at, other.last_trace_id
            self.last_status, self.threshold_ms, self.sample = other.last_status, other.threshold_ms, other.sample


class SlowRequestLog:
    """Peticiones lentas agrupadas por ruta (acotado) que se guardan en lotes. Seguro entre hilos."""

    def __init__(self, max_routes: int) -> None:
        self.max_routes = max_routes
        self._lock = threading.Lock()
        self._routes: dict[str, SlowStat] = {}
        #: Rutas que se sumaron a OTHER por falta de lugar (desde el arranque).
        self.folded = 0

    def record(
        self,
        route: str,
        ms: float,
        *,
        trace_id: str | None,
        status: int,
        threshold_ms: int,
        sample: dict[str, Any] | None,
        at: float | None = None,
    ) -> None:
        """Suma una petición lenta a su ruta. Solo memoria: nunca toca la BD ni lanza."""
        moment = time.time() if at is None else at
        entry = SlowStat(1, ms, ms, ms, moment, moment, trace_id, status, threshold_ms, sample)
        with self._lock:
            self._merge(route[:NAME_LIMIT], entry)

    def _merge(self, route: str, entry: SlowStat) -> None:
        current = self._routes.get(route)
        if current is None and len(self._routes) >= self.max_routes:
            self.folded += 1
            route, current = OTHER, self._routes.get(OTHER)
        if current is None:
            self._routes[route] = entry
        else:
            current.add(entry)

    def drain(self) -> dict[str, SlowStat]:
        with self._lock:
            routes, self._routes = self._routes, {}
        return routes

    def restore(self, routes: dict[str, SlowStat]) -> None:
        """Lo que no se pudo guardar vuelve (con el mismo tope)."""
        with self._lock:
            for route, entry in routes.items():
                self._merge(route, entry)

    def pending(self) -> int:
        with self._lock:
            return len(self._routes)

    def clear(self) -> None:
        with self._lock:
            self._routes = {}
            self.folded = 0


perf_meter = PerfMeter(settings.PERF_METER_MAX_KEYS, enabled=settings.PERF_METER_ENABLED)
slow_requests = SlowRequestLog(settings.SLOW_REQUEST_MAX_ROUTES)
