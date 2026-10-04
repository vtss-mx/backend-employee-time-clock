"""Reglas puras del cobro: periodos con corte a fin de mes, prorrateo por día, demo, descuentos,
IVA, reparto de pagos y vencimientos."""

from datetime import date
from decimal import Decimal

from app.models.enums import DiscountRecurrence, DiscountType, PricePeriod, PricingMode
from app.services.billing_rules import (
    Discount,
    Pricing,
    add_months,
    allocate,
    billable_days,
    cut_dates,
    day_rate,
    discount_amount,
    discount_applies,
    line_for,
    money,
    month_end,
    months_of,
    period_of,
    suspension_due,
    totals,
)

D = Decimal


def test_money_rounds_half_up_to_cents():
    assert money(D("0.005")) == D("0.01") and money(D("10.004")) == D("10.00") and money(D("2")) == D("2.00")


def test_months_and_cut_dates_at_month_end():
    assert month_end(date(2028, 2, 10)) == date(2028, 2, 29) and month_end(date(2027, 2, 1)) == date(2027, 2, 28)
    assert add_months(date(2026, 11, 15), 2) == date(2027, 1, 1)
    assert add_months(date(2026, 1, 31), -1) == date(2025, 12, 1)
    # Mensual desde el 15 de octubre: corte al final de cada mes.
    monthly = cut_dates(date(2026, 10, 15), 1, date(2026, 12, 31))
    assert monthly == [date(2026, 10, 31), date(2026, 11, 30), date(2026, 12, 31)]
    # Trimestral: un solo corte que cubre octubre, noviembre y diciembre.
    assert cut_dates(date(2026, 10, 15), 3, date(2027, 3, 30)) == [date(2026, 12, 31)]
    assert period_of(date(2026, 12, 31), 3) == (date(2026, 10, 1), date(2026, 12, 31))
    assert cut_dates(date(2026, 10, 15), 1, date(2026, 10, 30)) == []  # el mes aún no termina


def test_billable_days_skip_the_demo_and_the_days_before_billing_starts():
    oct_1, oct_31 = date(2026, 10, 1), date(2026, 10, 31)
    assert len(billable_days(oct_1, oct_31, starts_on=date(2026, 10, 15), trial_ends_on=None)) == 17
    assert billable_days(oct_1, oct_31, starts_on=oct_1, trial_ends_on=date(2026, 10, 20))[0] == date(2026, 10, 21)
    assert billable_days(oct_1, oct_31, starts_on=oct_1, trial_ends_on=date(2026, 11, 5)) == []  # todo es demo
    grouped = months_of([date(2026, 11, 2), date(2026, 10, 31), date(2026, 11, 1)])
    assert list(grouped) == [date(2026, 10, 1), date(2026, 11, 1)] and len(grouped[date(2026, 11, 1)]) == 2


def test_price_per_day_month_or_year():
    per_day = Pricing(PricingMode.PER_USER, D("10"), PricePeriod.DAY)
    per_month = Pricing(PricingMode.PER_USER, D("300"), PricePeriod.MONTH)
    per_year = Pricing(PricingMode.PER_USER, D("3660"), PricePeriod.YEAR)
    assert day_rate(per_day, date(2026, 2, 1)) == D("10")
    assert day_rate(per_month, date(2026, 11, 1)) == D("10")  # noviembre: 30 días
    assert day_rate(per_year, date(2028, 1, 1)) == D("10") and day_rate(per_year, date(2026, 1, 1)) == D("3660") / 365


def test_prorated_line_counts_active_employees_each_day():
    pricing = Pricing(PricingMode.PER_USER, D("300"), PricePeriod.MONTH)
    month = date(2026, 11, 1)
    november = [date(2026, 11, day) for day in range(1, 31)]
    # 10 empleados todo el mes y uno que entra el día 21 (10 días): 310 días-empleado × $10.
    headcount = {day: 10 + (1 if day.day >= 21 else 0) for day in november}
    line = line_for(pricing, month, november, headcount)
    assert (line.units, line.amount) == (310, D("3100.00"))
    assert line_for(pricing, month, november, {}).amount == D("0.00")  # sin empleados ese mes
    flat = line_for(Pricing(PricingMode.FLAT, D("900"), PricePeriod.MONTH), month, november[:15], headcount)
    assert (flat.units, flat.amount) == (15, D("450.00"))  # medio mes de la cuota fija


def test_discounts_always_first_or_every_n_charges():
    always = Discount(DiscountType.PERCENT, D("10"), DiscountRecurrence.ALWAYS)
    first_three = Discount(DiscountType.AMOUNT, D("500"), DiscountRecurrence.FIRST, 3)
    every_twelfth = Discount(DiscountType.PERCENT, D("100"), DiscountRecurrence.EVERY, 12)
    no_periods = Discount(DiscountType.PERCENT, D("5"), DiscountRecurrence.EVERY)
    assert discount_applies(always, 99) and discount_applies(first_three, 3) and not discount_applies(first_three, 4)
    assert discount_applies(every_twelfth, 24) and not discount_applies(every_twelfth, 13)
    assert discount_applies(no_periods, 7)  # sin N, cada cargo
    assert discount_amount(always, D("1000.00"), 1) == D("100.00")
    assert discount_amount(first_three, D("300.00"), 2) == D("300.00")  # nunca más que el subtotal
    assert discount_amount(first_three, D("900.00"), 4) == D("0")
    too_much = Discount(DiscountType.PERCENT, D("150"), DiscountRecurrence.ALWAYS)
    assert discount_amount(too_much, D("80.00"), 1) == D("80.00")  # el porcentaje se topa en 100
    assert discount_amount(None, D("100.00"), 1) == D("0")


def test_tax_applies_after_the_discount():
    result = totals(D("1000.00"), D("100.00"), D("16"))
    assert (result.taxable, result.tax, result.total) == (D("900.00"), D("144.00"), D("1044.00"))
    assert totals(D("10.00"), D("0"), D("0")).total == D("10.00")


def test_payments_settle_the_oldest_charges_first():
    applied, left = allocate(D("1500.00"), [(1, D("1000.00")), (2, D("0.00")), (3, D("800.00"))])
    assert applied == [(1, D("1000.00")), (3, D("500.00"))] and left == D("0.00")
    applied, left = allocate(D("300.00"), [(1, D("100.00"))])
    assert applied == [(1, D("100.00"))] and left == D("200.00")  # saldo a favor
    assert allocate(D("0"), [(1, D("10.00"))]) == ([], D("0"))


def test_suspension_after_the_grace_days():
    due = date(2026, 10, 31)
    assert not suspension_due(due, 5, date(2026, 11, 5)) and suspension_due(due, 5, date(2026, 11, 6))
