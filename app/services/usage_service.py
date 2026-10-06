"""Consumo de la plataforma (solo el ADMIN): cuánto usa cada empresa y cada usuario (peticiones, datos
enviados y recibidos, tiempo de proceso, errores) y cuánto almacena, para monitorearlo en todo momento
y fijar precios. Lo mide `usage_meter` (sin trabajo de BD por petición) y la foto diaria del
almacenamiento la toma el mantenimiento (`capture_storage`).
"""

from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.core.clock import business_today
from app.core.exceptions import NotFoundError, UnprocessableError
from app.models import BillingStatus, PricePeriod, PricingMode, StorageCategory
from app.repositories.billing_repository import BillingRepository
from app.repositories.company_repository import CompanyRepository
from app.repositories.usage_repository import STORAGE_SOURCES, UsageRepository
from app.schemas.common import PageParams
from app.schemas.usage import (
    CompanyUsage,
    CompanyUsageList,
    CompanyUsageRow,
    RouteUsage,
    RouteUsageList,
    StorageItem,
    StorageSummary,
    UsageBilling,
    UsageCounters,
    UsageDay,
    UsageOverview,
    UserUsage,
    UserUsageList,
)

#: Rango máximo de una consulta de consumo (un año y un día: comparar el mismo mes del año anterior).
MAX_RANGE_DAYS = 366
#: Rutas que muestra el detalle de una empresa (la lista completa va paginada).
TOP_ROUTES = 5
SORTS = ("requests", "bytes", "duration", "errors", "storage")


def usage_range(start: date | None, end: date | None) -> tuple[date, date]:
    """El rango pedido (por omisión, del primero del mes a hoy), validado y acotado."""
    today = business_today()
    last = end or today
    first = start or last.replace(day=1)
    if first > last:
        raise UnprocessableError(code="INVALID_RANGE", field="start")
    if (last - first).days >= MAX_RANGE_DAYS:
        raise UnprocessableError(code="RANGE_TOO_LONG", field="start")
    return first, last


def counters(row: Any) -> dict[str, Any]:
    """Contadores de una fila de sumas, con el promedio por petición."""
    requests = int(row.requests or 0)
    duration = int(row.duration_ms or 0)
    return {
        "requests": requests,
        "bytes_in": int(row.bytes_in or 0),
        "bytes_out": int(row.bytes_out or 0),
        "duration_ms": duration,
        "avg_ms": round(duration / requests, 1) if requests else 0.0,
        "server_errors": int(row.server_errors or 0),
        "client_errors": int(row.client_errors or 0),
    }


def series(rows: Sequence[Any], start: date, end: date) -> list[UsageDay]:
    """Un registro por día del rango (con ceros en los días sin tráfico), en orden."""
    by_day = {row.day: row for row in rows}
    empty = UsageCounters()
    return [
        UsageDay(day=day, **(counters(by_day[day]) if day in by_day else empty.model_dump()))
        for day in (start + timedelta(days=offset) for offset in range((end - start).days + 1))
    ]


def share(part: int, whole: int) -> float:
    return round(part * 100 / whole, 1) if whole else 0.0


def storage_summary(day: date | None, rows: Sequence[Any]) -> StorageSummary:
    items = [StorageItem(category=r.category, rows=int(r.rows or 0), bytes=int(r.bytes or 0)) for r in rows]
    return StorageSummary(day=day, rows=sum(i.rows for i in items), bytes=sum(i.bytes for i in items), items=items)


