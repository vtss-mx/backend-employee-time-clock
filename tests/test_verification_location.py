"""Ubicación de las verificaciones (decisión del dueño del producto, 2026-10-07; migración 0085).

Pruebas de la regla pura `ensure_verification_location` (lo que exige el modo `verification_location` de la empresa) y
del rango de fechas del listado de la empresa. El flujo completo (la verificación bloquea en ENFORCE y registra en
OBSERVE, la bitácora con su punto y el endpoint `GET /verifications` aislado por empresa) lo cubren además las matrices
de `tests/test_authorization.py`, `tests/test_tenant_isolation.py`, `tests/test_api_language.py`
y `tests/test_envelope.py`.
"""

from datetime import date

import pytest

from app.core.exceptions import UnprocessableError
from app.schemas.auth import DeviceLocation
from app.services.company_verifications_service import _range
from app.services.identity_core import ensure_verification_location
from app.services.policy_service import PolicySnapshot

HERE = DeviceLocation(latitude=29.1, longitude=-110.9, accuracy=12)
IMPRECISE = DeviceLocation(latitude=29.1, longitude=-110.9, accuracy=500)
NO_ACCURACY = DeviceLocation(latitude=29.1, longitude=-110.9, accuracy=None)


def test_off_never_requires_location() -> None:
    ensure_verification_location(PolicySnapshot(verification_location="OFF"), None)  # no lanza


def test_observe_records_but_never_blocks() -> None:
    ensure_verification_location(PolicySnapshot(verification_location="OBSERVE"), None)  # no lanza
    ensure_verification_location(PolicySnapshot(verification_location="OBSERVE"), IMPRECISE)  # no lanza


def test_enforce_without_location_is_required() -> None:
    with pytest.raises(UnprocessableError) as exc:
        ensure_verification_location(PolicySnapshot(verification_location="ENFORCE"), None)
    assert exc.value.code == "LOCATION_REQUIRED" and exc.value.key == "VERIFICATION_LOCATION_REQUIRED"


def test_enforce_rejects_imprecise_or_missing_accuracy() -> None:
    policy = PolicySnapshot(verification_location="ENFORCE", max_location_accuracy_m=100)
    for bad in (IMPRECISE, NO_ACCURACY):
        with pytest.raises(UnprocessableError) as exc:
            ensure_verification_location(policy, bad)
        assert exc.value.code == "LOCATION_INVALID" and exc.value.key == "VERIFICATION_LOCATION_INVALID"


def test_enforce_accepts_a_precise_location() -> None:
    ensure_verification_location(PolicySnapshot(verification_location="ENFORCE", max_location_accuracy_m=100), HERE)


def test_range_converts_business_days_to_utc_bounds() -> None:
    since, until = _range(date(2026, 10, 1), date(2026, 10, 7))
    assert since is not None and until is not None and since < until
    # El día de la hora del Centro empieza y termina a la medianoche local (UTC con desfase):
    # el inicio es exclusivo-abierto.
    assert since.tzinfo is not None and until.tzinfo is not None
    assert _range(None, None) == (None, None)
