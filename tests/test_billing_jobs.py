"""Tareas de cobranza del mantenimiento: plantilla diaria a partir del historial de altas y bajas
(repetible y reponiendo días que faltan), emisión de cargos por corte, pronósticos, la foto del
almacenamiento y que cada tarea falle sola."""

import logging
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import func, select

from app.core.config import settings
from app.core.database import SessionLocal
from app.models import BillingPlan, Charge, DailyTask, HeadcountDay, StorageSnapshot, UsageDaily
from app.repositories import usage_repository
from app.repositories.billing_repository import BillingRepository
from app.repositories.usage_repository import UsageRepository
from app.services import billing_jobs, maintenance_service
from tests.billing_support import at, billing_url, company_with_plan, hire, leave, run_jobs, set_today
from tests.conftest import create_employee, login

NOV, DEC = date(2026, 11, 1), date(2026, 12, 1)


def _headcount(company_id: int) -> dict[date, int]:
    with SessionLocal() as db:
        rows = db.execute(
            select(HeadcountDay.day, HeadcountDay.active_employees).where(HeadcountDay.company_id == company_id)
        )
        return dict(rows.all())


def _charges(company_id: int) -> list[Charge]:
    with SessionLocal() as db:
        return list(db.scalars(select(Charge).where(Charge.company_id == company_id).order_by(Charge.cut_on)))


def test_daily_headcount_counts_who_was_active_any_moment_of_the_day(client, admin_headers, monkeypatch):
    set_today(monkeypatch, NOV)
    company_id = company_with_plan(client, admin_headers, starts_on=NOV.isoformat())
    stays = hire(company_id, 3, date(2026, 10, 1))
    leaver = hire(company_id, 1, date(2026, 10, 1), first_id=20_000)[0]
    leave(company_id, leaver, date(2026, 11, 10))  # sale el 10: cuenta ese día, ya no el 11
    hire(company_id, 1, date(2026, 11, 21), first_id=30_000)  # entra el 21: desde ese día
    leave(company_id, stays[0], date(2026, 11, 25))
    hire(company_id, 1, date(2026, 11, 25), first_id=stays[0], hour=15)  # reactivado el mismo día: un día-empleado
    assert run_jobs(DEC)["plantilla diaria (días cerrados)"] == 30
    days = _headcount(company_id)
    assert days[date(2026, 11, 1)] == 4 and days[date(2026, 11, 10)] == 4 and days[date(2026, 11, 11)] == 3
    assert days[date(2026, 11, 21)] == 4 and days[date(2026, 11, 25)] == 4 and days[date(2026, 11, 30)] == 4
    # Prorrateo: 4×10 + 3×10 + 4×10 = 110 días-empleado × $10 = $1 100 + IVA.
    charge = _charges(company_id)[0]
    assert (charge.units, str(charge.subtotal), str(charge.total)) == (110, "1100.00", "1276.00")
    # Repetible: cerrar otra vez un día da lo mismo y nada falta por cerrar.
    with SessionLocal() as db:
        repo = BillingRepository(db)
        repo.close_headcount(date(2026, 11, 11), *billing_jobs.business_day_bounds(date(2026, 11, 11)))
        db.commit()
    assert _headcount(company_id) == days
    again = run_jobs(DEC)
    assert again["plantilla diaria (días cerrados)"] == 0 and again["cargos emitidos"] == 0


def test_charges_wait_for_the_closed_days_and_missing_days_are_backfilled(client, admin_headers, monkeypatch):
    monkeypatch.setattr(settings, "BILLING_DAYS_PER_ROUND", 20)
    set_today(monkeypatch, NOV)
    company_id = company_with_plan(client, admin_headers, starts_on=NOV.isoformat())
    hire(company_id, 2, date(2026, 10, 1))
    first = run_jobs(DEC)  # 20 de 30 días: el cargo espera
    assert (first["plantilla diaria (días cerrados)"], first["cargos emitidos"]) == (20, 0)
    second = run_jobs(DEC)
    assert (second["plantilla diaria (días cerrados)"], second["cargos emitidos"]) == (10, 1)
    assert _charges(company_id)[0].units == 60


def test_without_plans_only_yesterday_is_closed(client):
    assert run_jobs(DEC)["plantilla diaria (días cerrados)"] == 1
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(DailyTask).where(DailyTask.task == "HEADCOUNT")) == 1


