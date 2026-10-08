"""Reglas puras del monitoreo de deriva (antifraude fase 3, I+D §3.5): ventanas, percentiles, el índice de estabilidad
de población (PSI) y la comparación de una señal entre dos ventanas. Sin base de datos: `drift_service` las alimenta.

Por qué el PSI: compara la FORMA completa de la distribución (no solo un promedio) y es la medida estándar para saber si
una población cambió ("una versión nueva de iOS cambió la respuesta de la cámara"). Se calcula con los deciles de la
línea base como cubetas (así cada cubeta esperada vale 10 %) y con un suavizado mínimo para que una cubeta vacía no
dispare un logaritmo infinito. Regla práctica: < 0.1 estable, 0.1-0.2 cambio leve, > 0.2 cambio real (el umbral
`DRIFT_PSI_ALERT`).
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import numpy as np

from app.models.drift import (
    DRIFT_ALERT,
    DRIFT_INSUFFICIENT,
    DRIFT_NO_BASELINE,
    DRIFT_OK,
    DRIFT_VERSION_CHANGE,
)

#: Un lunes: las ventanas se alinean a él (con 7 días son las semanas del calendario).
EPOCH_MONDAY = date(2026, 1, 5)
#: Cubetas del PSI (los deciles de la línea base) y el suavizado de una cubeta vacía.
PSI_BINS = 10
PSI_EPSILON = 1e-4
#: La cola que vigila cada tipo de señal: un mínimo se degrada por abajo (p10); un máximo (moiré), por arriba (p90).
LOWER_TAIL = 10
UPPER_TAIL = 90


def window_start(day: date, days: int) -> date:
    """El primer día de la ventana que contiene `day` (ventanas consecutivas de `days` días desde `EPOCH_MONDAY`)."""
    offset = (day - EPOCH_MONDAY).days
    return EPOCH_MONDAY + timedelta(days=(offset // days) * days)


def last_closed_window(today: date, days: int) -> date:
    """La ventana anterior a la que contiene `today`: la última completa, la que se calcula."""
    return window_start(today, days) - timedelta(days=days)


def quantile(values: list[float], q: float) -> float | None:
    """El percentil `q` (0-1) de los valores, o None sin valores."""
    return round(float(np.quantile(values, q)), 5) if values else None


def psi(baseline: list[float], current: list[float]) -> float | None:
    """Índice de estabilidad de población entre la línea base y la ventana actual (None si alguna está vacía).

    Cubetas: los deciles de la línea base (sin repetidos: una señal casi constante tiene menos cubetas). Cada lado se
    reparte en ellas y se suaviza con `PSI_EPSILON` para que una cubeta sin valores no sea un logaritmo infinito.
    """
    if not baseline or not current:
        return None
    # Con la línea base no vacía siempre hay al menos un borde (una señal constante deja uno solo: dos cubetas).
    edges = np.unique(np.quantile(baseline, [i / PSI_BINS for i in range(1, PSI_BINS)]))
    bins = edges.size + 1

    def share(values: list[float]) -> np.ndarray:
        counts = np.bincount(np.digitize(values, edges), minlength=bins).astype(float)
        return (counts + PSI_EPSILON) / (counts.sum() + PSI_EPSILON * bins)

    expected, actual = share(baseline), share(current)
    return round(float(np.sum((actual - expected) * np.log(actual / expected))), 5)


@dataclass(frozen=True)
class Comparison:
    """Lo que queda en la fila de una señal × plataforma de una ventana."""

    samples: int
    baseline_samples: int
    median: float | None
    baseline_median: float | None
    tail: float | None
    baseline_tail: float | None
    tail_percentile: int
    tail_change: float | None
    psi: float | None
    status: str


def compare(
    current: list[float],
    baseline: list[float],
    *,
    upper: bool,
    min_samples: int,
    psi_alert: float,
    tail_drop_alert: float,
    version_changed: bool,
) -> Comparison:
    """Compara la ventana actual con la línea base. Decide el estado en este orden: cambió la versión del motor o los
    modelos (no comparable: se mide, no se alerta) → pocos intentos actuales → sin línea base suficiente → PSI y cola
    (alerta si el PSI pasa el umbral o la cola se movió hacia lo sospechoso más de la fracción: hacia abajo en un
    mínimo, hacia arriba en un máximo)."""
    percentile = UPPER_TAIL if upper else LOWER_TAIL
    median, baseline_median = quantile(current, 0.5), quantile(baseline, 0.5)
    tail, baseline_tail = quantile(current, percentile / 100), quantile(baseline, percentile / 100)
    comparable = len(current) >= min_samples and len(baseline) >= min_samples
    index = psi(baseline, current) if comparable else None
    change = None
    if comparable and tail is not None and baseline_tail not in (None, 0.0):
        change = round((tail - baseline_tail) / abs(baseline_tail), 5)
    if version_changed:
        status = DRIFT_VERSION_CHANGE
    elif len(current) < min_samples:
        status = DRIFT_INSUFFICIENT
    elif len(baseline) < min_samples:
        status = DRIFT_NO_BASELINE
    else:
        toward_suspicious = (change or 0.0) * (1 if upper else -1)
        alert = (index or 0.0) > psi_alert or toward_suspicious > tail_drop_alert
        status = DRIFT_ALERT if alert else DRIFT_OK
    return Comparison(
        samples=len(current),
        baseline_samples=len(baseline),
        median=median,
        baseline_median=baseline_median,
        tail=tail,
        baseline_tail=baseline_tail,
        tail_percentile=percentile,
        tail_change=change,
        psi=index,
        status=status,
    )


def quick_approval(status: str | None, opened_at: object, decided_at: object, seconds: int) -> bool:
    """Una revisión aprobada en menos de `seconds` desde que se abrió: la empresa la aprobó sin mirar."""
    if status != "CONFIRMED" or not isinstance(opened_at, datetime) or not isinstance(decided_at, datetime):
        return False
    return (decided_at - opened_at).total_seconds() < seconds
