"""Histograma de tiempos con cubetas FIJAS: la base de los percentiles de la pantalla "Rendimiento".

Por qué cubetas fijas y no guardar cada tiempo: un percentil (p50, p95, p99) no se puede sumar ni promediar entre
réplicas ni entre minutos, pero un histograma sí. Cada réplica cuenta cuántas peticiones cayeron en cada cubeta y el
guardado en lotes SUMA esos conteos (`ON CONFLICT ... DO UPDATE SET h_10 = h_10 + EXCLUDED.h_10`): el histograma de
toda la plataforma en cualquier ventana (una hora, una semana) es la suma de sus filas, y de ahí sale el percentil
con interpolación lineal dentro de su cubeta. El error queda acotado por el ancho de la cubeta (más fina donde
viven casi todas las peticiones: 1-100 ms) y por el máximo real, que se guarda aparte.

Las cubetas son las mismas en el código, en las columnas `h_*` de `ops.perf_minutes`/`perf_hours`/`perf_days` y en
la migración `0063`: cambiarlas exige una migración nueva (las filas viejas quedarían en cubetas distintas).
"""

from bisect import bisect_left
from collections.abc import Sequence
from itertools import accumulate
from typing import Final

#: Límite superior (ms, inclusive) de cada cubeta; la última cubeta (`h_inf`) recibe lo que pasa de 10 s.
BOUNDS_MS: Final = (1, 2, 5, 10, 20, 35, 50, 75, 100, 150, 250, 400, 600, 1000, 2000, 5000, 10000)
#: Cubetas en total (las de `BOUNDS_MS` más la de "más de 10 s").
BUCKETS: Final = len(BOUNDS_MS) + 1
#: Columna de cada cubeta, en orden (la misma posición que su conteo en una lista de `BUCKETS` enteros).
COLUMNS: Final = (*(f"h_{bound}" for bound in BOUNDS_MS), "h_inf")


def bucket_of(ms: float) -> int:
    """Cubeta de un tiempo: la primera cuyo límite lo cubre (5 ms cae en "≤ 5"; 10 001 ms en la última)."""
    return bisect_left(BOUNDS_MS, ms)


def empty() -> list[int]:
    """Un histograma sin conteos."""
    return [0] * BUCKETS


def percentile(counts: Sequence[int], q: float, max_ms: float) -> float:
    """El percentil `q` (0 < q ≤ 1) de un histograma, interpolado dentro de su cubeta.

    El límite de arriba de la cubeta se recorta al máximo real (`max_ms`): ninguna petición tardó más que eso,
    así que un percentil nunca lo pasa (y la última cubeta, abierta, termina ahí). Sin conteos: 0."""
    cumulative = list(accumulate(max(count, 0) for count in counts))
    total = cumulative[-1] if cumulative else 0
    if total <= 0:
        return 0.0
    rank = min(max(q, 0.0), 1.0) * total
    # La primera cubeta cuyo acumulado alcanza el rango (con conteo > 0: la anterior no lo alcanzaba).
    index = min(bisect_left(cumulative, rank), len(cumulative) - 1)
    count = cumulative[index] - (cumulative[index - 1] if index > 0 else 0)
    seen = cumulative[index] - count
    lower = float(BOUNDS_MS[index - 1]) if index > 0 else 0.0
    upper = float(BOUNDS_MS[index]) if index < len(BOUNDS_MS) else max(max_ms, lower)
    upper = max(lower, min(upper, max_ms)) if max_ms > 0 else upper
    return round(lower + (upper - lower) * (rank - seen) / count, 1) if count else round(lower, 1)
