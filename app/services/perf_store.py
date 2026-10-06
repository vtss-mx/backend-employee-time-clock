"""Guardado en lotes de la observabilidad de rendimiento: lo que los observadores sumaron en memoria
(`app/core/perf_meter.py`) pasa a `ops.perf_minutes` y `ops.slow_request_alerts` SIN tocar ninguna petición.

- Un hilo (`PerfFlusher`) guarda cada `PERF_FLUSH_SECONDS` y lo pendiente al apagar.
- Cada tabla en su transacción, con UN upsert que SUMA y las filas en orden de llave (dos réplicas que guardan a la
  vez no se interbloquean).
- **Nada se pierde si la BD falla**: lo que no se guardó vuelve al acumulador (con el mismo tope de memoria) y se
  guarda en la siguiente vuelta; la falla queda registrada para el ADMIN (log de error → `ops.error_reports`).
"""

import json
import logging
import threading
from datetime import UTC, datetime
from typing import Any

from app.core.database import platform_session
from app.core.observability import observed
from app.core.perf_meter import PerfMeter, SlowRequestLog, SlowStat, Stat, perf_meter, slow_requests
from app.models import SlowAlertStatus
from app.repositories.performance_repository import PerformanceRepository

logger = logging.getLogger("app.performance")


def _instant(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


def _minute_rows(stats: dict[tuple[int, str, str], Stat]) -> list[dict[str, Any]]:
    return [
        {"kind": kind, "minute": _instant(minute), "name": name, **stats[(minute, kind, name)].row()}
        for minute, kind, name in sorted(stats)
    ]


def _alert_row(route: str, entry: SlowStat) -> dict[str, Any]:
    method, _, path = route.partition(" ")
    return {
        "route": route,
        "method": method[:10] if path else "",
        "path": path or route,
        "status": SlowAlertStatus.OPEN.value,
        "count": entry.count,
        "total_ms": round(entry.total_ms, 3),
        "last_ms": round(entry.last_ms, 3),
        "max_ms": round(entry.max_ms, 3),
        "threshold_ms": entry.threshold_ms,
        "first_seen_at": _instant(entry.first_at),
        "last_seen_at": _instant(entry.last_at),
        "opened_at": _instant(entry.last_at),
        "last_trace_id": entry.last_trace_id,
        "last_status": entry.last_status,
        "sample": json.dumps(entry.sample, ensure_ascii=False, default=str) if entry.sample else None,
        "reopened": 0,
    }


def flush(meter: PerfMeter = perf_meter, slow: SlowRequestLog = slow_requests) -> int:
    """Guarda lo acumulado; devuelve cuántas filas se guardaron. Lo que no se pudo guardar vuelve a su acumulador."""
    saved = 0
    stats = meter.drain()
    if stats:
        rows = _minute_rows(stats)
        try:
            with platform_session() as db:
                PerformanceRepository(db).add_minutes(rows)
                db.commit()
            saved += len(rows)
        except Exception:
            logger.exception("No se pudo guardar el rendimiento por minuto (se reintenta en la siguiente vuelta)")
            meter.restore(stats)
    routes = slow.drain()
    if routes:
        alerts = [_alert_row(route, routes[route]) for route in sorted(routes)]
        try:
            with platform_session() as db:
                PerformanceRepository(db).add_slow(alerts)
                db.commit()
            saved += len(alerts)
        except Exception:
            logger.exception("No se pudieron guardar las peticiones lentas (se reintenta en la siguiente vuelta)")
            slow.restore(routes)
    return saved


class PerfFlusher:
    """Hilo que guarda la observabilidad cada `interval` segundos (y lo pendiente al apagar). Nunca muere: una vuelta
    que falla se registra y la siguiente sigue."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="perf-meter", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        flush()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                with observed("perf.flush"):
                    flush()
            except Exception:
                logger.exception("Falló el guardado periódico del rendimiento")
