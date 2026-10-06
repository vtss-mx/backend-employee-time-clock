"""Cobranza de las empresas (solo el ADMIN de la plataforma): plan de cobro, vista previa, estimación en
vivo del periodo en curso, cargos, pagos registrados a mano, estado de cuenta, suspensión y
reactivación, y el resumen de la plataforma.

Decisiones del dueño del producto (detalle en el README, "Cobranza"):
- Se cobra por empleado ACTIVO, prorrateado por día (o un monto fijo por empresa); precios sin IVA y
  el IVA a la tasa de cada empresa; corte el último día del mes, un cargo cada `interval_months`.
- Los pagos los registra y confirma el ADMIN (sin pasarela); se aplican al cargo abierto más antiguo y
  lo que sobra queda a favor.
- Falta de pago: suspensión automática después de los días de gracia (mantenimiento) y suspensión o
  reactivación manual. Suspender cierra al momento todas las sesiones de la empresa.
- Cambiar el plan aplica desde el próximo cargo: lo emitido guarda su copia del precio y del IVA.
- Cada empresa se cobra en la moneda de su plan (MXN, USD o EUR; catálogo `currencies`). La moneda se cambia
  libremente hasta el primer cargo o pago; después queda fija (422 `CURRENCY_LOCKED`). Un pago debe ser en la
  moneda de la empresa (422 `CURRENCY_MISMATCH`). Los totales de la plataforma salen POR MONEDA: nunca se
  suman monedas distintas ni se convierten. El IVA es de cada empresa, independiente de la moneda.
"""

import base64
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.core.clock import business_today
from app.core.exceptions import ConflictError, NotFoundError, UnprocessableError
from app.core.object_storage import StorageError
from app.i18n import t
from app.models import (
    BillingPlan,
    BillingStatus,
    Charge,
    ChargeStatus,
    Company,
    DiscountRecurrence,
    DiscountType,
    Payment,
    PaymentStatus,
    PricePeriod,
    PricingMode,
    SuspensionReason,
)
from app.repositories.billing_repository import (
    BILLED,
    COLLECTED,
    COMPANIES,
    CREDIT,
    IN_TRIAL,
    LAST_CUT,
    LAST_CUT_ON,
    OUTSTANDING,
    OVERDUE,
    PLANS,
    SUSPENDED,
    Balance,
    BillingRepository,
    Headcount,
    Metric,
)
from app.repositories.company_repository import CompanyRepository
from app.schemas.billing import (
    AccruedRead,
    AllocationRead,
    AppliedRead,
    BalanceRead,
    BillingAccount,
    BillingOverview,
    BillingPlanIn,
    BillingPlanRead,
    ChargeDetail,
    ChargeLineRead,
    ChargeList,
    ChargePreviewRead,
    ChargeRead,
    ChargeVoidResult,
    CompanyBillingList,
    CompanyBillingRow,
    CompanyCounts,
    CurrencyTotals,
    DiscountRead,
    EstimateLine,
    ForecastRead,
    PaymentList,
    PaymentRead,
    PaymentResult,
    PaymentVoidResult,
    PeriodEstimate,
    PlanPreview,
    PlanSummary,
    ReceiptFile,
    ReceiptInfo,
    StatementEntry,
    StatementList,
    SuspensionRead,
)
from app.schemas.common import PageParams
from app.services import image_storage
from app.services.billing_calc import ChargeCalc, PlanTerms, plan_terms, preview
from app.services.billing_ledger import Ledger, currency_decimals
from app.services.billing_rules import ZERO, representable
from app.services.catalog_service import get_catalogs
from app.services.image_storage import PAYMENT_RECEIPTS

logger = logging.getLogger(__name__)
#: Hasta qué tan atrás o adelante puede empezar el cobro de un plan nuevo o cambiado (días).
START_WINDOW_DAYS = 366


@dataclass(frozen=True)
class PaymentInput:
    """Un pago que el ADMIN registra (ya validado por la ruta, salvo lo que depende de hoy y del catálogo)."""

    amount: Decimal
    paid_on: date
    method: str
    reference: str | None
    note: str | None
    #: La moneda en que se recibió: debe ser la de la empresa (422 `CURRENCY_MISMATCH`).
    currency: str


