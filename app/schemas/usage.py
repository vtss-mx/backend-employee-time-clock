"""Consumo de la plataforma (solo el ADMIN): peticiones, datos, tiempo de proceso, errores y
almacenamiento de cada empresa y de cada usuario, en un rango de días del negocio."""

from datetime import date

from pydantic import BaseModel

from app.models.enums import BillingStatus, PricePeriod, PricingMode
from app.schemas.billing import Money
from app.schemas.common import Page


class UsageCounters(BaseModel):
    requests: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    #: Tiempo total de proceso (ms) y el promedio por petición.
    duration_ms: int = 0
    avg_ms: float = 0.0
    #: Respuestas 5xx (fallas del servidor) y 4xx (rechazos normales).
    server_errors: int = 0
    client_errors: int = 0


class UsageDay(UsageCounters):
    day: date


class StorageItem(BaseModel):
    category: str
    rows: int
    bytes: int


class StorageSummary(BaseModel):
    #: Día de la foto (None: aún no se toma la primera).
    day: date | None = None
    rows: int = 0
    bytes: int = 0
    items: list[StorageItem] = []


class UsageOverview(BaseModel):
    start: date
    end: date
    totals: UsageCounters
    days: list[UsageDay]
    storage: StorageSummary
    companies_with_traffic: int


class CompanyUsageRow(UsageCounters):
    company_id: int
    name: str
    active: bool
    status: BillingStatus
    storage_bytes: int
    storage_rows: int
    active_employees: int
    #: Validadores activos: cuentan como empleados en el cobro por empleado activo.
    active_validators: int = 0
    #: % de las peticiones de la plataforma en el rango.
    share: float


class CompanyUsageList(Page[CompanyUsageRow]):
    """Empresas con su consumo (la que más consume primero)."""


class RouteUsage(UsageCounters):
    route: str
    max_ms: int


class RouteUsageList(Page[RouteUsage]):
    """Rutas de la API que usa una empresa (la más usada primero)."""


class UserUsage(UsageCounters):
    user_id: int
    email: str | None = None
    role: str | None = None
    #: Nombre del empleado si la cuenta es de un empleado de esa empresa.
    name: str | None = None
    #: % de las peticiones de la empresa.
    share: float


class UserUsageList(Page[UserUsage]):
    """Cuentas de una empresa con su consumo (la que más consume primero)."""


class UsageBilling(BaseModel):
    #: Moneda del plan: la del precio, el pronóstico y el costo estimado frente al consumo.
    currency: str
    pricing_mode: PricingMode
    unit_price: Money
    price_period: PricePeriod
    forecast_total: Money | None = None


class CompanyUsage(BaseModel):
    company_id: int
    name: str
    status: BillingStatus
    start: date
    end: date
    totals: UsageCounters
    days: list[UsageDay]
    top_routes: list[RouteUsage]
    storage: StorageSummary
    active_employees: int
    #: Validadores activos: cuentan como empleados en el cobro por empleado activo.
    active_validators: int = 0
    billing: UsageBilling | None = None
