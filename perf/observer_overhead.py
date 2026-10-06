"""Costo de los observadores de rendimiento por petición (README, "Observabilidad de rendimiento").

Mide, en el mismo proceso y sin red ni base real, lo que la observabilidad agrega a CADA petición:

1. `perf_meter.record` de la petición (candado, minuto, cubeta del histograma y sumas);
2. `_observe` completo del middleware (lo anterior más la revisión del umbral de la regla 18) y `route_key`;
3. los eventos del motor que suman el tiempo de BD de la petición, por sentencia (con y sin temporizador);
4. una petición lenta (arma su muestra y la suma a su alerta: solo las que pasan el umbral);
5. `observed(...)` alrededor de una función.

Uso (imagen de desarrollo, desde `backend-employee-time-clock/`):
`docker run --rm -e PYTHONPATH=/app -v "$PWD:/app" time-clock-backend-dev python perf/observer_overhead.py`
"""

import logging
import os
import statistics
import time
from collections.abc import Callable

os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine, text

from app.core.observability import RequestTimer, observed, request_timer_var
from app.core.perf_meter import PerfMeter, SlowRequestLog
from app.middleware import request_id

ROUNDS = 7


def per_call(work: Callable[[], object], calls: int) -> float:
    """Microsegundos por llamada (mediana de varias rondas)."""
    samples = []
    for _ in range(ROUNDS):
        started = time.perf_counter()
        for _ in range(calls):
            work()
        samples.append((time.perf_counter() - started) * 1e6 / calls)
    return statistics.median(samples)


class _Route:
    path_format = "/employees/{employee_id}"


def main() -> None:
    # La línea WARNING de cada petición lenta se arma igual (su costo cuenta), pero no se imprime 50 000 veces.
    access = logging.getLogger("app.access")
    access.propagate = False
    access.addHandler(logging.NullHandler())
    meter, slow = PerfMeter(1_000_000), SlowRequestLog(100_000)
    request_id.perf_meter, request_id.slow_requests = meter, slow  # acumuladores propios (no los del proceso)
    scope = {"type": "http", "method": "GET", "path": "/api/employees/12", "route": _Route(), "query_string": b""}
    info = request_id.RequestInfo(method="GET", path="/api/employees/12")
    exchange = request_id._Exchange(scope)
    exchange.status, exchange.sent_bytes = 200, 512
    timer = RequestTimer(db_ms=1.2, db_queries=2)
    results: dict[str, float] = {}

    results["perf_meter.record (HTTP)"] = per_call(
        lambda: meter.record("HTTP", "GET /api/employees/{employee_id}", 12.5, db_ms=1.2, db_queries=2), 200_000
    )
    results["route_key (plantilla de la ruta)"] = per_call(lambda: request_id.route_key(scope), 200_000)
    route = request_id.route_key(scope)
    results["_observe completo (petición normal)"] = per_call(
        lambda: request_id._observe(scope, info, exchange, 12.5, route, 200, "trace", timer), 200_000
    )
    threshold = request_id.settings.SLOW_REQUEST_THRESHOLD_MS
    results["_observe de una petición LENTA (muestra + alerta)"] = per_call(
        lambda: request_id._observe(scope, info, exchange, threshold + 1.0, route, 200, "trace", timer), 50_000
    )

    engine = create_engine("sqlite://")
    with engine.connect() as conn:
        bare = per_call(lambda: conn.execute(text("SELECT 1")), 20_000)
        token = request_timer_var.set(RequestTimer())
        try:
            timed = per_call(lambda: conn.execute(text("SELECT 1")), 20_000)
        finally:
            request_timer_var.reset(token)
    results["sentencia SQL sin petición (eventos, sin temporizador)"] = bare
    results["sentencia SQL dentro de una petición (con temporizador)"] = timed
    results["  → costo del tiempo de BD por sentencia"] = max(timed - bare, 0.0)

    @observed("bench.noop")
    def noop() -> None:
        return None

    results["observed(...) alrededor de una función"] = per_call(noop, 200_000) - per_call(lambda: None, 200_000)

    width = max(len(name) for name in results)
    for name, micros in results.items():
        print(f"{name:<{width}}  {micros:7.2f} µs")
    request_overhead = results["_observe completo (petición normal)"] + results["route_key (plantilla de la ruta)"]
    per_statement = results["  → costo del tiempo de BD por sentencia"]
    print(f"\nPor petición normal (route_key + _observe): {request_overhead:.2f} µs y 0 consultas;")
    print(f"más {per_statement:.2f} µs por cada sentencia SQL de la petición (su tiempo de BD).")


if __name__ == "__main__":
    main()
