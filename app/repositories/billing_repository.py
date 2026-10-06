"""Cobranza (esquema billing): planes, plantilla diaria, cargos, pagos y sus aplicaciones.

Cada consulta va por su índice (ver los modelos): lo de una empresa por la llave que empieza con su
`company_id`; lo de toda la plataforma por los índices de fecha o el parcial de cargos abiertos. Los
saldos se suman en la base (un GROUP BY para toda una página de empresas), nunca en Python. Lo de toda la
plataforma se agrupa POR MONEDA: nunca se suman importes de monedas distintas.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Date,
    Integer,
    Numeric,
    String,
    and_,
    case,
    cast,
    delete,
    func,
    literal,
    null,
    select,
    tuple_,
    union,
    union_all,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import InstrumentedAttribute, Session

from app.models import (
    BillingPlan,
    Charge,
    ChargeLine,
    ChargeStatus,
    Company,
    DailyTask,
    Employee,
    EmployeeStatusEvent,
    HeadcountDay,
    Payment,
    PaymentAllocation,
    PaymentStatus,
    User,
    UserRole,
    ValidatorStatusEvent,
)
from app.models.company import company_search_text
from app.repositories.aggregates import insert_many
from app.repositories.search import contains_text, search_term

ZERO = Decimal("0")
_OPEN = Charge.status == ChargeStatus.OPEN
_LIVE = Charge.status != ChargeStatus.VOID
_CONFIRMED = Payment.status == PaymentStatus.CONFIRMED
#: Tarea diaria del mantenimiento que cierra la plantilla de un día (`ops.daily_tasks`).
HEADCOUNT_TASK = "HEADCOUNT"
#: Qué se cuenta en la plantilla: empleados y validadores (cada uno con su historial de estados).
_EMPLOYEES, _VALIDATORS = 0, 1


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def _active_during(
    event: type[EmployeeStatusEvent] | type[ValidatorStatusEvent],
    subject: InstrumentedAttribute[int],
    kind: int,
    start: datetime,
    end: datetime,
) -> tuple[Any, Any]:
    """Quién de un historial de estados estuvo activo en algún momento de `[start, end)`: (empresa, quién, tipo) de
    los activos al empezar (su último evento anterior es un alta: ventana por persona en el orden de su índice) y de
    los dados de alta durante el día (índice por fecha). La unión de ambos quita a quien está en los dos."""
    ranked = (
        select(
            event.company_id,
            subject.label("subject_id"),
            event.active,
            func.row_number()
            .over(partition_by=(event.company_id, subject), order_by=(event.occurred_at.desc(), event.id.desc()))
            .label("rank"),
        )
        .where(event.occurred_at < start)
        .subquery()
    )
    at_start = select(ranked.c.company_id, ranked.c.subject_id, literal(kind).label("kind")).where(
        ranked.c.rank == 1, ranked.c.active
    )
    joined = select(event.company_id, subject, literal(kind)).where(
        event.occurred_at >= start, event.occurred_at < end, event.active
    )
    return at_start, joined


@dataclass(frozen=True)
class Balance:
    """Saldo de una empresa (todo calculado en la base)."""

    charged: Decimal = ZERO
    paid: Decimal = ZERO
    outstanding: Decimal = ZERO
    overdue: Decimal = ZERO
    credit: Decimal = ZERO
    open_charges: int = 0
    overdue_charges: int = 0
    oldest_due_on: date | None = None
    last_payment_on: date | None = None
    #: Moneda de sus movimientos (None sin movimientos): la de la empresa.
    currency: str | None = None
    #: Cargos y pagos registrados, también los anulados: con uno, la moneda de la empresa queda fija.
    movements: int = 0


#: Cifras del resumen de la plataforma (`overview`). Por moneda: lo emitido y lo cobrado en el mes, lo abierto,
#: lo vencido (cuántas empresas), el saldo a favor, los planes (cuántos y su pronóstico) y el último corte.
BILLED, COLLECTED, OUTSTANDING, OVERDUE, CREDIT, PLANS, LAST_CUT = (
    "billed",
    "collected",
    "outstanding",
    "overdue",
    "credit",
    "plans",
    "last_cut",
)
#: Sin moneda: empresas, suspendidas, en demo y el día del último corte.
COMPANIES, SUSPENDED, IN_TRIAL, LAST_CUT_ON = ("companies", "suspended", "in_trial", "last_cut_on")


@dataclass(frozen=True)
class Metric:
    """Una cifra del resumen: en una moneda (importe y cuántos) o de toda la plataforma (`currency` None)."""

    name: str
    currency: str | None
    count: int
    amount: Decimal
    day: date | None = None


@dataclass(frozen=True)
class Headcount:
    """Plantilla activa de una empresa: empleados y validadores. Decisión del dueño del producto: un validador
    activo cuenta como un empleado en el cobro por empleado activo (`total`); se guardan aparte para el desglose."""

    employees: int = 0
    validators: int = 0

    @property
    def total(self) -> int:
        """Lo que se cobra en el modo por empleado activo."""
        return self.employees + self.validators


@dataclass(frozen=True)
class OverdueCompany:
    """Una empresa con cargos vencidos sin pagar (candidata a la suspensión automática)."""

    company_id: int
    oldest_due_on: date
    grace_days: int
    grace_until: date | None


class BillingRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ---------- Candado y plan ----------

    def lock_company(self, company_id: int) -> Company | None:
        """La empresa bloqueada (`FOR UPDATE`): toda operación de cobranza de una empresa (emitir, pagar,
        anular, suspender, cambiar el plan) se serializa en este candado, así los saldos nunca se
        calculan con datos que otra transacción está cambiando. También lo que cambia cuántos validadores activos
        puede tener o tiene (su límite, darlos de alta o activarlos): el límite se revisa sin carreras. No bloquea
        las lecturas."""
        return self.db.get(Company, company_id, with_for_update={"of": Company}, populate_existing=True)

    def plan(self, company_id: int) -> BillingPlan | None:
        return self.db.get(BillingPlan, company_id)

    def add_plan(self, plan: BillingPlan) -> BillingPlan:
        self.db.add(plan)
        self.db.flush()
        return plan

    def plans_by_ids(self, company_ids: Iterable[int]) -> dict[int, BillingPlan]:
        ids = set(company_ids)
        if not ids:
            return {}
        return {p.company_id: p for p in self.db.scalars(select(BillingPlan).where(BillingPlan.company_id.in_(ids)))}

    def plans_due(self, last_closed_day: date, limit: int) -> list[int]:
        """Empresas cuyo corte ya pasó (índice por `next_cut_on`), la más atrasada primero. Una empresa en
        «Eliminados» no se cobra (el JOIN con su llave primaria la deja fuera: borrado lógico)."""
        stmt = (
            select(BillingPlan.company_id)
            .join(Company, Company.id == BillingPlan.company_id)
            .where(BillingPlan.next_cut_on <= last_closed_day)
            .order_by(BillingPlan.next_cut_on, BillingPlan.company_id)
            .limit(limit)
        )
        return list(self.db.scalars(stmt))

    def plans_to_forecast(self, since: datetime, limit: int) -> list[BillingPlan]:
        """Planes cuyo pronóstico no se ha refrescado desde `since` (la condición es el cursor: lo que no
        cupo en esta vuelta sigue en la siguiente). La tabla tiene una fila por empresa; las de una empresa en
        «Eliminados» no se pronostican."""
        stmt = (
            select(BillingPlan)
            .join(Company, Company.id == BillingPlan.company_id)
            .where((BillingPlan.forecast_at.is_(None)) | (BillingPlan.forecast_at < since))
            .order_by(BillingPlan.company_id)
            .limit(limit)
        )
        return list(self.db.scalars(stmt))

    def earliest_start(self) -> date | None:
        return self.db.scalar(select(func.min(BillingPlan.starts_on)))

    def has_movements(self, company_id: int) -> bool:
        """¿Tiene cargos o pagos? (una empresa con movimientos no se elimina: se desactiva o se suspende)."""
        return self.movement_currency(company_id) is not None

    def movement_currency(self, company_id: int) -> str | None:
        """La moneda de sus cargos o pagos (también anulados) o None si aún no tiene ninguno: desde el primero, la
        moneda de la empresa queda fija. A lo más dos búsquedas por los índices que empiezan con la empresa."""
        charge = self.db.scalar(select(Charge.currency).where(Charge.company_id == company_id).limit(1))
        if charge is not None:
            return charge
        return self.db.scalar(select(Payment.currency).where(Payment.company_id == company_id).limit(1))

    # ---------- Plantilla diaria (empleados y validadores activos por día) ----------

    def record_status(self, company_id: int, employee_id: int, active: bool) -> None:
        """Una alta, baja o reactivación de un empleado (en la transacción de quien la hace)."""
        self.db.add(
            EmployeeStatusEvent(
                company_id=company_id, employee_id=employee_id, active=active, occurred_at=datetime.now(UTC)
            )
        )

    def record_validator_status(self, company_id: int, validator_id: int, active: bool) -> None:
        """Una alta, baja o reactivación de un validador (en la transacción de quien la hace): cuenta para el cobro
        como un empleado."""
        self.db.add(
            ValidatorStatusEvent(
                company_id=company_id, validator_id=validator_id, active=active, occurred_at=datetime.now(UTC)
            )
        )

    def closed_days(self, start: date, end: date) -> set[date]:
        """Días del rango cuya plantilla ya se cerró."""
        stmt = select(DailyTask.day).where(DailyTask.task == HEADCOUNT_TASK, DailyTask.day.between(start, end))
        return set(self.db.scalars(stmt))

    def close_headcount(self, day: date, start: datetime, end: datetime) -> int:
        """Cierra la plantilla de un día para TODAS las empresas en una sentencia (y lo anota).

        Cuenta a quien estuvo activo en algún momento del día, empleados y validadores por separado (cada uno con su
        historial): activo al empezarlo (su último evento anterior es un alta) o dado de alta durante él. Repetible:
        el historial de un día que ya pasó no cambia, así que recalcularlo da lo mismo. Devuelve cuántas empresas
        tuvieron alguien activo."""
        people = union(
            *_active_during(EmployeeStatusEvent, EmployeeStatusEvent.employee_id, _EMPLOYEES, start, end),
            *_active_during(ValidatorStatusEvent, ValidatorStatusEvent.validator_id, _VALIDATORS, start, end),
        ).subquery()
        counts = (
            select(
                people.c.company_id,
                literal(day, Date),
                func.count().filter(people.c.kind == _EMPLOYEES),
                func.count().filter(people.c.kind == _VALIDATORS),
            )
            .where(people.c.company_id.is_not(None))
            .group_by(people.c.company_id)
        )
        dialect = postgresql if self.db.get_bind().dialect.name == "postgresql" else sqlite
        columns = ["company_id", "day", "active_employees", "active_validators"]
        insert = dialect.insert(HeadcountDay).from_select(columns, counts)
        result = self.db.execute(
            insert.on_conflict_do_update(
                index_elements=["company_id", "day"],
                set_={
                    "active_employees": insert.excluded.active_employees,
                    "active_validators": insert.excluded.active_validators,
                },
            )
        )
        marker = dialect.insert(DailyTask).values(task=HEADCOUNT_TASK, day=day, done_at=datetime.now(UTC))
        self.db.execute(
            marker.on_conflict_do_update(index_elements=["task", "day"], set_={"done_at": marker.excluded.done_at})
        )
        return int(getattr(result, "rowcount", 0) or 0)

    def headcount(self, company_id: int, start: date, end: date) -> dict[date, Headcount]:
        """Empleados y validadores activos de cada día cerrado de un periodo (en el modo por empleado activo se cobran
        los dos: `Headcount.total`; aparte dan el desglose de los días-persona). Por la llave primaria: a lo más los
        días del periodo."""
        stmt = select(HeadcountDay.day, HeadcountDay.active_employees, HeadcountDay.active_validators).where(
            HeadcountDay.company_id == company_id, HeadcountDay.day.between(start, end)
        )
        return {day: Headcount(int(employees), int(validators)) for day, employees, validators in self.db.execute(stmt)}

    def active_headcount(self, company_ids: Iterable[int]) -> dict[int, Headcount]:
        """Empleados y validadores activos HOY por empresa, en UNA sentencia para todas (dos GROUP BY unidos: los
        empleados por su índice de empresa, los validadores por el de cuentas `(company_id, role)`). Toda empresa
        pedida tiene su entrada (en ceros si no tiene a nadie)."""
        ids = set(company_ids)
        if not ids:
            return {}
        employees = (
            select(Employee.company_id, literal(_EMPLOYEES), func.count())
            .where(Employee.company_id.in_(ids), Employee.active.is_(True))
            .group_by(Employee.company_id)
        )
        validators = (
            select(User.company_id, literal(_VALIDATORS), func.count())
            .where(User.company_id.in_(ids), User.role == UserRole.VALIDATOR, User.active.is_(True))
            .group_by(User.company_id)
        )
        both: Any = union_all(employees, validators)
        counts = {(int(row[0]), int(row[1])): int(row[2]) for row in self.db.execute(both)}
        return {
            company: Headcount(counts.get((company, _EMPLOYEES), 0), counts.get((company, _VALIDATORS), 0))
            for company in ids
        }

    def daily_task_done(self, task: str, day: date) -> bool:
        return self.db.get(DailyTask, (task, day)) is not None

    def mark_daily_task(self, task: str, day: date) -> None:
        self.db.add(DailyTask(task=task, day=day, done_at=datetime.now(UTC)))

    # ---------- Cargos ----------

    def last_cut(self, company_id: int) -> date | None:
        """El corte del último cargo emitido (anulado o no: un corte nunca se vuelve a cobrar)."""
        return self.db.scalar(select(func.max(Charge.cut_on)).where(Charge.company_id == company_id))

    def last_sequence(self, company_id: int) -> int:
        return int(self.db.scalar(select(func.max(Charge.sequence)).where(Charge.company_id == company_id)) or 0)

    def add_charge(self, charge: Charge, lines: list[ChargeLine]) -> Charge:
        self.db.add(charge)
        self.db.flush()
        for line in lines:
            line.charge_id, line.company_id = charge.id, charge.company_id
        insert_many(self.db, lines)
        return charge

    def charge(self, company_id: int, charge_id: int) -> Charge | None:
        found = self.db.get(Charge, charge_id)
        return found if found is not None and found.company_id == company_id else None

    def charges_page(self, company_id: int, status: str | None, *, offset: int, limit: int) -> tuple[list[Charge], int]:
        """Cargos de la empresa, el corte más reciente primero (índice único empresa + corte)."""
        stmt = select(Charge).where(Charge.company_id == company_id)
        if status:
            stmt = stmt.where(Charge.status == status)
        total = int(self.db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
        items = self.db.scalars(stmt.order_by(Charge.cut_on.desc(), Charge.id.desc()).offset(offset).limit(limit))
        return list(items), total

    def open_charges(self, company_id: int) -> list[Charge]:
        """Cargos abiertos de la empresa, el más antiguo primero (índice parcial de abiertos)."""
        stmt = select(Charge).where(Charge.company_id == company_id, _OPEN).order_by(Charge.cut_on, Charge.id)
        return list(self.db.scalars(stmt))

    def lines(self, charge_id: int) -> list[ChargeLine]:
        return list(
            self.db.scalars(select(ChargeLine).where(ChargeLine.charge_id == charge_id).order_by(ChargeLine.month))
        )

    def charge_allocations(self, charge_id: int) -> list[Any]:
        """Pagos aplicados a un cargo (con su fecha y referencia), en orden."""
        stmt = (
            select(PaymentAllocation.payment_id, Payment.paid_on, Payment.reference, PaymentAllocation.amount)
            .join(Payment, Payment.id == PaymentAllocation.payment_id)
            .where(PaymentAllocation.charge_id == charge_id)
            .order_by(Payment.paid_on, Payment.id)
        )
        return list(self.db.execute(stmt).all())

    def charges_by_ids(self, ids: Iterable[int]) -> dict[int, Charge]:
        wanted = set(ids)
        if not wanted:
            return {}
        return {c.id: c for c in self.db.scalars(select(Charge).where(Charge.id.in_(wanted)))}

    def overdue_companies(self, today: date) -> list[OverdueCompany]:
        """Empresas activas, no suspendidas, con cargos abiertos ya vencidos: su vencimiento más antiguo y
        su gracia (índice parcial de cargos abiertos; una fila por empresa)."""
        stmt = (
            select(Charge.company_id, func.min(Charge.due_on), BillingPlan.grace_days, BillingPlan.grace_until)
            .join(BillingPlan, BillingPlan.company_id == Charge.company_id)
            .join(Company, Company.id == Charge.company_id)
            .where(_OPEN, Charge.due_on < today, Company.suspended_at.is_(None), Company.active.is_(True))
            .group_by(Charge.company_id, BillingPlan.grace_days, BillingPlan.grace_until)
            .order_by(Charge.company_id)
        )
        return [OverdueCompany(int(c), due, int(g), until) for c, due, g, until in self.db.execute(stmt)]

    # ---------- Pagos ----------

    def add_payment(self, payment: Payment) -> Payment:
        """Inserta el pago (con su id, que nombra el objeto de su comprobante en el bucket)."""
        self.db.add(payment)
        self.db.flush()
        return payment

    def payment(self, company_id: int, payment_id: int) -> Payment | None:
        found = self.db.get(Payment, payment_id)
        return found if found is not None and found.company_id == company_id else None

    def payments_page(
        self, company_id: int, status: str | None, *, offset: int, limit: int
    ) -> tuple[list[Payment], int]:
        """Pagos de la empresa, el más reciente primero (índice empresa + fecha + id)."""
        stmt = select(Payment).where(Payment.company_id == company_id)
        if status:
            stmt = stmt.where(Payment.status == status)
        total = int(self.db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
        order = (Payment.paid_on.desc(), Payment.id.desc())
        return list(self.db.scalars(stmt.order_by(*order).offset(offset).limit(limit))), total

    def payments_with_credit(self, company_id: int) -> list[Payment]:
        """Pagos confirmados con saldo sin aplicar, el más antiguo primero."""
        stmt = (
            select(Payment)
            .where(Payment.company_id == company_id, _CONFIRMED, Payment.applied < Payment.amount)
            .order_by(Payment.paid_on, Payment.id)
        )
        return list(self.db.scalars(stmt))

    def add_allocations(self, allocations: list[PaymentAllocation]) -> None:
        insert_many(self.db, allocations)

    def allocations_of(self, *, payment_id: int | None = None, charge_id: int | None = None) -> list[PaymentAllocation]:
        stmt = select(PaymentAllocation)
        if payment_id is not None:
            stmt = stmt.where(PaymentAllocation.payment_id == payment_id)
        if charge_id is not None:
            stmt = stmt.where(PaymentAllocation.charge_id == charge_id)
        return list(self.db.scalars(stmt))

    def delete_allocations(self, allocations: list[PaymentAllocation]) -> None:
        """Borra esas aplicaciones en una sentencia (anular un pago o un cargo)."""
        if not allocations:
            return
        keys = [(a.payment_id, a.charge_id) for a in allocations]
        stmt = delete(PaymentAllocation).where(
            tuple_(PaymentAllocation.payment_id, PaymentAllocation.charge_id).in_(keys)
        )
        self.db.execute(stmt.execution_options(synchronize_session=False))
        for allocation in allocations:
            self.db.expunge(allocation)

    def payments_by_ids(self, ids: Iterable[int]) -> dict[int, Payment]:
        wanted = set(ids)
        if not wanted:
            return {}
        return {p.id: p for p in self.db.scalars(select(Payment).where(Payment.id.in_(wanted)))}

    # ---------- Saldos ----------

    def balances(self, company_ids: Iterable[int], today: date) -> dict[int, Balance]:
        """Saldo de cada empresa, en su moneda: dos GROUP BY (cargos y pagos) para toda la página. Cuenta también
        los movimientos anulados (fijan la moneda) y toma la moneda de ellos."""
        ids = set(company_ids)
        if not ids:
            return {}
        overdue = and_(_OPEN, Charge.due_on < today)
        open_balance = Charge.total - Charge.paid
        charges = (
            select(
                Charge.company_id,
                func.sum(case((_LIVE, Charge.total), else_=0)),
                func.sum(case((_LIVE, Charge.paid), else_=0)),
                func.sum(case((_OPEN, open_balance), else_=0)),
                func.sum(case((overdue, open_balance), else_=0)),
                func.count().filter(_OPEN),
                func.count().filter(overdue),
                func.min(case((_OPEN, Charge.due_on), else_=None)),
                func.count(),
                func.max(Charge.currency),
            )
            .where(Charge.company_id.in_(ids))
            .group_by(Charge.company_id)
        )
        payments = (
            select(
                Payment.company_id,
                func.sum(case((_CONFIRMED, Payment.amount - Payment.applied), else_=0)),
                func.max(case((_CONFIRMED, Payment.paid_on), else_=None)),
                func.count(),
                func.max(Payment.currency),
            )
            .where(Payment.company_id.in_(ids))
            .group_by(Payment.company_id)
        )
        found: dict[int, dict[str, Any]] = {cid: {} for cid in ids}
        for row in self.db.execute(charges):
            cid, charged, paid, outstanding, late, open_count, late_count, oldest, count, currency = row
            found[int(cid)].update(
                charged=_money(charged),
                paid=_money(paid),
                outstanding=_money(outstanding),
                overdue=_money(late),
                open_charges=int(open_count or 0),
                overdue_charges=int(late_count or 0),
                oldest_due_on=oldest,
                movements=int(count),
                currency=currency,
            )
        for cid, credit, last, count, currency in self.db.execute(payments):
            values = found[int(cid)]
            values.update(
                credit=_money(credit),
                last_payment_on=last,
                movements=values.get("movements", 0) + int(count),
                currency=values.get("currency") or currency,
            )
        return {cid: Balance(**values) for cid, values in found.items()}

    # ---------- Estado de cuenta ----------

    def statement_page(self, company_id: int, *, offset: int, limit: int) -> tuple[list[Any], int]:
        """Movimientos de la empresa (cargos vigentes como cargo, pagos confirmados como abono) con el
        saldo acumulado tras cada uno, el más reciente primero. El saldo es una ventana sobre los
        movimientos de UNA empresa (pocos: uno por periodo y uno por pago)."""
        money = Numeric(14, 2)
        charges = select(
            Charge.issued_on.label("date"),
            literal(0).label("kind_order"),
            Charge.id.label("id"),
            Charge.sequence.label("sequence"),
            Charge.period_start.label("period_start"),
            Charge.period_end.label("period_end"),
            cast(null(), String).label("method"),
            cast(null(), String).label("reference"),
            Charge.currency.label("currency"),
            Charge.total.label("debit"),
            cast(literal(0), money).label("credit"),
        ).where(Charge.company_id == company_id, _LIVE)
        payments = select(
            Payment.paid_on.label("date"),
            literal(1).label("kind_order"),
            Payment.id.label("id"),
            cast(null(), Integer).label("sequence"),
            cast(null(), Date).label("period_start"),
            cast(null(), Date).label("period_end"),
            Payment.method.label("method"),
            Payment.reference.label("reference"),
            Payment.currency.label("currency"),
            cast(literal(0), money).label("debit"),
            Payment.amount.label("credit"),
        ).where(Payment.company_id == company_id, _CONFIRMED)
        entries = union_all(charges, payments).subquery()
        running = func.sum(entries.c.debit - entries.c.credit).over(
            order_by=(entries.c.date, entries.c.kind_order, entries.c.id)
        )
        ledger = select(entries, running.label("balance")).subquery()
        total = int(self.db.scalar(select(func.count()).select_from(entries)) or 0)
        stmt = (
            select(ledger)
            .order_by(ledger.c.date.desc(), ledger.c.kind_order.desc(), ledger.c.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(self.db.execute(stmt).all()), total

    # ---------- Plataforma ----------

    def overview(self, today: date, month_start: date) -> list[Metric]:
        """Las cifras de la plataforma en UNA consulta: un UNION ALL de consultas chicas, cada una por su índice
        (emisión, cobro y corte por fecha; abiertos y saldo a favor por sus parciales). El dinero sale POR
        MONEDA (`GROUP BY currency`, nunca sumado entre monedas ni convertido) y los conteos de empresas y el
        último corte, sin moneda."""
        overdue = and_(_OPEN, Charge.due_on < today)
        last_cut = select(func.max(Charge.cut_on)).where(_LIVE).scalar_subquery()
        money = Numeric(14, 2)

        def per_currency(name: str, currency: Any, amount: Any, *where: Any, count: Any = None) -> Any:
            return (
                select(
                    literal(name).label("metric"),
                    currency.label("currency"),
                    (count if count is not None else func.count()).label("quantity"),
                    cast(func.coalesce(func.sum(amount), 0), money).label("amount"),
                    cast(null(), Date).label("day"),
                )
                .where(*where)
                .group_by(currency)
            )

        def platform(name: str, count: Any, day: Any = None) -> Any:
            return select(
                literal(name).label("metric"),
                cast(null(), String).label("currency"),
                count.label("quantity"),
                cast(literal(0), money).label("amount"),
                (day if day is not None else cast(null(), Date)).label("day"),
            )

        stmt = union_all(
            per_currency(BILLED, Charge.currency, Charge.total, _LIVE, Charge.issued_on.between(month_start, today)),
            per_currency(
                COLLECTED, Payment.currency, Payment.amount, _CONFIRMED, Payment.paid_on.between(month_start, today)
            ),
            per_currency(OUTSTANDING, Charge.currency, Charge.total - Charge.paid, _OPEN),
            per_currency(
                OVERDUE,
                Charge.currency,
                Charge.total - Charge.paid,
                overdue,
                count=func.count(func.distinct(Charge.company_id)),
            ),
            per_currency(
                CREDIT, Payment.currency, Payment.amount - Payment.applied, _CONFIRMED, Payment.applied < Payment.amount
            ),
            per_currency(PLANS, BillingPlan.currency, BillingPlan.forecast_total),
            per_currency(LAST_CUT, Charge.currency, Charge.total, _LIVE, Charge.cut_on == last_cut),
            platform(COMPANIES, select(func.count()).select_from(Company).scalar_subquery()),
            platform(SUSPENDED, select(func.count()).where(Company.suspended_at.is_not(None)).scalar_subquery()),
            platform(IN_TRIAL, select(func.count()).where(BillingPlan.trial_ends_on >= today).scalar_subquery()),
            platform(LAST_CUT_ON, literal(0), day=last_cut),
        )
        return [
            Metric(row.metric, row.currency, int(row.quantity or 0), _money(row.amount), row.day)
            for row in self.db.execute(stmt)
        ]

    def company_page(
        self,
        today: date,
        *,
        search: str | None,
        suspended: bool | None,
        overdue: bool | None,
        offset: int,
        limit: int,
    ) -> tuple[list[Company], int]:
        """Empresas para la cobranza: la de cargo vencido más antiguo primero, luego por nombre. El
        vencimiento de cada empresa sale del índice parcial de cargos abiertos (una búsqueda por empresa)."""
        oldest_overdue = (
            select(func.min(Charge.due_on))
            .where(Charge.company_id == Company.id, _OPEN, Charge.due_on < today)
            .correlate(Company)
            .scalar_subquery()
        )
        stmt = select(Company)
        term = search_term(search)
        if term:
            stmt = stmt.where(contains_text(company_search_text(), term))
        if suspended is not None:
            stmt = stmt.where(Company.suspended_at.is_not(None) if suspended else Company.suspended_at.is_(None))
        if overdue is not None:
            stmt = stmt.where(oldest_overdue.is_not(None) if overdue else oldest_overdue.is_(None))
        total = int(self.db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
        ordered = stmt.order_by(oldest_overdue.asc().nulls_last(), func.lower(Company.name), Company.id)
        return list(self.db.scalars(ordered.offset(offset).limit(limit))), total
