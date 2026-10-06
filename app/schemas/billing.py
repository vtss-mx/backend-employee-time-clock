"""Cobranza (solo el ADMIN): plan de cobro de cada empresa, vista previa, estimación del periodo en
curso, cargos, pagos, estado de cuenta, suspensión y resumen de la plataforma.

Dinero en `Decimal` con 2 decimales (en JSON viaja como texto: "1234.50", sin errores de redondeo de
punto flotante). Precios SIN IVA; `tax` y `total` ya lo incluyen. Cada importe está en la moneda de su
empresa (la de su plan; código ISO 4217 del catálogo `currencies`) y cada respuesta dice cuál (`currency`):
el cliente nunca supone una moneda ni suma importes de monedas distintas.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from app.i18n import LocalizedValueError, StoredText
from app.models.company import VALIDATORS_MAX
from app.models.enums import (
    BillingStatus,
    ChargeStatus,
    Currency,
    DiscountRecurrence,
    DiscountType,
    PaymentStatus,
    PricePeriod,
    PricingMode,
    SuspensionReason,
)
from app.schemas.common import Page

#: Precio unitario máximo (sin IVA) y monto máximo de un pago.
MAX_PRICE = Decimal("1000000")
MAX_PAYMENT = Decimal("100000000")

CENT = Decimal("0.01")
#: Forma de un código de moneda ISO 4217 (que exista y esté activo lo decide el catálogo `currencies`).
CURRENCY_CODE = r"^[A-Z]{3}$"


def _cents(value: Decimal) -> Decimal:
    """Todo importe sale con sus dos decimales ("0.00", "1500.00"): el cliente nunca recibe "0" o "1.5"."""
    return value.quantize(CENT)


#: Importe de salida: Decimal a centavos (en JSON, texto con dos decimales).
Money = Annotated[Decimal, AfterValidator(_cents)]


class DiscountIn(BaseModel):
    """Descuento del plan: porcentaje del subtotal (hasta 100) o monto fijo, en todos los cargos, en
    los primeros N o cada N."""

    type: DiscountType
    value: Decimal = Field(gt=0, le=MAX_PRICE, max_digits=12, decimal_places=2)
    recurrence: DiscountRecurrence = DiscountRecurrence.ALWAYS
    periods: int | None = Field(
        default=None, ge=1, le=120, validate_default=True, description="N de 'primeros N' o 'cada N' cargos"
    )

    @field_validator("value")
    @classmethod
    def _percent_cap(cls, value: Decimal, info: ValidationInfo) -> Decimal:
        if info.data.get("type") == DiscountType.PERCENT and value > 100:
            raise LocalizedValueError("DISCOUNT_PERCENT_MAX")
        return value

    @field_validator("periods")
    @classmethod
    def _periods(cls, value: int | None, info: ValidationInfo) -> int | None:
        recurrence = info.data.get("recurrence")
        if recurrence == DiscountRecurrence.ALWAYS:
            return None  # en todos los cargos: no lleva N
        if value is None:
            raise LocalizedValueError("DISCOUNT_CHARGES_REQUIRED")
        return value


class BillingPlanIn(BaseModel):
    """Plan de cobro de una empresa. Precios SIN IVA. Por omisión: por empleado activo, mensual, desde
    hoy, sin demo, IVA 16 % y 10 días de gracia."""

    pricing_mode: PricingMode = PricingMode.PER_USER
    unit_price: Decimal = Field(
        ge=0, le=MAX_PRICE, max_digits=12, decimal_places=2, description="Por empleado activo o de la empresa"
    )
    price_period: PricePeriod = PricePeriod.MONTH
    interval_months: int = Field(default=1, ge=1, le=12, description="Un cargo cada N meses (corte a fin de mes)")
    starts_on: date | None = Field(default=None, description="Inicio del cobro (vacío = hoy)")
    trial_days: int = Field(default=0, ge=0, le=365, description="Días de demo sin cobro")
    discount: DiscountIn | None = None
    tax_rate: Decimal = Field(
        default=Decimal("16"),
        ge=0,
        le=100,
        max_digits=5,
        decimal_places=2,
        description="IVA de la empresa (no depende de la moneda; un cliente del extranjero suele llevar 0)",
    )
    grace_days: int = Field(default=10, ge=0, le=90, description="Días después del vencimiento antes de suspender")
    currency: str = Field(
        default=Currency.MXN.value,
        pattern=CURRENCY_CODE,
        description="Moneda de todo el cobro (catálogo currencies); fija desde el primer cargo o pago",
    )


class PlanPreviewIn(BaseModel):
    plan: BillingPlanIn
    employees: int = Field(default=0, ge=0, le=1_000_000, description="Empleados activos esperados")
    #: Cada validador activo cuenta como un empleado en el cobro por empleado activo (decisión del dueño).
    validators: int = Field(default=0, ge=0, le=VALIDATORS_MAX, description="Validadores activos esperados")


class ReasonIn(BaseModel):
    """Motivo obligatorio (anular un cargo o un pago, suspender una empresa)."""

    reason: str = Field(min_length=5, max_length=300)

    @field_validator("reason")
    @classmethod
    def _clean(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if len(cleaned) < 5:
            raise LocalizedValueError("BILLING_REASON_REQUIRED")
        return cleaned


class ReactivateIn(BaseModel):
    note: str | None = Field(default=None, max_length=300)


# ---------------------------------------------------------------- lecturas


class DiscountRead(BaseModel):
    type: DiscountType
    value: Money
    recurrence: DiscountRecurrence
    periods: int | None = None


class BillingPlanRead(BaseModel):
    pricing_mode: PricingMode
    unit_price: Money
    price_period: PricePeriod
    interval_months: int
    starts_on: date
    trial_days: int
    discount: DiscountRead | None = None
    tax_rate: Money
    grace_days: int
    currency: str
    trial_ends_on: date | None = None
    next_cut_on: date
    forecast_total: Money | None = None
    forecast_at: datetime | None = None
    updated_at: datetime


class SuspensionRead(BaseModel):
    reason: SuspensionReason
    #: El motivo que escribió el ADMIN o, si fue automática, la nota del sistema en el idioma de quien la lee.
    note: StoredText = None
    suspended_at: datetime
    suspended_by: str | None = None


class BalanceRead(BaseModel):
    charged: Money
    paid: Money
    outstanding: Money
    overdue: Money
    credit: Money
    #: outstanding - credit (positivo: debe; negativo: a favor).
    balance: Money
    open_charges: int
    overdue_charges: int
    oldest_due_on: date | None = None
    #: Día en que se suspendería sola si no paga (None si no aplica o ya está suspendida).
    suspends_on: date | None = None
    last_payment_on: date | None = None


class BillingAccount(BaseModel):
    company_id: int
    company_name: str
    company_active: bool
    status: BillingStatus
    #: Moneda de la cuenta: la del plan (o, sin plan, la de sus movimientos); None sin plan ni movimientos.
    currency: str | None = None
    #: Ya tiene cargos o pagos: la moneda no se puede cambiar (422 `CURRENCY_LOCKED`).
    currency_locked: bool = False
    suspension: SuspensionRead | None = None
    plan: BillingPlanRead | None = None
    balance: BalanceRead
    grace_until: date | None = None


class ChargeLineRead(BaseModel):
    month: date
    days: int
    units: int
    #: De los días-persona (`units`, cobro por empleado activo), cuántos fueron de validadores; los demás son de
    #: empleados. None: monto fijo o un cargo emitido antes de guardar el desglose (migración 0072).
    validator_units: int | None = None
    amount: Money


class ChargePreviewRead(BaseModel):
    sequence: int
    cut_on: date
    period_start: date
    period_end: date
    billable_days: int
    units: int
    #: De los días-persona (`units`, cobro por empleado activo), cuántos fueron de validadores; los demás son de
    #: empleados. None: monto fijo o un cargo emitido antes de guardar el desglose (migración 0072).
    validator_units: int | None = None
    lines: list[ChargeLineRead]
    subtotal: Money
    discount: Money
    tax: Money
    total: Money


class PlanPreview(BaseModel):
    #: Moneda de todos los importes de la vista previa (la del plan).
    currency: str
    trial_ends_on: date | None = None
    #: None: todo el primer periodo es demo (no se emite cargo).
    first: ChargePreviewRead | None = None
    recurring: ChargePreviewRead
    monthly_equivalent: Money


class EstimateLine(ChargeLineRead):
    projected: bool


class AccruedRead(BaseModel):
    units: int
    #: De los días-persona (`units`, cobro por empleado activo), cuántos fueron de validadores; los demás son de
    #: empleados. None: monto fijo o un cargo emitido antes de guardar el desglose (migración 0072).
    validator_units: int | None = None
    subtotal: Money


class ForecastRead(BaseModel):
    units: int
    #: De los días-persona (`units`, cobro por empleado activo), cuántos fueron de validadores; los demás son de
    #: empleados. None: monto fijo o un cargo emitido antes de guardar el desglose (migración 0072).
    validator_units: int | None = None
    subtotal: Money
    discount: Money
    tax: Money
    total: Money


class PeriodEstimate(BaseModel):
    currency: str
    sequence: int
    cut_on: date
    period_start: date
    period_end: date
    as_of: date
    days_total: int
    days_elapsed: int
    in_trial: bool
    trial_ends_on: date | None = None
    #: La plantilla de hoy: lo que falta del periodo se calcula con empleados + validadores activos.
    active_employees: int
    active_validators: int = 0
    accrued: AccruedRead
    forecast: ForecastRead
    lines: list[EstimateLine]


class ChargeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sequence: int
    currency: str
    cut_on: date
    period_start: date
    period_end: date
    issued_on: date
    due_on: date
    billable_days: int
    units: int
    #: De los días-persona (`units`, cobro por empleado activo), cuántos fueron de validadores; los demás son de
    #: empleados. None: monto fijo o un cargo emitido antes de guardar el desglose (migración 0072).
    validator_units: int | None = None
    subtotal: Money
    discount: Money
    tax: Money
    total: Money
    paid: Money
    balance: Money = Decimal("0")
    status: ChargeStatus
    overdue: bool = False
    voided_at: datetime | None = None
    void_reason: str | None = None


class ChargeList(Page[ChargeRead]):
    """Cargos de una empresa (el corte más reciente primero)."""


class AllocationRead(BaseModel):
    payment_id: int
    paid_on: date
    reference: str | None = None
    amount: Money


class ChargeDetail(ChargeRead):
    pricing_mode: PricingMode
    unit_price: Money
    price_period: PricePeriod
    tax_rate: Money
    lines: list[ChargeLineRead]
    allocations: list[AllocationRead]
    voided_by: str | None = None


class ReceiptInfo(BaseModel):
    file_name: str
    content_type: str
    size: int


class PaymentRead(BaseModel):
    id: int
    currency: str
    amount: Money
    paid_on: date
    method: str
    reference: str | None = None
    note: str | None = None
    status: PaymentStatus
    applied: Money
    unapplied: Money
    receipt: ReceiptInfo | None = None
    recorded_by: str | None = None
    created_at: datetime
    voided_at: datetime | None = None
    void_reason: str | None = None
    voided_by: str | None = None


class PaymentList(Page[PaymentRead]):
    """Pagos de una empresa (el más reciente primero)."""


class AppliedRead(BaseModel):
    charge_id: int
    sequence: int
    cut_on: date
    amount: Money


class PaymentResult(BaseModel):
    payment: PaymentRead
    applied: list[AppliedRead]
    account: BillingAccount
    #: Estaba suspendida por falta de pago y este pago la reactivó.
    reactivated: bool = False


class PaymentVoidResult(BaseModel):
    payment: PaymentRead
    account: BillingAccount


class ChargeVoidResult(BaseModel):
    charge: ChargeDetail
    account: BillingAccount


class ReceiptFile(ReceiptInfo):
    #: Contenido en base64 (el contrato único de respuesta es JSON).
    data: str


class StatementEntry(BaseModel):
    date: date
    kind: str  # CHARGE | PAYMENT
    id: int
    description: str
    currency: str
    debit: Money
    credit: Money
    #: Saldo acumulado tras este movimiento (positivo: debe).
    balance: Money


class StatementList(Page[StatementEntry]):
    """Estado de cuenta (el movimiento más reciente primero)."""


class CompanyCounts(BaseModel):
    total: int
    with_plan: int
    without_plan: int
    overdue: int
    suspended: int
    in_trial: int


class CurrencyTotals(BaseModel):
    """El dinero de la plataforma en UNA moneda: nunca se suman importes de monedas distintas ni se
    convierten (sin tipo de cambio)."""

    currency: str
    #: Empresas con plan en esta moneda.
    companies: int
    billed_month: Money
    collected_month: Money
    outstanding: Money
    overdue: Money
    #: Empresas con saldo vencido en esta moneda.
    overdue_companies: int
    credit: Money
    #: Suma de los pronósticos de los planes en esta moneda (periodo en curso, con IVA).
    forecast: Money
    #: Cargos de esta moneda en el último corte de la plataforma y su total.
    last_cut_charges: int
    last_cut_total: Money


class BillingOverview(BaseModel):
    as_of: date
    month_start: date
    #: El corte más reciente de la plataforma (None: aún no se emite ningún cargo).
    last_cut_on: date | None = None
    #: Una entrada por moneda en uso (con planes o movimientos), en el orden del catálogo.
    currencies: list[CurrencyTotals]
    companies: CompanyCounts


class PlanSummary(BaseModel):
    pricing_mode: PricingMode
    unit_price: Money
    price_period: PricePeriod
    interval_months: int


class CompanyBillingRow(BaseModel):
    company_id: int
    name: str
    rfc: str | None = None
    active: bool
    status: BillingStatus
    #: Moneda de sus importes (None: sin plan ni movimientos; todo en cero).
    currency: str | None = None
    suspension_reason: SuspensionReason | None = None
    plan: PlanSummary | None = None
    outstanding: Money
    overdue: Money
    credit: Money
    open_charges: int
    oldest_due_on: date | None = None
    next_cut_on: date | None = None
    forecast_total: Money | None = None
    last_payment_on: date | None = None


class CompanyBillingList(Page[CompanyBillingRow]):
    """Empresas para la cobranza (la de cargo vencido más antiguo primero)."""
