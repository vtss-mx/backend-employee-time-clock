"""Cálculo de un cargo, su vista previa y la estimación del periodo en curso: funciones puras (sin BD)
que ARMAN las reglas de `billing_rules` (cortes, prorrateo, demo, descuento, IVA). Ninguna regla se
repite aquí; este módulo solo decide qué días y qué plantilla se le dan a cada regla.

Las usan la emisión de cargos del mantenimiento, la vista previa del alta de empresa (lo que costará el
primer cargo con la plantilla esperada) y la estimación en vivo del periodo en curso.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from app.models.enums import DiscountRecurrence, DiscountType, PricePeriod, PricingMode
from app.services.billing_rules import (
    CENTS,
    ZERO,
    Discount,
    Line,
    Pricing,
    Totals,
    add_months,
    billable_days,
    discount_amount,
    line_for,
    money,
    month_end,
    months_of,
    period_of,
    totals,
)


@dataclass(frozen=True)
class PlanTerms:
    """Lo que el cálculo necesita del plan de una empresa."""

    pricing: Pricing
    interval_months: int
    starts_on: date
    trial_days: int
    discount: Discount | None
    tax_rate: Decimal

    @property
    def decimals(self) -> int:
        """Decimales de la moneda del plan (van con el precio): cada importe se redondea a ellos."""
        return self.pricing.decimals

    @property
    def trial_ends_on(self) -> date | None:
        """Último día de la demo (sin demo: None)."""
        return self.starts_on + timedelta(days=self.trial_days - 1) if self.trial_days > 0 else None

    def first_cut(self) -> date:
        """El primer corte: fin del mes en que termina el primer periodo (el mes del inicio cuenta)."""
        return month_end(add_months(self.starts_on, self.interval_months - 1))

    def cut_after(self, cut: date) -> date:
        return month_end(add_months(cut, self.interval_months))

    def billable(self, cut: date) -> list[date]:
        """Días cobrables del periodo que cierra en `cut` (desde el inicio del cobro y después de la demo)."""
        start, end = period_of(cut, self.interval_months)
        return billable_days(start, end, starts_on=self.starts_on, trial_ends_on=self.trial_ends_on)


def plan_terms(
    *,
    pricing_mode: str,
    unit_price: Decimal,
    price_period: str,
    interval_months: int,
    starts_on: date,
    trial_days: int,
    discount_type: str | None,
    discount_value: Decimal | None,
    discount_recurrence: str | None,
    discount_periods: int | None,
    tax_rate: Decimal,
    decimals: int = CENTS,
) -> PlanTerms:
    """Los términos de un plan (columnas del modelo o campos de la API) como objetos de las reglas; `decimals`
    son los de su moneda (catálogo `currencies`)."""
    discount = (
        Discount(DiscountType(discount_type), discount_value, DiscountRecurrence(discount_recurrence), discount_periods)
        if discount_type and discount_value is not None and discount_recurrence
        else None
    )
    return PlanTerms(
        pricing=Pricing(PricingMode(pricing_mode), unit_price, PricePeriod(price_period), decimals),
        interval_months=interval_months,
        starts_on=starts_on,
        trial_days=trial_days,
        discount=discount,
        tax_rate=tax_rate,
    )


@dataclass(frozen=True)
class ChargeCalc:
    """Un cargo calculado: su periodo, sus líneas por mes y sus importes."""

    sequence: int
    cut_on: date
    period_start: date
    period_end: date
    days: tuple[date, ...]
    lines: tuple[Line, ...]
    totals: Totals

    @property
    def billable_days(self) -> int:
        return len(self.days)

    @property
    def units(self) -> int:
        return sum(line.units for line in self.lines)

    @property
    def validator_units(self) -> int | None:
        """De los días-persona, cuántos fueron de validadores (None: monto fijo o sin el desglose)."""
        return _validator_units(self.lines)

    def days_in(self, month: date) -> int:
        """Días cobrables del periodo que caen en ese mes."""
        return sum(1 for day in self.days if day.replace(day=1) == month)


def _validator_units(lines: Sequence[Line]) -> int | None:
    """La suma del desglose de varias líneas (None si alguna no lo tiene)."""
    parts = [line.validator_units for line in lines]
    return None if any(part is None for part in parts) else sum(part or 0 for part in parts)


def compute_charge(
    terms: PlanTerms,
    cut: date,
    sequence: int,
    headcount: Mapping[date, int],
    validators: Mapping[date, int] | None = None,
) -> ChargeCalc:
    """El cargo del periodo que cierra en `cut` con la plantilla diaria dada (empleados + validadores activos por
    día) y, para el desglose de los días-persona, los validadores de cada día (no cambian el importe)."""
    start, end = period_of(cut, terms.interval_months)
    days = terms.billable(cut)
    lines = tuple(
        line_for(terms.pricing, month, month_days, headcount, validators)
        for month, month_days in months_of(days).items()
    )
    subtotal = sum((line.amount for line in lines), ZERO)
    discount = discount_amount(terms.discount, subtotal, sequence, terms.decimals)
    return ChargeCalc(
        sequence=sequence,
        cut_on=cut,
        period_start=start,
        period_end=end,
        days=tuple(days),
        lines=lines,
        totals=totals(subtotal, discount, terms.tax_rate, terms.decimals),
    )


def constant(terms: PlanTerms, cut: date, count: int) -> dict[date, int]:
    """La misma plantilla todos los días cobrables del periodo (vista previa: "con N empleados y M validadores")."""
    return dict.fromkeys(terms.billable(cut), count)


@dataclass(frozen=True)
class Preview:
    """Lo que costará el plan con una plantilla fija: el primer cargo (si el primer periodo no es todo
    demo) y un periodo completo después de la demo."""

    trial_ends_on: date | None
    first: ChargeCalc | None
    recurring: ChargeCalc
    monthly_equivalent: Decimal


def preview(terms: PlanTerms, headcount: int, validators: int = 0) -> Preview:
    """`headcount`: lo que se cobra por día (empleados + validadores activos esperados); `validators`, cuántos de ellos
    son validadores (solo el desglose de los días-persona)."""
    cut = terms.first_cut()
    first = compute_charge(terms, cut, 1, constant(terms, cut, headcount), constant(terms, cut, validators))
    issued = 1 if first.billable_days else 0
    # Un periodo completo: el primero que empieza después de la demo (o el siguiente al primero).
    cut = terms.cut_after(cut)
    trial_end = terms.trial_ends_on
    # Termina: cada vuelta avanza un periodo y la demo dura a lo más un año.
    while trial_end is not None and period_of(cut, terms.interval_months)[0] <= trial_end:
        issued += 1 if terms.billable(cut) else 0
        cut = terms.cut_after(cut)
    recurring = compute_charge(
        terms, cut, issued + 1, constant(terms, cut, headcount), constant(terms, cut, validators)
    )
    monthly = money(recurring.totals.total / terms.interval_months, terms.decimals)
    return Preview(trial_end, first if first.billable_days else None, recurring, monthly)


@dataclass(frozen=True)
class Estimate:
    """El periodo en curso (aún sin cargo): lo devengado hasta hoy y el pronóstico del periodo completo
    con la plantilla de hoy en los días que faltan."""

    charge: ChargeCalc
    as_of: date
    days_total: int
    days_elapsed: int
    #: Lo que se cobra por día con la plantilla de hoy: empleados + validadores activos (un validador cuenta como un
    #: empleado).
    active_now: int
    accrued_units: int
    accrued_subtotal: Decimal
    in_trial: bool
    #: De los días-persona devengados, cuántos fueron de validadores (None: monto fijo).
    accrued_validator_units: int | None = None

    def projected(self, month: date) -> bool:
        """¿La línea de ese mes incluye días que aún no pasan?"""
        return month_end(month) > self.as_of


def _daily(terms: PlanTerms, cut: date, today: date, closed: Mapping[date, int], now: int) -> dict[date, int]:
    """Cada día cobrable del periodo: lo cerrado si ya pasó y está cerrado; si no (hoy y lo que falta), lo de hoy."""
    return {day: closed.get(day, now) if day < today else now for day in terms.billable(cut)}


def estimate(
    terms: PlanTerms,
    cut: date,
    sequence: int,
    today: date,
    closed: Mapping[date, int],
    active_now: int,
    closed_validators: Mapping[date, int] | None = None,
    validators_now: int = 0,
) -> Estimate:
    """`closed`: plantilla de los días ya cerrados; los días que faltan (y los aún sin cerrar, como hoy)
    usan la plantilla de hoy (`active_now`: empleados + validadores activos). `closed_validators` y `validators_now`
    son cuántos de ellos eran validadores: solo el desglose de los días-persona."""
    headcount = _daily(terms, cut, today, closed, active_now)
    validators = _daily(terms, cut, today, closed_validators or {}, validators_now)
    charge = compute_charge(terms, cut, sequence, headcount, validators)
    elapsed = tuple(day for day in charge.days if day <= today)
    accrued = [
        line_for(terms.pricing, month, days, headcount, validators) for month, days in months_of(elapsed).items()
    ]
    trial_end = terms.trial_ends_on
    return Estimate(
        charge=charge,
        as_of=today,
        days_total=(charge.period_end - charge.period_start).days + 1,
        days_elapsed=max(
            0, min((today - charge.period_start).days + 1, (charge.period_end - charge.period_start).days + 1)
        ),
        active_now=active_now,
        accrued_units=sum(line.units for line in accrued),
        accrued_subtotal=sum((line.amount for line in accrued), ZERO),
        in_trial=trial_end is not None and terms.starts_on <= today <= trial_end,
        accrued_validator_units=_validator_units(accrued),
    )