@dataclass(frozen=True)
class Receipt:
    """El comprobante de un pago, ya revisado (tipo por su contenido y tamaño)."""

    file_name: str
    content_type: str
    data: bytes


def _lines(calc: ChargeCalc) -> list[ChargeLineRead]:
    return [
        ChargeLineRead(
            month=line.month,
            days=calc.days_in(line.month),
            units=line.units,
            validator_units=line.validator_units,
            amount=line.amount,
        )
        for line in calc.lines
    ]


def _preview_read(calc: ChargeCalc) -> ChargePreviewRead:
    totals = calc.totals
    return ChargePreviewRead(
        sequence=calc.sequence,
        cut_on=calc.cut_on,
        period_start=calc.period_start,
        period_end=calc.period_end,
        billable_days=calc.billable_days,
        units=calc.units,
        validator_units=calc.validator_units,
        lines=_lines(calc),
        subtotal=totals.subtotal,
        discount=totals.discount,
        tax=totals.tax,
        total=totals.total,
    )


def _currency_error(code: str, field: str) -> UnprocessableError:
    return UnprocessableError(code=code, key="CURRENCY_CHOOSE", field=field)


def _decimals_error(currency: str, decimals: int, field: str) -> UnprocessableError:
    return UnprocessableError(code="AMOUNT_DECIMALS", params={"currency": currency, "count": decimals}, field=field)


def _terms_in(data: BillingPlanIn, starts_on: date, prefix: str = "") -> tuple[dict[str, Any], PlanTerms]:
    """Las columnas del plan (sin la empresa ni la moneda) y sus términos para las reglas, con los decimales de
    su moneda: un descuento de monto fijo que no se puede escribir en ella responde 422 `AMOUNT_DECIMALS`."""
    discount = data.discount
    decimals = currency_decimals(data.currency)
    if discount and discount.type == DiscountType.AMOUNT and not representable(discount.value, decimals):
        raise _decimals_error(data.currency, decimals, f"{prefix}discount.value")
    columns: dict[str, Any] = {
        "pricing_mode": data.pricing_mode.value,
        "unit_price": data.unit_price,
        "price_period": data.price_period.value,
        "interval_months": data.interval_months,
        "starts_on": starts_on,
        "trial_days": data.trial_days,
        "discount_type": discount.type.value if discount else None,
        "discount_value": discount.value if discount else None,
        "discount_recurrence": discount.recurrence.value if discount else None,
        "discount_periods": discount.periods if discount else None,
        "tax_rate": data.tax_rate,
    }
    return columns, plan_terms(**columns, decimals=decimals)


def plan_read(plan: BillingPlan) -> BillingPlanRead:
    discount = (
        DiscountRead(
            type=DiscountType(plan.discount_type),
            value=plan.discount_value or ZERO,
            recurrence=DiscountRecurrence(plan.discount_recurrence or DiscountRecurrence.ALWAYS),
            periods=plan.discount_periods,
        )
        if plan.discount_type
        else None
    )
    return BillingPlanRead(
        pricing_mode=PricingMode(plan.pricing_mode),
        unit_price=plan.unit_price,
        price_period=PricePeriod(plan.price_period),
        interval_months=plan.interval_months,
        starts_on=plan.starts_on,
        trial_days=plan.trial_days,
        discount=discount,
        tax_rate=plan.tax_rate,
        grace_days=plan.grace_days,
        currency=plan.currency,
        trial_ends_on=plan.trial_ends_on,
        next_cut_on=plan.next_cut_on,
        forecast_total=plan.forecast_total,
        forecast_at=plan.forecast_at,
        updated_at=plan.updated_at,
    )


def _catalog_order(codes: Iterable[str]) -> list[str]:
    """Las monedas en el orden del catálogo (una que no estuviera en él, al final por su código)."""
    order = {row["code"]: row["sort_order"] for row in get_catalogs().entries["currencies"]}
    return sorted(codes, key=lambda code: (order.get(code, len(order) + 1), code))