def test_flat_trial_and_recurring_discounts(client, admin_headers, monkeypatch):
    set_today(monkeypatch, date(2026, 10, 20))
    every_second = {"type": "AMOUNT", "value": "100", "recurrence": "EVERY", "periods": 2}
    company_id = company_with_plan(
        client,
        admin_headers,
        pricing_mode="FLAT",
        unit_price="3100",
        starts_on="2026-10-20",
        trial_days=12,  # demo hasta el 31 de octubre: octubre no se emite
        discount=every_second,
    )
    run_jobs(NOV)
    assert _charges(company_id) == []
    with SessionLocal() as db:
        assert db.get(BillingPlan, company_id).next_cut_on == date(2026, 11, 30)
    run_jobs(DEC)
    run_jobs(date(2027, 1, 1))
    november, december = _charges(company_id)
    assert (november.sequence, november.units, str(november.discount)) == (1, 30, "0.00")
    assert str(november.subtotal) == "3100.00" and str(november.tax) == "496.00"
    assert (december.sequence, december.units, str(december.discount)) == (2, 31, "100.00")


def test_a_period_without_employees_is_a_zero_charge_already_paid(client, admin_headers, monkeypatch):
    set_today(monkeypatch, NOV)
    company_id = company_with_plan(client, admin_headers, starts_on=NOV.isoformat())
    run_jobs(DEC)
    (charge,) = _charges(company_id)
    assert (str(charge.total), charge.status) == ("0.00", "PAID")


def test_forecasts_refresh_once_a_day(client, admin_headers, monkeypatch):
    set_today(monkeypatch, NOV)
    company_id = company_with_plan(client, admin_headers, starts_on=NOV.isoformat())
    owner = login(client, "admin@panificadora.com", "Empresa1234")
    assert create_employee(client, owner, number="F-1", email="f1@x.com").status_code == 201
    with SessionLocal() as db:
        db.get(BillingPlan, company_id).forecast_at = datetime(2026, 10, 1, tzinfo=UTC)
        db.commit()
    assert run_jobs(date(2026, 11, 5))["pronósticos de cobro"] == 1
    with SessionLocal() as db:
        plan = db.get(BillingPlan, company_id)
        assert str(plan.forecast_total) == "348.00"  # 1 empleado todo noviembre: $300 + IVA
    assert run_jobs(date(2026, 11, 5))["pronósticos de cobro"] == 0


def test_storage_snapshot_is_taken_once_a_day(client, admin_headers, company_headers, monkeypatch):
    create_employee(client, company_headers)
    set_today(monkeypatch, DEC)
    company_id = company_with_plan(client, admin_headers)
    files = {"receipt": ("r.pdf", b"%PDF-1.7 recibo", "application/pdf")}
    form = {"amount": "10", "paid_on": DEC.isoformat(), "method": "CASH", "currency": "MXN"}
    assert (
        client.post(billing_url(company_id, "/payments"), data=form, files=files, headers=admin_headers).status_code
        == 201
    )
    saved = run_jobs(DEC)["fotos de almacenamiento"]
    assert saved == 2  # el personal de la empresa de las pruebas y la cobranza de la nueva
    with SessionLocal() as db:
        rows = {(s.company_id, s.category): (s.rows, s.bytes) for s in db.scalars(select(StorageSnapshot))}
    billing_rows, billing_bytes = rows[(company_id, "BILLING")]
    # El comprobante con su tamaño real; en PostgreSQL además la parte de la tabla (en SQLite no hay tamaño).
    assert billing_rows == 1 and billing_bytes >= len(b"%PDF-1.7 recibo")
    assert any(category == "PEOPLE" and count[0] == 1 for (_, category), count in rows.items())
    assert run_jobs(DEC)["fotos de almacenamiento"] == 0  # ya se tomó hoy


def test_table_sizes_come_from_postgresql():
    class FakeDb:
        def __init__(self, dialect: str) -> None:
            self.dialect = dialect
            self.statements: list[str] = []

        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name=self.dialect))

        def execute(self, stmt):
            self.statements.append(str(stmt))
            return SimpleNamespace(one=lambda: (2048, None))

    postgres = FakeDb("postgresql")
    sizes = UsageRepository(postgres).relation_bytes(["workforce.employees", "auth.auth_sessions"])
    assert sizes == {"auth.auth_sessions": 2048, "workforce.employees": 0}
    assert "pg_total_relation_size" in postgres.statements[0]
    assert UsageRepository(FakeDb("sqlite")).relation_bytes(["workforce.employees"]) == {}
    assert UsageRepository(postgres).relation_bytes([]) == {}
    assert usage_repository.STORAGE_SOURCES


