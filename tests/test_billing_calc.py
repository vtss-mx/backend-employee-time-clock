"""Cálculo de un cargo, su vista previa y la estimación del periodo en curso (funciones puras que arman
las reglas de `billing_rules`)."""

from datetime import date, timedelta
from decimal import Decimal

from app.services.billing_calc import compute_charge, constant, estimate, plan_terms, preview

D = Decimal


def terms(**overrides):
    values = {
        "pricing_mode": "PER_USER",
        "unit_price": D("300"),
        "price_period": "MONTH",
        "interval_months": 1,
        "starts_on": date(2026, 11, 1),
        "trial_days": 0,
        "discount_type": None,
        "discount_value": None,
        "discount_recurrence": None,
        "discount_periods": None,
        "tax_rate": D("16"),
    }
    return plan_terms(**{**values, **overrides})


def test_terms_cuts_trial_and_billable_days():
    monthly = terms(starts_on=date(2026, 11, 15), trial_days=10)
    assert monthly.trial_ends_on == date(2026, 11, 24)
    assert monthly.first_cut() == date(2026, 11, 30) and monthly.cut_after(date(2026, 11, 30)) == date(2026, 12, 31)
    assert monthly.billable(date(2026, 11, 30))[0] == date(2026, 11, 25)  # después de la demo
    assert terms().trial_ends_on is None
    quarterly = terms(interval_months=3, starts_on=date(2026, 11, 2))
    assert quarterly.first_cut() == date(2027, 1, 31) and len(quarterly.billable(date(2027, 1, 31))) == 91


def test_a_charge_prorates_each_month_and_applies_discount_and_tax():
    plan = terms(discount_type="PERCENT", discount_value=D("10"), discount_recurrence="FIRST", discount_periods=1)
    november = [date(2026, 11, d) for d in range(1, 31)]
    headcount = {day: 10 + (1 if day.day >= 21 else 0) for day in november}
    first = compute_charge(plan, date(2026, 11, 30), 1, headcount)
    assert (first.billable_days, first.units, first.totals.subtotal) == (30, 310, D("3100.00"))
    assert (first.totals.discount, first.totals.tax, first.totals.total) == (D("310.00"), D("446.40"), D("3236.40"))
    assert first.days_in(date(2026, 11, 1)) == 30 and first.days_in(date(2026, 12, 1)) == 0
    second = compute_charge(plan, date(2026, 11, 30), 2, headcount)
    assert second.totals.discount == D("0")  # solo el primer cargo
    flat = compute_charge(terms(pricing_mode="FLAT", unit_price=D("900")), date(2026, 11, 30), 1, {})
    assert (flat.units, flat.totals.subtotal) == (30, D("900.00"))


def test_preview_first_and_recurring_period():
    plan = terms(starts_on=date(2026, 11, 16))
    result = preview(plan, 10)
    assert (
        result.first is not None and result.first.billable_days == 15 and result.first.totals.subtotal == D("1500.00")
    )
    assert result.recurring.sequence == 2 and result.recurring.cut_on == date(2026, 12, 31)
    assert result.recurring.totals.total == D("3480.00") and result.monthly_equivalent == D("3480.00")
    assert constant(plan, date(2026, 11, 30), 3)[date(2026, 11, 20)] == 3


def test_preview_skips_the_demo_and_numbers_only_issued_charges():
    # Demo de 60 días desde el 1 de noviembre: noviembre es todo demo (no se emite); diciembre es demo hasta
    # el 30 y cobra un día (cargo 1); el periodo completo es enero (cargo 2).
    plan = terms(trial_days=60)
    result = preview(plan, 4)
    assert result.first is None and result.trial_ends_on == date(2026, 12, 30)
    assert result.recurring.cut_on == date(2027, 1, 31) and result.recurring.sequence == 2
    quarterly = preview(terms(interval_months=3, unit_price=D("100")), 1)
    assert quarterly.monthly_equivalent == (quarterly.recurring.totals.total / 3).quantize(D("0.01"))


def test_estimate_uses_closed_days_and_todays_headcount_for_the_rest():
    plan = terms()
    today = date(2026, 11, 11)
    closed = {date(2026, 11, d): 8 for d in range(1, 11)}
    closed.pop(date(2026, 11, 5))  # un día aún sin cerrar: se usa la plantilla de hoy
    result = estimate(plan, date(2026, 11, 30), 3, today, closed, 10)
    assert result.charge.sequence == 3 and result.days_total == 30 and result.days_elapsed == 11
    # 9 días cerrados × 8 + (el día sin cerrar + hoy) × 10 = 92 días-persona hasta hoy, a $10 cada uno.
    assert (result.accrued_units, result.accrued_subtotal) == (92, D("920.00"))
    assert result.charge.units == 92 + 19 * 10 and result.projected(date(2026, 11, 1))
    assert not result.projected(date(2026, 10, 1)) and not result.in_trial
    # Antes de que empiece el periodo: nada devengado; en la demo: in_trial.
    future = estimate(terms(starts_on=today + timedelta(days=30)), date(2026, 12, 31), 1, today, {}, 5)
    assert future.days_elapsed == 0 and future.accrued_units == 0
    trial = estimate(terms(trial_days=20), date(2026, 11, 30), 1, today, {}, 5)
    assert trial.in_trial and trial.accrued_units == 0


def test_the_person_days_carry_their_validator_breakdown_without_changing_the_money():
    """Decisión del dueño (2026-10-06): las unidades del cobro por empleado activo son días-persona (empleados y
    validadores) y cada línea dice cuántos fueron de validadores; el importe es el mismo con o sin el desglose."""
    plan = terms()
    november = [date(2026, 11, d) for d in range(1, 31)]
    headcount = dict.fromkeys(november, 11)  # 10 empleados y un validador
    validators = {day: 1 for day in november if day.day <= 20}  # el validador hasta el 20 (lo demás, empleados)
    split = compute_charge(plan, date(2026, 11, 30), 1, headcount, validators)
    plain = compute_charge(plan, date(2026, 11, 30), 1, headcount)
    assert (split.units, split.validator_units, split.lines[0].validator_units) == (330, 20, 20)
    assert split.totals == plain.totals and plain.validator_units is None  # sin el dato: sin desglose
    flat = compute_charge(terms(pricing_mode="FLAT", unit_price=D("900")), date(2026, 11, 30), 1, {}, validators)
    assert flat.validator_units is None  # el monto fijo no lleva desglose
    shown = preview(terms(starts_on=date(2026, 11, 16)), 12, 2)
    assert shown.first is not None and (shown.first.units, shown.first.validator_units) == (180, 30)
    assert shown.recurring.validator_units == 62 and preview(plan, 12).recurring.validator_units == 0
    today = date(2026, 11, 11)
    closed = {date(2026, 11, d): 8 for d in range(1, 11)}
    closed_validators = {date(2026, 11, d): 2 for d in range(1, 11)}
    result = estimate(plan, date(2026, 11, 30), 3, today, closed, 10, closed_validators, validators_now=3)
    # 10 días cerrados × 2 + hoy × 3 = 23 días-persona de validadores hasta hoy; y 20 días más × 3 en el pronóstico.
    assert (result.accrued_units, result.accrued_validator_units) == (90, 23)
    assert result.charge.validator_units == 23 + 19 * 3 and result.accrued_subtotal == D("900.00")