def _currency_totals(currency: str, metrics: Mapping[str, Metric]) -> CurrencyTotals:
    """Las cifras de una moneda (lo que no tuvo filas, en cero)."""
    empty = Metric("", currency, 0, ZERO)

    def get(name: str) -> Metric:
        return metrics.get(name, empty)

    return CurrencyTotals(
        currency=currency,
        companies=get(PLANS).count,
        billed_month=get(BILLED).amount,
        collected_month=get(COLLECTED).amount,
        outstanding=get(OUTSTANDING).amount,
        overdue=get(OVERDUE).amount,
        overdue_companies=get(OVERDUE).count,
        credit=get(CREDIT).amount,
        forecast=get(PLANS).amount,
        last_cut_charges=get(LAST_CUT).count,
        last_cut_total=get(LAST_CUT).amount,
    )


def suspends_on(balance: Balance, plan: BillingPlan | None, company: Company) -> date | None:
    """Primer día en que la suspensión automática aplicaría (si nada se paga)."""
    if company.suspended or balance.oldest_due_on is None or plan is None:
        return None
    day = balance.oldest_due_on + timedelta(days=plan.grace_days + 1)
    if plan.grace_until is not None:
        day = max(day, plan.grace_until + timedelta(days=1))
    return day


def charge_read(charge: Charge, today: date) -> ChargeRead:
    read = ChargeRead.model_validate(charge)
    open_ = charge.status == ChargeStatus.OPEN
    return read.model_copy(update={"balance": charge.total - charge.paid, "overdue": open_ and charge.due_on < today})


def payment_read(payment: Payment) -> PaymentRead:
    receipt = (
        ReceiptInfo(
            file_name=payment.receipt_name, content_type=payment.receipt_type or "", size=payment.receipt_size or 0
        )
        if payment.receipt_name
        else None
    )
    return PaymentRead(
        id=payment.id,
        currency=payment.currency,
        amount=payment.amount,
        paid_on=payment.paid_on,
        method=payment.method,
        reference=payment.reference,
        note=payment.note,
        status=PaymentStatus(payment.status),
        applied=payment.applied,
        unapplied=payment.amount - payment.applied if payment.status == PaymentStatus.CONFIRMED else ZERO,
        receipt=receipt,
        recorded_by=payment.recorded_by,
        created_at=payment.created_at,
        voided_at=payment.voided_at,
        void_reason=payment.void_reason,
        voided_by=payment.voided_by,
    )