class UsageService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.usage = UsageRepository(db)

    def overview(self, start: date | None, end: date | None) -> UsageOverview:
        """Toda la plataforma en el rango: totales, serie diaria y el almacenamiento más reciente."""
        first, last = usage_range(start, end)
        day, storage = self.usage.platform_storage()
        return UsageOverview(
            start=first,
            end=last,
            totals=UsageCounters(**counters(self.usage.platform_totals(first, last))),
            days=series(self.usage.platform_days(first, last), first, last),
            storage=storage_summary(day, storage),
            companies_with_traffic=self.usage.companies_with_traffic(first, last),
        )

    def companies(
        self, start: date | None, end: date | None, *, search: str | None, sort: str, page: PageParams
    ) -> CompanyUsageList:
        """Empresas con su consumo del rango (la que más consume primero) y su parte del total."""
        first, last = usage_range(start, end)
        rows, total = self.usage.company_usage_page(
            first, last, search=search, sort=sort, offset=page.offset, limit=page.size
        )
        platform = int(self.usage.platform_totals(first, last).requests or 0)
        active = BillingRepository(self.db).active_headcount(r.id for r in rows)
        items = [
            CompanyUsageRow(
                company_id=r.id,
                name=r.name,
                active=r.active,
                status=BillingStatus.SUSPENDED if r.suspended_at else BillingStatus.ACTIVE,
                storage_bytes=int(r.storage_bytes),
                storage_rows=int(r.storage_rows),
                active_employees=active[r.id].employees,
                active_validators=active[r.id].validators,
                share=share(int(r.requests), platform),
                **counters(r),
            )
            for r in rows
        ]
        return CompanyUsageList.of(items, total, page)

    def _company(self, company_id: int) -> Any:
        company = CompanyRepository(self.db).get(company_id)
        if company is None:
            raise NotFoundError(code="COMPANY_NOT_FOUND")
        return company

    def company(self, company_id: int, start: date | None, end: date | None) -> CompanyUsage:
        """Una empresa: totales, serie diaria, sus rutas más usadas, su almacenamiento, sus empleados y
        validadores activos (lo que se cobra por empleado activo) y lo que paga (para ver el consumo junto a su
        costo)."""
        company = self._company(company_id)
        first, last = usage_range(start, end)
        routes, _ = self.usage.routes_page(company_id, first, last, offset=0, limit=TOP_ROUTES)
        day, storage = self.usage.company_storage(company_id)
        billing = BillingRepository(self.db)
        plan = billing.plan(company_id)
        active = billing.active_headcount([company_id])[company_id]
        return CompanyUsage(
            company_id=company.id,
            name=company.name,
            status=BillingStatus(company.billing_status),
            start=first,
            end=last,
            totals=UsageCounters(**counters(self.usage.company_totals(company_id, first, last))),
            days=series(self.usage.company_days(company_id, first, last), first, last),
            top_routes=[RouteUsage(route=r.route, max_ms=int(r.max_ms or 0), **counters(r)) for r in routes],
            storage=storage_summary(day, storage),
            active_employees=active.employees,
            active_validators=active.validators,
            billing=UsageBilling(
                currency=plan.currency,
                pricing_mode=PricingMode(plan.pricing_mode),
                unit_price=plan.unit_price,
                price_period=PricePeriod(plan.price_period),
                forecast_total=plan.forecast_total,
            )
            if plan
            else None,
        )

    def routes(self, company_id: int, start: date | None, end: date | None, page: PageParams) -> RouteUsageList:
        self._company(company_id)
        first, last = usage_range(start, end)
        rows, total = self.usage.routes_page(company_id, first, last, offset=page.offset, limit=page.size)
        items = [RouteUsage(route=r.route, max_ms=int(r.max_ms or 0), **counters(r)) for r in rows]
        return RouteUsageList.of(items, total, page)

    def users(self, company_id: int, start: date | None, end: date | None, page: PageParams) -> UserUsageList:
        """Cuentas de la empresa con su consumo: correo, rol y, si es empleado de ESA empresa, su nombre
        (dos consultas para toda la página)."""
        self._company(company_id)
        first, last = usage_range(start, end)
        rows, total = self.usage.users_page(company_id, first, last, offset=page.offset, limit=page.size)
        whole = int(self.usage.company_totals(company_id, first, last).requests or 0)
        ids = [r.user_id for r in rows if r.user_id]
        accounts = self._accounts(company_id, ids)
        items = []
        for r in rows:
            email, role, name = accounts.get(r.user_id, (None, None, None))
            items.append(
                UserUsage(
                    user_id=r.user_id,
                    email=email,
                    role=role,
                    name=name,
                    share=share(int(r.requests), whole),
                    **counters(r),
                )
            )
        return UserUsageList.of(items, total, page)

    def _accounts(self, company_id: int, ids: list[int]) -> dict[int, tuple[str, str, str | None]]:
        if not ids:
            return {}
        return self.usage.accounts(company_id, ids)


# ---------------------------------------------------------------- foto diaria del almacenamiento


def capture_storage(db: Session, day: date) -> int:
    """La foto del almacenamiento de cada empresa por grupo: filas de cada tabla y bytes estimados
    (filas × tamaño promedio por fila de la tabla con sus índices; los comprobantes de pago y los documentos de la
    empresa suman su tamaño real). Devuelve cuántas filas de la foto se guardaron."""
    usage = UsageRepository(db)
    counts = usage.table_counts()
    sizes = usage.relation_bytes(counts)
    totals: dict[tuple[int, str], list[int]] = {}
    for source in STORAGE_SOURCES:
        per_company = counts[source.table]
        table_rows = sum(per_company.values())
        per_row = sizes.get(source.table, 0) / table_rows if table_rows else 0
        for company_id, rows in per_company.items():
            entry = totals.setdefault((company_id, source.category.value), [0, 0])
            entry[0] += rows
            entry[1] += round(rows * per_row)
    for files in (usage.receipt_bytes(), usage.document_bytes()):
        for company_id, size in files.items():
            totals.setdefault((company_id, StorageCategory.BILLING.value), [0, 0])[1] += size
    snapshot = [
        {"company_id": company_id, "category": category, "rows": values[0], "bytes": values[1]}
        for (company_id, category), values in sorted(totals.items())
    ]
    return usage.save_storage(day, snapshot)
