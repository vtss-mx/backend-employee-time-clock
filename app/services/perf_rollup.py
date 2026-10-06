"""Resúmenes del rendimiento (mantenimiento): de los minutos a las horas y de las horas a los días del negocio.

Los periodos largos de la pantalla "Rendimiento" (24 horas a 90 días) leen pocas filas gracias a esto. En cada
vuelta del mantenimiento se VUELVEN a sumar las horas de las últimas `PERF_ROLLUP_LOOKBACK_HOURS` (la hora en curso
incluida, todavía parcial) y los días del negocio que tocan: cada resumen reemplaza su fila, así repetirlo da lo
mismo y un lote que se guardó tarde (BD caída un rato) entra en la siguiente vuelta. Las horas y los días del
negocio coinciden con horas UTC (la hora del Centro no tiene horario de verano desde 2022): un día es la suma de
sus 24 horas.
"""

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.core.clock import business_date, business_day_bounds
from app.core.config import settings
from app.repositories.performance_repository import PerformanceRepository


def run(db: Session, now: datetime) -> int:
    """Vuelve a sumar las horas y los días recientes; devuelve cuántas filas de resumen escribió (todo en una
    transacción corta: unas cuantas sentencias `INSERT ... SELECT`)."""
    repo = PerformanceRepository(db)
    current = now.replace(minute=0, second=0, microsecond=0)
    hour = current - timedelta(hours=settings.PERF_ROLLUP_LOOKBACK_HOURS)
    done = 0
    while hour <= current:
        done += repo.rollup_hour(hour)
        hour += timedelta(hours=1)
    day = business_date(current - timedelta(hours=settings.PERF_ROLLUP_LOOKBACK_HOURS))
    while day <= business_date(now):
        start, end = business_day_bounds(day)
        done += repo.rollup_day(day, start, end)
        day += timedelta(days=1)
    db.commit()
    return done
