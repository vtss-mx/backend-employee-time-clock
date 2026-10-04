"""Reglas del cobro a cada empresa: funciones puras (sin BD), en la hora del negocio.

- **Corte:** el último día de cada mes. Cada `interval_months` meses (1 mensual, 3 trimestral, 12
  anual...) se emite un cargo que cubre esos meses; el primer periodo empieza el mes en que empieza
  el cobro.
- **Quién cuenta:** los empleados activos, día por día (prorrateo): quien entra o sale a mitad de mes
  paga solo sus días. Validadores y administradores no cuentan. Con precio fijo (`FLAT`) cuenta la
  empresa (1) cada día cobrable.
- **Precio** por día, por mes (se divide entre los días de ese mes) o por año (entre los del año).
- **Demo:** los días de prueba y los anteriores al inicio del cobro no se cobran.
- **Descuento:** porcentaje del subtotal o monto fijo (nunca mayor que el subtotal); en todos los
  cargos, solo en los primeros N o cada N cargos.
- **IVA:** sobre el subtotal con descuento, a la tasa de la empresa (16 % por omisión; 0 permitido).
- **Redondeo:** a centavos, mitad hacia arriba, al cerrar cada importe (nunca en los pasos intermedios).
- **Pagos:** se aplican al cargo abierto más antiguo primero; lo que sobra queda a favor.
"""

from calendar import isleap, monthrange
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from app.models.enums import DiscountRecurrence, DiscountType, PricePeriod, PricingMode

CENT = Decimal("0.01")
ZERO = Decimal("0")
HUNDRED = Decimal("100")


def money(value: Decimal) -> Decimal:
    """Importe a centavos (mitad hacia arriba)."""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------- periodos


def month_end(day: date) -> date:
    return day.replace(day=monthrange(day.year, day.month)[1])


def add_months(day: date, months: int) -> date:
    """El primer día del mes que está `months` meses después del de `day`."""
    index = day.year * 12 + day.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def cut_dates(starts_on: date, interval_months: int, until: date) -> list[date]:
    """Cortes (fin de mes) en que toca emitir un cargo, desde el inicio del cobro hasta `until`."""
    cuts = []
    cut = month_end(add_months(starts_on, interval_months - 1))
    while cut <= until:
        cuts.append(cut)
        cut = month_end(add_months(cut, interval_months))
    return cuts


def period_of(cut: date, interval_months: int) -> tuple[date, date]:
    """Los días que cubre el cargo de ese corte: del primero del mes inicial al corte."""
    return add_months(cut, 1 - interval_months), cut


def days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def billable_days(start: date, end: date, *, starts_on: date, trial_ends_on: date | None) -> list[date]:
    """Días cobrables del periodo: desde el inicio del cobro y después de la demo."""
    first = max(start, starts_on, (trial_ends_on + timedelta(days=1)) if trial_ends_on else start)
    return days(first, end) if first <= end else []


def months_of(day_list: Iterable[date]) -> dict[date, list[date]]:
    """Los días agrupados por mes (clave: el primero del mes), en orden: una línea del cargo por mes."""
    grouped: dict[date, list[date]] = {}
    for day in sorted(day_list):
        grouped.setdefault(day.replace(day=1), []).append(day)
    return grouped


# ---------------------------------------------------------------- importes


@dataclass(frozen=True)
class Pricing:
    mode: PricingMode
    unit_price: Decimal
    period: PricePeriod


def day_rate(pricing: Pricing, day: date) -> Decimal:
    """Lo que cuesta un día (de un empleado o de la empresa) sin redondear."""
    if pricing.period == PricePeriod.DAY:
        return pricing.unit_price
    if pricing.period == PricePeriod.MONTH:
        return pricing.unit_price / monthrange(day.year, day.month)[1]
    return pricing.unit_price / (366 if isleap(day.year) else 365)


@dataclass(frozen=True)
class Line:
    """Una línea del cargo (un mes): cuántas unidades-día y su importe."""

    month: date
    #: Días-empleado (PER_USER) o días cobrables (FLAT).
    units: int
    amount: Decimal


def line_for(pricing: Pricing, month: date, month_days: Sequence[date], headcount: Mapping[date, int]) -> Line:
    """Importe de un mes: la tarifa de cada día por los empleados activos ese día (o 1 si es fijo)."""
    per_day = [(day, headcount.get(day, 0) if pricing.mode == PricingMode.PER_USER else 1) for day in month_days]
    amount = sum((day_rate(pricing, day) * units for day, units in per_day), ZERO)
    return Line(month=month, units=sum(units for _, units in per_day), amount=money(amount))


@dataclass(frozen=True)
class Discount:
    type: DiscountType
    value: Decimal
    recurrence: DiscountRecurrence
    #: N de "primeros N" o "cada N" (sin uso con ALWAYS).
    periods: int | None = None


def discount_applies(discount: Discount, sequence: int) -> bool:
    """¿Aplica al cargo número `sequence` (1 = el primero de la empresa)?"""
    if discount.recurrence == DiscountRecurrence.ALWAYS:
        return True
    periods = max(discount.periods or 1, 1)
    if discount.recurrence == DiscountRecurrence.FIRST:
        return sequence <= periods
    return sequence % periods == 0


def discount_amount(discount: Discount | None, subtotal: Decimal, sequence: int) -> Decimal:
    if discount is None or not discount_applies(discount, sequence):
        return ZERO
    if discount.type == DiscountType.PERCENT:
        return money(subtotal * min(discount.value, HUNDRED) / HUNDRED)
    return min(money(discount.value), subtotal)


@dataclass(frozen=True)
class Totals:
    subtotal: Decimal
    discount: Decimal
    taxable: Decimal
    tax: Decimal
    total: Decimal


def totals(subtotal: Decimal, discount: Decimal, tax_rate: Decimal) -> Totals:
    """IVA sobre lo que queda después del descuento (`tax_rate` en porcentaje: 16 = 16 %)."""
    taxable = subtotal - discount
    tax = money(taxable * tax_rate / HUNDRED)
    return Totals(subtotal=subtotal, discount=discount, taxable=taxable, tax=tax, total=taxable + tax)


# ---------------------------------------------------------------- pagos y vencimientos


def allocate(amount: Decimal, open_charges: Sequence[tuple[int, Decimal]]) -> tuple[list[tuple[int, Decimal]], Decimal]:
    """Reparte un pago entre los cargos abiertos (id, saldo) del más antiguo al más nuevo. Devuelve
    lo aplicado a cada uno y lo que sobra (saldo a favor)."""
    left = amount
    applied = []
    for charge_id, balance in open_charges:
        if left <= ZERO:
            break
        part = min(left, balance)
        if part > ZERO:
            applied.append((charge_id, part))
            left -= part
    return applied, left


def suspension_due(due_on: date, grace_days: int, today: date) -> bool:
    """¿Ya pasó la gracia de un cargo vencido sin pagar? (se suspende al día siguiente de la gracia)."""
    return today > due_on + timedelta(days=grace_days)
