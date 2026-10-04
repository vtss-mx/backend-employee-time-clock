"""Fechas en UTC con zona horaria, iguales en PostgreSQL y SQLite."""

from datetime import UTC, date, datetime
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


def business_now() -> datetime:
    """Ahora en la zona horaria del negocio (APP_TIMEZONE), no la del servidor."""
    return datetime.now(ZoneInfo(settings.APP_TIMEZONE))


def business_today() -> date:
    return business_now().date()


def business_day_start() -> datetime:
    """Inicio del día de hoy en la zona horaria del negocio (para conteos "de hoy")."""
    return business_now().replace(hour=0, minute=0, second=0, microsecond=0)
