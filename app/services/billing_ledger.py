"""Movimientos de dinero de una empresa: emitir el cargo de un corte, aplicar los pagos (y el saldo a
favor) a los cargos abiertos, anular un pago o un cargo, y suspender o reactivar a la empresa.

Todo con la empresa bloqueada (`BillingRepository.lock_company`): dos operaciones de la misma empresa
(un pago y la emisión del mantenimiento, dos pagos a la vez) nunca calculan saldos sobre datos que la
otra está cambiando. Nada confirma la transacción aquí: lo hace quien llama (el servicio de la API o el
mantenimiento), junto con su propio cambio.

Moneda: cada cargo se emite en la moneda del plan y se redondea a sus decimales (catálogo `currencies`); un
pago solo se aplica a cargos de SU moneda (una empresa tiene una sola, pero el reparto no confía en ello).
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, NotFoundError
from app.models import (
    BillingPlan,
    Charge,
    ChargeLine,
    ChargeStatus,
    Company,
    Payment,
    PaymentAllocation,
    PaymentStatus,
    PricingMode,
    SessionRevocationReason,
    SuspensionReason,
)
from app.repositories.billing_repository import BillingRepository, Headcount
from app.services.billing_calc import Estimate, PlanTerms, compute_charge, estimate, plan_terms
from app.services.billing_rules import CENTS, ZERO, allocate, suspension_due
from app.services.catalog_service import get_catalogs
from app.services.session_service import SessionService

logger = logging.getLogger(__name__)


def currency_decimals(currency: str) -> int:
    """Decimales de una moneda según el catálogo (una que no está en él —no debería: la FK lo impide— usa
    centavos)."""
    row = get_catalogs().get("currencies", currency)
    return int(row["decimals"]) if row else CENTS


def _split(daily: Mapping[date, Headcount]) -> tuple[dict[date, int], dict[date, int]]:
    """La plantilla diaria en lo que se cobra cada día (empleados + validadores) y en cuántos fueron validadores (el
    desglose de los días-persona; no cambia el importe)."""
    return {day: people.total for day, people in daily.items()}, {
        day: people.validators for day, people in daily.items()
    }


def terms_of(plan: BillingPlan) -> PlanTerms:
    """Los términos de un plan guardado (para las reglas de cobro), con los decimales de su moneda."""
    return plan_terms(
        pricing_mode=plan.pricing_mode,
        unit_price=plan.unit_price,
        price_period=plan.price_period,
        interval_months=plan.interval_months,
        starts_on=plan.starts_on,
        trial_days=plan.trial_days,
        discount_type=plan.discount_type,
        discount_value=plan.discount_value,
        discount_recurrence=plan.discount_recurrence,
        discount_periods=plan.discount_periods,
        tax_rate=plan.tax_rate,
        decimals=currency_decimals(plan.currency),
    )


@dataclass(frozen=True)
class Applied:
    """Cuánto de un pago se aplicó a un cargo."""

    payment: Payment
    charge: Charge
    amount: Decimal


class Ledger:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = BillingRepository(db)

    def lock(self, company_id: int) -> Company:
        company = self.repo.lock_company(company_id)
        if company is None:
            raise NotFoundError(code="COMPANY_NOT_FOUND")
        return company

    # ---------- Emisión ----------

    def issue_due_charge(self, company_id: int, today: date, closed_from: date) -> Charge | None:
        """Emite el cargo del corte que ya pasó (idempotente: un cargo por corte). Espera a que estén
        cerrados los días del periodo desde `closed_from` (los anteriores se toman como estén). Un
        periodo sin días cobrables (todo demo) no emite cargo: solo avanza al siguiente corte."""
        self.lock(company_id)
        plan = self.repo.plan(company_id)
        if plan is None or plan.next_cut_on >= today:
            return None
        terms = terms_of(plan)
        cut = plan.next_cut_on
        days = terms.billable(cut)
        per_user = plan.pricing_mode == PricingMode.PER_USER
        if per_user and days and not self._closed(days, closed_from):
            return None  # la plantilla de algún día aún no se cierra: en la siguiente vuelta
        plan.next_cut_on = terms.cut_after(cut)
        if not days:
            return None
        sequence = self.repo.last_sequence(company_id) + 1
        daily = self.repo.headcount(company_id, days[0], days[-1]) if per_user else {}
        calc = compute_charge(terms, cut, sequence, *_split(daily))
        result = calc.totals
        charge = Charge(
            company_id=company_id,
            sequence=sequence,
            cut_on=cut,
            period_start=calc.period_start,
            period_end=calc.period_end,
            issued_on=today,
            due_on=today,
            billable_days=calc.billable_days,
            units=calc.units,
            validator_units=calc.validator_units,
            pricing_mode=plan.pricing_mode,
            unit_price=plan.unit_price,
            price_period=plan.price_period,
            tax_rate=plan.tax_rate,
            currency=plan.currency,
            subtotal=result.subtotal,
            discount=result.discount,
            tax=result.tax,
            total=result.total,
            paid=ZERO,
            status=ChargeStatus.PAID if result.total == ZERO else ChargeStatus.OPEN,
        )
        lines = [
            ChargeLine(
                month=line.month,
                days=calc.days_in(line.month),
                units=line.units,
                validator_units=line.validator_units,
                amount=line.amount,
            )
            for line in calc.lines
        ]
        self.repo.add_charge(charge, lines)
        self.settle(company_id)
        return charge

    def _closed(self, days: list[date], closed_from: date) -> bool:
        wanted = {day for day in days if day >= closed_from}
        return not wanted or wanted <= self.repo.closed_days(min(wanted), max(wanted))

    def estimate(self, plan: BillingPlan, today: date, active_now: Headcount) -> Estimate:
        """El periodo en curso (el del próximo corte) con lo devengado y su pronóstico (`active_now`: la plantilla de
        hoy, para los días que faltan)."""
        terms = terms_of(plan)
        days = terms.billable(plan.next_cut_on)
        closed, closed_validators = _split(self.repo.headcount(plan.company_id, days[0], days[-1]) if days else {})
        sequence = self.repo.last_sequence(plan.company_id) + 1
        return estimate(
            terms,
            plan.next_cut_on,
            sequence,
            today,
            closed,
            active_now.total,
            closed_validators=closed_validators,
            validators_now=active_now.validators,
        )

    # ---------- Pagos ----------

    def settle(self, company_id: int) -> list[Applied]:
        """Aplica el saldo a favor de los pagos (el más antiguo primero) a los cargos abiertos (el más
        antiguo primero); lo que sobra queda a favor. Un pago solo cubre cargos de SU moneda: el dinero de una
        moneda jamás paga una deuda en otra. Una inserción para todas las aplicaciones."""
        open_charges = self.repo.open_charges(company_id)
        pending = {charge.id: charge for charge in open_charges}
        applied: list[Applied] = []
        for payment in self.repo.payments_with_credit(company_id):
            balances = [
                (c.id, c.total - c.paid) for c in open_charges if c.id in pending and c.currency == payment.currency
            ]
            parts, _ = allocate(payment.amount - payment.applied, balances)
            for charge_id, amount in parts:
                charge = pending[charge_id]
                charge.paid += amount
                payment.applied += amount
                if charge.paid >= charge.total:
                    charge.status = ChargeStatus.PAID
                    del pending[charge_id]
                applied.append(Applied(payment, charge, amount))
        self.repo.add_allocations(
            [
                PaymentAllocation(
                    payment_id=a.payment.id, charge_id=a.charge.id, company_id=company_id, amount=a.amount
                )
                for a in applied
            ]
        )
        self.db.flush()  # sin autoflush: lo que se consulte después (¿sigue debiendo?) ve los saldos nuevos
        return applied

    def void_payment(self, company_id: int, payment_id: int, reason: str, actor: str) -> Payment:
        """Anula un pago: los cargos que cubría vuelven a quedar por pagar (y se cubren con otro saldo a
        favor, si lo hay)."""
        self.lock(company_id)
        payment = self.repo.payment(company_id, payment_id)
        if payment is None:
            raise NotFoundError(code="PAYMENT_NOT_FOUND")
        if payment.status == PaymentStatus.VOID:
            raise ConflictError(code="PAYMENT_ALREADY_VOID")
        allocations = self.repo.allocations_of(payment_id=payment.id)
        charges = self.repo.charges_by_ids(a.charge_id for a in allocations)
        for allocation in allocations:
            charge = charges[allocation.charge_id]
            charge.paid -= allocation.amount
            if charge.status == ChargeStatus.PAID:
                charge.status = ChargeStatus.OPEN
        self.repo.delete_allocations(allocations)
        payment.applied = ZERO
        payment.status = PaymentStatus.VOID
        payment.voided_at, payment.voided_by, payment.void_reason = datetime.now(UTC), actor, reason
        self.db.flush()
        self.settle(company_id)
        return payment

    def void_charge(self, company_id: int, charge_id: int, reason: str, actor: str) -> Charge:
        """Anula un cargo: no se cobra y lo que tenía aplicado vuelve a sus pagos (saldo a favor, que se
        aplica a los demás cargos abiertos)."""
        self.lock(company_id)
        charge = self.repo.charge(company_id, charge_id)
        if charge is None:
            raise NotFoundError(code="CHARGE_NOT_FOUND")
        if charge.status == ChargeStatus.VOID:
            raise ConflictError(code="CHARGE_ALREADY_VOID")
        allocations = self.repo.allocations_of(charge_id=charge.id)
        payments = self.repo.payments_by_ids(a.payment_id for a in allocations)
        for allocation in allocations:
            payments[allocation.payment_id].applied -= allocation.amount
        self.repo.delete_allocations(allocations)
        charge.paid = ZERO
        charge.status = ChargeStatus.VOID
        charge.voided_at, charge.voided_by, charge.void_reason = datetime.now(UTC), actor, reason
        self.db.flush()
        self.settle(company_id)
        return charge

    # ---------- Suspensión ----------

    def suspend(self, company: Company, reason: SuspensionReason, note: str | None, actor: str | None) -> None:
        """Suspende y cierra AL MOMENTO todas las sesiones de la empresa (administradores, validadores y
        los empleados que entraron a ella), en la misma transacción."""
        company.suspended_at = datetime.now(UTC)
        company.suspension_reason = reason
        company.suspension_note = note
        company.suspended_by = actor
        SessionService(self.db).close_company(company.id, SessionRevocationReason.COMPANY_SUSPENDED)

    @staticmethod
    def lift(company: Company) -> None:
        company.suspended_at = None
        company.suspension_reason = None
        company.suspension_note = None
        company.suspended_by = None

    def overdue_past_grace(self, company_id: int, today: date) -> bool:
        """¿Le queda algún cargo abierto que ya pasó su gracia (motivo de suspensión automática)?"""
        plan = self.repo.plan(company_id)
        grace = plan.grace_days if plan else 0
        return any(suspension_due(c.due_on, grace, today) for c in self.repo.open_charges(company_id))

    def reactivate_if_paid(self, company: Company, today: date) -> bool:
        """Una empresa suspendida POR FALTA DE PAGO se reactiva sola cuando ya no debe nada vencido más
        allá de su gracia. Una suspensión manual solo la levanta el ADMIN."""
        if company.suspension_reason != SuspensionReason.NON_PAYMENT or self.overdue_past_grace(company.id, today):
            return False
        self.lift(company)
        logger.info("Empresa %s reactivada: se cubrió su adeudo vencido", company.id)
        return True