class BillingService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = BillingRepository(db)
        self.ledger = Ledger(db)

    # ---------- Consultas ----------

    def _company(self, company_id: int) -> Company:
        company = CompanyRepository(self.db).get(company_id)
        if company is None:
            raise NotFoundError(code="COMPANY_NOT_FOUND")
        return company

    def account(self, company_id: int) -> BillingAccount:
        """Estado de servicio, plan y saldo de la empresa."""
        return self._account(self._company(company_id))

    def _account(self, company: Company) -> BillingAccount:
        today = business_today()
        plan = self.repo.plan(company.id)
        balance = self.repo.balances([company.id], today)[company.id]
        suspension = (
            SuspensionRead(
                reason=SuspensionReason(company.suspension_reason or SuspensionReason.MANUAL),
                note=company.suspension_note,
                suspended_at=company.suspended_at,
                suspended_by=company.suspended_by,
            )
            if company.suspended_at is not None
            else None
        )
        return BillingAccount(
            company_id=company.id,
            company_name=company.name,
            company_active=company.active,
            status=BillingStatus(company.billing_status),
            currency=plan.currency if plan else balance.currency,
            currency_locked=balance.movements > 0,
            suspension=suspension,
            plan=plan_read(plan) if plan else None,
            balance=BalanceRead(
                charged=balance.charged,
                paid=balance.paid,
                outstanding=balance.outstanding,
                overdue=balance.overdue,
                credit=balance.credit,
                balance=balance.outstanding - balance.credit,
                open_charges=balance.open_charges,
                overdue_charges=balance.overdue_charges,
                oldest_due_on=balance.oldest_due_on,
                suspends_on=suspends_on(balance, plan, company),
                last_payment_on=balance.last_payment_on,
            ),
            grace_until=plan.grace_until if plan else None,
        )

    @staticmethod
    def preview(data: BillingPlanIn, employees: int, validators: int = 0) -> PlanPreview:
        """Lo que costará el plan con N empleados y M validadores activos (el primer cargo y un periodo completo), en
        su moneda: cada validador cuenta como un empleado (`Headcount.total`). Nada se guarda: la regla de cobro
        vive aquí y la vista previa del alta la usa tal cual."""
        if not get_catalogs().is_active("currencies", data.currency):
            raise _currency_error("CURRENCY_INVALID", "plan.currency")
        _, terms = _terms_in(data, data.starts_on or business_today(), "plan.")
        result = preview(terms, Headcount(employees, validators).total, validators)
        return PlanPreview(
            currency=data.currency,
            trial_ends_on=result.trial_ends_on,
            first=_preview_read(result.first) if result.first else None,
            recurring=_preview_read(result.recurring),
            monthly_equivalent=result.monthly_equivalent,
        )

    def estimate(self, company_id: int) -> PeriodEstimate:
        """El periodo en curso en vivo: lo devengado hasta hoy y el pronóstico del cargo completo (nada se
        guarda)."""
        self._company(company_id)
        plan = self.repo.plan(company_id)
        if plan is None:
            raise NotFoundError(code="BILLING_PLAN_NOT_FOUND")
        today = business_today()
        active = self.repo.active_headcount([company_id])[company_id]
        result = self.ledger.estimate(plan, today, active)
        calc, totals = result.charge, result.charge.totals
        lines = [EstimateLine(**line.model_dump(), projected=result.projected(line.month)) for line in _lines(calc)]
        return PeriodEstimate(
            currency=plan.currency,
            sequence=calc.sequence,
            cut_on=calc.cut_on,
            period_start=calc.period_start,
            period_end=calc.period_end,
            as_of=today,
            days_total=result.days_total,
            days_elapsed=result.days_elapsed,
            in_trial=result.in_trial,
            trial_ends_on=plan.trial_ends_on,
            active_employees=active.employees,
            active_validators=active.validators,
            accrued=AccruedRead(
                units=result.accrued_units,
                validator_units=result.accrued_validator_units,
                subtotal=result.accrued_subtotal,
            ),
            forecast=ForecastRead(
                units=calc.units,
                validator_units=calc.validator_units,
                subtotal=totals.subtotal,
                discount=totals.discount,
                tax=totals.tax,
                total=totals.total,
            ),
            lines=lines,
        )

    def charges(self, company_id: int, status: str | None, page: PageParams) -> ChargeList:
        self._company(company_id)
        items, total = self.repo.charges_page(company_id, status, offset=page.offset, limit=page.size)
        today = business_today()
        return ChargeList.of([charge_read(c, today) for c in items], total, page)

    def charge(self, company_id: int, charge_id: int) -> ChargeDetail:
        self._company(company_id)
        charge = self.repo.charge(company_id, charge_id)
        if charge is None:
            raise NotFoundError(code="CHARGE_NOT_FOUND")
        return self._charge_detail(charge)

    def _charge_detail(self, charge: Charge) -> ChargeDetail:
        base = charge_read(charge, business_today())
        return ChargeDetail(
            **base.model_dump(),
            pricing_mode=PricingMode(charge.pricing_mode),
            unit_price=charge.unit_price,
            price_period=PricePeriod(charge.price_period),
            tax_rate=charge.tax_rate,
            lines=[
                ChargeLineRead(
                    month=line.month,
                    days=line.days,
                    units=line.units,
                    validator_units=line.validator_units,
                    amount=line.amount,
                )
                for line in self.repo.lines(charge.id)
            ],
            allocations=[
                AllocationRead(payment_id=pid, paid_on=paid_on, reference=reference, amount=amount)
                for pid, paid_on, reference, amount in self.repo.charge_allocations(charge.id)
            ],
            voided_by=charge.voided_by,
        )

    def payments(self, company_id: int, status: str | None, page: PageParams) -> PaymentList:
        self._company(company_id)
        items, total = self.repo.payments_page(company_id, status, offset=page.offset, limit=page.size)
        return PaymentList.of([payment_read(p) for p in items], total, page)

    def receipt(self, company_id: int, payment_id: int) -> ReceiptFile:
        self._company(company_id)
        payment = self.repo.payment(company_id, payment_id)
        if payment is None:
            raise NotFoundError(code="PAYMENT_NOT_FOUND")
        try:  # del bucket (descifrado en memoria, con tiempo límite y verificado)
            data = image_storage.read(PAYMENT_RECEIPTS, payment)
        except StorageError as exc:
            raise image_storage.unavailable(exc) from exc
        if data is None:
            raise NotFoundError(code="RECEIPT_NOT_FOUND")
        return ReceiptFile(
            file_name=payment.receipt_name or "comprobante",
            content_type=payment.receipt_type or "application/octet-stream",
            size=len(data),
            data=base64.b64encode(data).decode(),
        )

    def statement(self, company_id: int, page: PageParams) -> StatementList:
        """Estado de cuenta: cargos y pagos con el saldo acumulado tras cada movimiento."""
        self._company(company_id)
        rows, total = self.repo.statement_page(company_id, offset=page.offset, limit=page.size)
        catalogs = get_catalogs()
        entries = []
        for row in rows:
            if row.kind_order == 0:
                params = {"sequence": row.sequence, "start": row.period_start, "end": row.period_end}
                kind, description = "CHARGE", t("STATEMENT_CHARGE", params)
            else:
                method = catalogs.name("payment_methods", row.method)
                payment = t("STATEMENT_PAYMENT")
                kind, description = "PAYMENT", " · ".join(filter(None, (payment, method, row.reference)))
            entries.append(
                StatementEntry(
                    date=row.date,
                    kind=kind,
                    id=row.id,
                    description=description,
                    currency=row.currency,
                    debit=row.debit,
                    credit=row.credit,
                    balance=row.balance,
                )
            )
        return StatementList.of(entries, total, page)

    def overview(self) -> BillingOverview:
        """Cifras de toda la plataforma en una consulta, POR MONEDA (nunca sumadas entre monedas ni convertidas):
        lo facturado y cobrado en el mes, lo pendiente, lo vencido, el saldo a favor, el pronóstico y el último
        corte; y cuántas empresas están en cada situación."""
        today = business_today()
        month_start = today.replace(day=1)
        metrics = self.repo.overview(today, month_start)
        platform = {m.name: m for m in metrics if m.currency is None}
        by_currency: dict[str, dict[str, Metric]] = {}
        for metric in metrics:
            if metric.currency is not None:
                by_currency.setdefault(metric.currency, {})[metric.name] = metric
        groups = [_currency_totals(code, by_currency[code]) for code in _catalog_order(by_currency)]
        companies = platform[COMPANIES].count
        with_plan = sum(group.companies for group in groups)
        return BillingOverview(
            as_of=today,
            month_start=month_start,
            last_cut_on=platform[LAST_CUT_ON].day,
            currencies=groups,
            companies=CompanyCounts(
                total=companies,
                with_plan=with_plan,
                without_plan=companies - with_plan,
                overdue=sum(group.overdue_companies for group in groups),
                suspended=platform[SUSPENDED].count,
                in_trial=platform[IN_TRIAL].count,
            ),
        )

    def companies(
        self, *, search: str | None, status: str | None, overdue: bool | None, page: PageParams
    ) -> CompanyBillingList:
        """Empresas con su saldo (la de cargo vencido más antiguo primero): tres consultas por página."""
        today = business_today()
        suspended = None if status is None else status == "SUSPENDED"
        items, total = self.repo.company_page(
            today, search=search, suspended=suspended, overdue=overdue, offset=page.offset, limit=page.size
        )
        ids = [c.id for c in items]
        plans = self.repo.plans_by_ids(ids)
        balances = self.repo.balances(ids, today)
        rows = []
        for company in items:
            plan, balance = plans.get(company.id), balances[company.id]
            rows.append(
                CompanyBillingRow(
                    company_id=company.id,
                    name=company.name,
                    rfc=company.legacy_rfc,
                    active=company.active,
                    status=BillingStatus(company.billing_status),
                    currency=plan.currency if plan else balance.currency,
                    suspension_reason=SuspensionReason(company.suspension_reason)
                    if company.suspension_reason
                    else None,
                    plan=PlanSummary(
                        pricing_mode=PricingMode(plan.pricing_mode),
                        unit_price=plan.unit_price,
                        price_period=PricePeriod(plan.price_period),
                        interval_months=plan.interval_months,
                    )
                    if plan
                    else None,
                    outstanding=balance.outstanding,
                    overdue=balance.overdue,
                    credit=balance.credit,
                    open_charges=balance.open_charges,
                    oldest_due_on=balance.oldest_due_on,
                    next_cut_on=plan.next_cut_on if plan else None,
                    forecast_total=plan.forecast_total if plan else None,
                    last_payment_on=balance.last_payment_on,
                )
            )
        return CompanyBillingList.of(rows, total, page)

    # ---------- Comandos ----------

    def save_plan(
        self,
        company_id: int,
        data: BillingPlanIn,
        actor: str | None,
        *,
        commit: bool = True,
        prefix: str = "",
    ) -> None:
        """Crea o reemplaza el plan. Aplica desde el PRÓXIMO cargo: lo ya emitido no cambia y un periodo
        ya cortado nunca se vuelve a cobrar (el siguiente corte sale del último emitido). El pronóstico se
        refresca al momento. Con `commit=False` va dentro de la transacción de quien llama (alta de la
        empresa con su plan); `prefix` es dónde va el plan en ese cuerpo ("billing.") para marcar sus errores.

        El inicio del cobro puede estar hasta un año atrás (cobrar lo que ya se usó) o adelante (un contrato
        que empieza después); el de un plan que ya existe se conserva aunque ya tenga más de un año. La moneda
        se valida con la empresa bloqueada (`_check_currency`)."""
        self.ledger.lock(company_id)
        today = business_today()
        plan = self.repo.plan(company_id)
        self._check_currency(company_id, plan, data.currency, f"{prefix}currency")
        start = data.starts_on or today
        if start != (plan.starts_on if plan else None) and abs((start - today).days) > START_WINDOW_DAYS:
            raise UnprocessableError(
                code="PLAN_START_OUT_OF_RANGE",
                field=f"{prefix}starts_on",
            )
        columns, terms = _terms_in(data, start, prefix)
        last_cut = self.repo.last_cut(company_id)
        next_cut = terms.cut_after(last_cut) if last_cut else terms.first_cut()
        values = {
            **columns,
            "currency": data.currency,
            "grace_days": data.grace_days,
            "trial_ends_on": terms.trial_ends_on,
            "next_cut_on": next_cut,
            "updated_by": actor,
        }
        if plan is None:
            plan = self.repo.add_plan(BillingPlan(company_id=company_id, **values))
        else:
            for name, value in values.items():
                setattr(plan, name, value)
        self.db.flush()
        self.refresh_forecast(plan, today, self.repo.active_headcount([company_id])[company_id])
        if commit:
            self.db.commit()

    def _check_currency(self, company_id: int, plan: BillingPlan | None, currency: str, field: str) -> None:
        """La moneda del plan se cambia libremente hasta el primer cargo o pago de la empresa; desde ahí queda
        fija (422 `CURRENCY_LOCKED`): su historial y su saldo están en ella y jamás se convierten. Una moneda
        nueva debe estar activa en el catálogo (422 `CURRENCY_INVALID`); la que ya tiene la empresa se conserva
        aunque se haya desactivado. Sin cambio de moneda no consulta nada."""
        if plan is not None and currency == plan.currency:
            return
        locked = self.repo.movement_currency(company_id)
        if locked is not None and currency != locked:
            raise UnprocessableError(code="CURRENCY_LOCKED", params={"currency": locked}, field=field)
        if locked is None and not get_catalogs().is_active("currencies", currency):
            raise _currency_error("CURRENCY_INVALID", field)

    def _payment_currency(self, company_id: int, data: PaymentInput) -> None:
        """El pago va en la moneda de la empresa (la de su plan o, sin plan, la de sus movimientos): el dinero
        de una moneda nunca paga una deuda en otra (422 `CURRENCY_MISMATCH`). El primer movimiento de una
        empresa sin plan fija su moneda (debe estar activa). El monto debe poder escribirse en ella (422
        `AMOUNT_DECIMALS`)."""
        plan = self.repo.plan(company_id)
        expected = plan.currency if plan else self.repo.movement_currency(company_id)
        if expected is None and not get_catalogs().is_active("currencies", data.currency):
            raise _currency_error("CURRENCY_INVALID", "currency")
        if expected is not None and data.currency != expected:
            raise UnprocessableError(code="CURRENCY_MISMATCH", params={"currency": expected}, field="currency")
        decimals = currency_decimals(data.currency)
        if not representable(data.amount, decimals):
            raise _decimals_error(data.currency, decimals, "amount")

    def refresh_forecast(
        self, plan: BillingPlan, today: date, active_now: Headcount, at: datetime | None = None
    ) -> None:
        """Pronóstico (con IVA) del cargo del periodo en curso, guardado en el plan para el resumen. `active_now`:
        empleados y validadores activos hoy (se cobran los dos). `at`: el instante con que se marca (el mantenimiento
        usa el de su vuelta)."""
        plan.forecast_total = self.ledger.estimate(plan, today, active_now).charge.totals.total
        plan.forecast_at = at or datetime.now(UTC)

    def register_payment(
        self, company_id: int, data: PaymentInput, receipt: Receipt | None, actor: str
    ) -> PaymentResult:
        """Registra un pago confirmado por el ADMIN, en la moneda de la empresa, y lo aplica al cargo abierto más
        antiguo (lo que sobra queda a favor). Si la empresa estaba suspendida por falta de pago y ya no debe nada
        vencido, se reactiva en la misma operación."""
        company = self.ledger.lock(company_id)
        today = business_today()
        if data.paid_on > today:
            raise UnprocessableError(code="PAYMENT_DATE_IN_FUTURE", field="paid_on")
        if not get_catalogs().is_active("payment_methods", data.method):
            raise UnprocessableError(code="PAYMENT_METHOD_INVALID", field="method")
        self._payment_currency(company_id, data)
        payment = self.repo.add_payment(
            Payment(
                company_id=company_id,
                currency=data.currency,
                amount=data.amount,
                paid_on=data.paid_on,
                method=data.method,
                reference=data.reference,
                note=data.note,
                status=PaymentStatus.CONFIRMED,
                applied=ZERO,
                receipt_name=receipt.file_name if receipt else None,
                receipt_type=receipt.content_type if receipt else None,
                receipt_size=len(receipt.data) if receipt else None,
                recorded_by=actor,
            )
        )
        if receipt is not None:  # el comprobante va CIFRADO al bucket, nunca a la BD (sin bucket: 503)
            image_storage.store(self.db, PAYMENT_RECEIPTS, payment, receipt.data)
        applied = self.ledger.settle(company_id)
        reactivated = self.ledger.reactivate_if_paid(company, today)
        self.db.commit()
        self.db.refresh(payment)
        return PaymentResult(
            payment=payment_read(payment),
            applied=[
                AppliedRead(charge_id=a.charge.id, sequence=a.charge.sequence, cut_on=a.charge.cut_on, amount=a.amount)
                for a in applied
                if a.payment.id == payment.id
            ],
            account=self._account(company),
            reactivated=reactivated,
        )

    def void_payment(self, company_id: int, payment_id: int, reason: str, actor: str) -> PaymentVoidResult:
        payment = self.ledger.void_payment(company_id, payment_id, reason, actor)
        self.db.commit()
        return PaymentVoidResult(payment=payment_read(payment), account=self.account(company_id))

    def void_charge(self, company_id: int, charge_id: int, reason: str, actor: str) -> ChargeVoidResult:
        charge = self.ledger.void_charge(company_id, charge_id, reason, actor)
        company = self.ledger.lock(company_id)
        self.ledger.reactivate_if_paid(company, business_today())
        self.db.commit()
        return ChargeVoidResult(charge=self._charge_detail(charge), account=self._account(company))

    def suspend(self, company_id: int, reason: str, actor: str) -> BillingAccount:
        """Suspensión manual: cierra al momento todas las sesiones de la empresa y nadie de ella entra ni
        opera hasta que se reactive."""
        company = self.ledger.lock(company_id)
        if company.suspended:
            raise ConflictError(code="COMPANY_ALREADY_SUSPENDED")
        self.ledger.suspend(company, SuspensionReason.MANUAL, reason, actor)
        self.db.commit()
        return self._account(company)

    def reactivate(self, company_id: int, note: str | None, actor: str) -> BillingAccount:
        """Reactivación manual: el acceso vuelve al momento. Si aún debe algo vencido, tiene otro periodo de
        gracia completo antes de que la suspensión automática vuelva a aplicar."""
        company = self.ledger.lock(company_id)
        if not company.suspended:
            raise ConflictError(code="COMPANY_NOT_SUSPENDED")
        self.ledger.lift(company)
        plan = self.repo.plan(company_id)
        if plan is not None:
            plan.grace_until = business_today() + timedelta(days=plan.grace_days)
        logger.info("Empresa %s reactivada por %s%s", company_id, actor, f": {note}" if note else "")
        self.db.commit()
        return self._account(company)
