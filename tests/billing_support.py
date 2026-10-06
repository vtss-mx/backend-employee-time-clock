"""Ayudas de las pruebas de cobranza: el "hoy" del negocio fijo, empresas con plan, historial de altas y
bajas escrito en la BD y una vuelta del mantenimiento en un instante dado."""

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.core.database import SessionLocal
from app.models import EmployeeStatusEvent
from app.services import billing_jobs
from tests.conftest import create_company

#: Plan por omisión de las pruebas: $300 por empleado al mes (=$10 por día en un mes de 30 días).
PLAN = {
    "pricing_mode": "PER_USER",
    "unit_price": "300.00",
    "price_period": "MONTH",
    "interval_months": 1,
    "starts_on": None,
    "trial_days": 0,
    "discount": None,
    "tax_rate": "16",
    "grace_days": 5,
}
#: Módulos que leen "hoy" del negocio (se fijan juntos).
_TODAY_USERS = ("app.services.billing_service", "app.services.usage_service")
PDF = b"%PDF-1.4\n% comprobante de prueba\n"


def set_today(monkeypatch, day: date) -> None:
    """Fija el día del negocio que ven la cobranza y el consumo."""
    for module in _TODAY_USERS:
        monkeypatch.setattr(f"{module}.business_today", lambda day=day: day)


def at(day: date, hour: int = 6) -> datetime:
    """Un instante de ese día del negocio (para el mantenimiento)."""
    return datetime.combine(day, time(hour), tzinfo=ZoneInfo(settings.APP_TIMEZONE)).astimezone(UTC)


def company_with_plan(client, admin_headers, *, rfc="PNO120315AB1", email="admin@panificadora.com", **plan) -> int:
    response = create_company(client, admin_headers, rfc=rfc, admin_email=email, billing={**PLAN, **plan})
    assert response.status_code == 201, response.text
    return response.json()["data"]["id"]


def hire(company_id: int, employees: int, since: date, *, first_id: int = 10_000, hour: int = 9) -> list[int]:
    """Historial de altas escrito directo (el cobro solo lee el historial, no los empleados)."""
    ids = list(range(first_id, first_id + employees))
    with SessionLocal() as db:
        db.add_all(
            EmployeeStatusEvent(company_id=company_id, employee_id=i, active=True, occurred_at=at(since, hour))
            for i in ids
        )
        db.commit()
    return ids


def leave(company_id: int, employee_id: int, day: date) -> None:
    with SessionLocal() as db:
        db.add(
            EmployeeStatusEvent(company_id=company_id, employee_id=employee_id, active=False, occurred_at=at(day, 12))
        )
        db.commit()


def run_jobs(day: date) -> dict[str, int]:
    """Una vuelta de las tareas de cobranza ese día (a las 6 de la mañana del negocio)."""
    with SessionLocal() as db:
        return billing_jobs.run(db, at(day))


def billing_url(company_id: int, path: str = "") -> str:
    return f"/api/admin/billing/companies/{company_id}{path}"


def pay(client, admin_headers, company_id: int, amount: str, paid_on: date, *, files=None, **fields):
    """Registra un pago (formulario multipart, con comprobante opcional), en pesos salvo que se diga otra moneda."""
    data = {"amount": amount, "paid_on": paid_on.isoformat(), "method": "TRANSFER", "currency": "MXN", **fields}
    return client.post(billing_url(company_id, "/payments"), data=data, files=files, headers=admin_headers)