def test_capture_estimates_bytes_with_the_average_row_size(client, company_headers, monkeypatch):
    create_employee(client, company_headers)
    monkeypatch.setattr(UsageRepository, "relation_bytes", lambda self, tables: {"workforce.employees": 4096})
    from app.services.usage_service import capture_storage

    with SessionLocal() as db:
        capture_storage(db, DEC)
        db.commit()
        people = db.scalars(select(StorageSnapshot).where(StorageSnapshot.category == "PEOPLE")).all()
    assert [(p.rows, p.bytes) for p in people] == [(1, 4096)]


def test_each_task_fails_alone(client, admin_headers, monkeypatch, caplog):
    set_today(monkeypatch, NOV)
    company_id = company_with_plan(client, admin_headers, starts_on=NOV.isoformat())
    hire(company_id, 1, date(2026, 10, 1))

    def broken(*_args):
        raise RuntimeError("falla inesperada")

    monkeypatch.setattr(billing_jobs, "TASKS", (*billing_jobs.TASKS[:-1], ("rota", broken)))
    with caplog.at_level(logging.ERROR):
        done = run_jobs(DEC)
    assert done["rota"] == 0 and done["cargos emitidos"] == 1
    assert any("Falló la tarea de rota" in r.getMessage() for r in caplog.records)


def test_one_company_failing_to_issue_does_not_stop_the_others(client, admin_headers, monkeypatch, caplog):
    set_today(monkeypatch, NOV)
    bad = company_with_plan(client, admin_headers, starts_on=NOV.isoformat())
    good = company_with_plan(client, admin_headers, rfc="GOO120315AB1", email="g@g.com", starts_on=NOV.isoformat())
    real = billing_jobs.Ledger.issue_due_charge

    def flaky(self, company_id, today, closed_from):
        if company_id == bad:
            raise RuntimeError("cargo imposible")
        return real(self, company_id, today, closed_from)

    monkeypatch.setattr(billing_jobs.Ledger, "issue_due_charge", flaky)
    with caplog.at_level(logging.ERROR):
        assert run_jobs(DEC)["cargos emitidos"] == 1
    assert _charges(good) and not _charges(bad)
    assert any(f"empresa {bad}" in r.getMessage() for r in caplog.records)


def test_usage_and_snapshots_are_purged_by_retention():
    old = (datetime.now(UTC) - timedelta(days=settings.USAGE_RETENTION_DAYS + 2)).date()
    with SessionLocal() as db:
        db.add(UsageDaily(company_id=1, day=old, requests=3))
        db.add(UsageDaily(company_id=1, day=date.today(), requests=3))
        db.add(DailyTask(task="HEADCOUNT", day=old - timedelta(days=400), done_at=datetime.now(UTC)))
        db.commit()
        removed = maintenance_service.purge_expired(db, now=at(date.today()))
        assert removed["consumo diario"] == 1 and removed["tareas diarias"] == 1
        assert db.scalar(select(func.count()).select_from(UsageDaily)) == 1


def test_issuing_rechecks_the_plan_under_the_lock(client, admin_headers, monkeypatch):
    """Entre la lista de cortes vencidos y el candado, el ADMIN pudo cambiar el plan: se vuelve a revisar."""
    set_today(monkeypatch, NOV)
    company_id = company_with_plan(client, admin_headers, starts_on=NOV.isoformat())
    with SessionLocal() as db:
        ledger = billing_jobs.Ledger(db)
        assert ledger.issue_due_charge(company_id, NOV, NOV) is None  # su corte aún no pasa
        no_plan = db.scalar(select(func.min(BillingPlan.company_id))) - 1
        assert ledger.issue_due_charge(no_plan, DEC, NOV) is None  # la empresa de las pruebas no tiene plan


def test_employee_history_records_hires_leaves_and_removals(client, company_headers):
    from app.models import EmployeeStatusEvent

    def events() -> list[tuple[int, bool]]:
        with SessionLocal() as db:
            return [
                (e.employee_id, e.active)
                for e in db.scalars(select(EmployeeStatusEvent).order_by(EmployeeStatusEvent.id))
            ]

    first = create_employee(client, company_headers).json()["data"]["id"]
    second = create_employee(client, company_headers, number="EMP-002", email="ana@empresa.com").json()["data"]["id"]
    status = f"/api/employees/{first}/status"
    client.patch(status, json={"active": False}, headers=company_headers)
    client.patch(status, json={"active": False}, headers=company_headers)  # sin cambio: sin evento
    client.patch(status, json={"active": True}, headers=company_headers)
    client.patch(f"/api/employees/{second}/status", json={"active": False}, headers=company_headers)
    assert client.delete(f"/api/employees/{second}", headers=company_headers).status_code == 200  # ya inactivo
    assert client.delete(status.removesuffix("/status"), headers=company_headers).status_code == 200
    assert events() == [(first, True), (second, True), (first, False), (first, True), (second, False), (first, False)]
