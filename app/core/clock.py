"""Fechas en UTC con zona horaria, iguales en PostgreSQL y SQLite."""

from datetime import UTC, date, datetime, time, timedelta
from typing import overload
from zoneinfo import ZoneInfo

from app.core.config import settings


@overload
def as_utc(value: datetime) -> datetime: ...
@overload
def as_utc(value: None) -> None: ...
def as_utc(value: datetime | None) -> datetime | None:
    """SQLite (pruebas) devuelve fechas sin zona horaria: se interpretan como UTC."""
    return value if value is None or value.tzinfo else value.replace(tzinfo=UTC)


def has_passed(moment: datetime, now: datetime | None = None) -> bool:
    """¿Ya llegó ese momento? (vencimientos de QR, llaves, retos, cuentas recordadas...). Acepta
    fechas sin zona (SQLite) y un `now` fijo para evaluar varias con el mismo instante."""
    return as_utc(moment) <= (now or datetime.now(UTC))


def epoch_ms() -> int:
    """Ahora en milisegundos desde 1970 (reloj de pared, el mismo en todas las réplicas con NTP): los tiempos del
    destello dictado viajan sellados en un token y otra réplica puede continuarlos."""
    return int(datetime.now(UTC).timestamp() * 1000)


def business_now() -> datetime:
    """Ahora en la zona horaria del negocio (APP_TIMEZONE), no la del servidor."""
    return datetime.now(ZoneInfo(settings.APP_TIMEZONE))


def business_today() -> date:
    return business_now().date()


def business_date(moment: datetime) -> date:
    """El día del negocio de un instante (el mantenimiento recibe su `now`)."""
    return moment.astimezone(ZoneInfo(settings.APP_TIMEZONE)).date()


def business_day_start() -> datetime:
    """Inicio del día de hoy en la zona horaria del negocio (para conteos "de hoy")."""
    return business_now().replace(hour=0, minute=0, second=0, microsecond=0)


def business_day_bounds(day: date) -> tuple[datetime, datetime]:
    """[inicio, fin) de un día del negocio, en UTC: para contar lo que pasó ese día (en la hora del
    Centro) en columnas guardadas en UTC."""
    start = datetime.combine(day, time.min, tzinfo=ZoneInfo(settings.APP_TIMEZONE))
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=ZoneInfo(settings.APP_TIMEZONE))
    return start.astimezone(UTC), end.astimezone(UTC)
