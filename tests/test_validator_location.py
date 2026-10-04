"""Domicilio del validador y "requiere ubicación": solo inicia sesión dentro del radio del punto."""

import pytest

from app.core.database import SessionLocal
from app.core.geo import distance_m
from app.models import Validator
from app.services.location_service import format_distance
from tests.conftest import COMPANY_EMAIL, COMPANY_PASSWORD, create_company, login
from tests.test_policy import set_policy
from tests.test_validators import ADDRESS, PASSWORD, URL, create_validator

EMAIL = "recepcion@empresa.com"
#: ~55 m al norte del punto del domicilio (29.0729, -110.9559) y ~1.1 km al norte.
NEAR = {"latitude": 29.0734, "longitude": -110.9559}
FAR = {"latitude": 29.0829, "longitude": -110.9559}


@pytest.fixture(autouse=True)
def _no_device_approval(client, company_headers):
    """Estas pruebas tratan la ubicación: la autorización de dispositivos tiene las suyas."""
    set_policy(client, company_headers, validator_device_approval=False)


def _login(client, location=None):
    body = {"email": EMAIL, "password": PASSWORD, **({"location": location} if location else {})}
    return client.post("/api/auth/login", json=body)


def _guarded(client, company_headers, radius=100) -> dict:
    created = create_validator(client, company_headers, location_required=True, location_radius_m=radius)
    assert created.status_code == 201, created.text
    return created.json()["data"]


def test_distance_and_its_text():
    assert distance_m(29.0729, -110.9559, 29.0729, -110.9559) == 0
    assert 50 < distance_m(29.0729, -110.9559, NEAR["latitude"], NEAR["longitude"]) < 60
    assert 1_000 < distance_m(29.0729, -110.9559, FAR["latitude"], FAR["longitude"]) < 1_200
    assert [format_distance(m) for m in (55.4, 1_234, 15_600)] == ["55 m", "1.2 km", "16 km"]


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"address": None}, "address"),
        ({"postal_code": "830"}, "postal_code"),
        ({"country_code": "ZZ"}, "country_code"),
        ({"street": "  "}, "street"),
        ({"longitude": None}, "address"),
    ],
)
def test_the_address_is_required_and_validated(client, company_headers, change, field):
    address = None if change.get("address", ...) is None else {**ADDRESS, **change}
    response = client.post(
        URL,
        json={"name": "Recepción", "email": EMAIL, "password": PASSWORD, "address": address},
        headers=company_headers,
    )
    assert response.status_code == 422
    assert any(field in (error["field"] or "") for error in response.json()["errors"]), response.json()["errors"]


def test_address_is_normalized_and_returned(client, company_headers):
    other_country = {
        **ADDRESS,
        "country_code": "us",
        "postal_code": "94103 ",
        "interior_number": "  ",
        "street": "  Main   St ",
    }
    created = create_validator(client, company_headers, address=other_country)
    assert created.status_code == 201, created.text
    data = created.json()["data"]
    assert data["address"]["country_code"] == "US" and data["address"]["postal_code"] == "94103"
    assert data["address"]["interior_number"] is None and data["address"]["street"] == "Main St"
    assert data["location_required"] is False and data["location_radius_m"] is None


def test_a_validator_from_before_addresses_reads_without_one(client, company_headers):
    """Los validadores dados de alta antes de exigir domicilio siguen listándose (sin domicilio)."""
    created = create_validator(client, company_headers).json()["data"]
    with SessionLocal() as db:
        legacy = db.get(Validator, created["id"])
        assert legacy is not None
        legacy.street = None
        db.commit()
    assert client.get(f"{URL}/{created['id']}", headers=company_headers).json()["data"]["address"] is None


def test_requiring_location_needs_the_point_and_the_radius(client, company_headers):
    no_point = create_validator(
        client,
        company_headers,
        address={**ADDRESS, "latitude": None, "longitude": None},
        location_required=True,
        location_radius_m=100,
    )
    assert no_point.status_code == 422 and no_point.json()["code"] == "LOCATION_POINT_REQUIRED"
    no_radius = create_validator(client, company_headers, location_required=True)
    assert no_radius.status_code == 422 and no_radius.json()["code"] == "LOCATION_RADIUS_REQUIRED"
    tiny = create_validator(client, company_headers, location_required=True, location_radius_m=5)
    assert tiny.status_code == 422

    data = _guarded(client, company_headers, radius=150)
    assert data["location_required"] is True and data["location_radius_m"] == 150


def test_login_only_inside_the_allowed_radius(client, company_headers):
    _guarded(client, company_headers)

    missing = _login(client)
    assert missing.status_code == 403 and missing.json()["code"] == "LOCATION_REQUIRED"
    assert missing.json()["errors"][0]["details"] == {"radius_m": 100}

    far = _login(client, FAR)
    assert far.status_code == 403 and far.json()["code"] == "LOCATION_OUT_OF_RANGE"
    assert far.json()["errors"][0]["details"]["radius_m"] == 100
    assert 1_000 < far.json()["errors"][0]["details"]["distance_m"] < 1_200
    assert "1.1 km" in far.json()["message"]

    inaccurate = _login(client, {**NEAR, "accuracy": 900})
    assert inaccurate.status_code == 403 and inaccurate.json()["code"] == "LOCATION_INACCURATE"

    assert _login(client, {**NEAR, "accuracy": 12}).status_code == 200


def test_gps_error_is_tolerated_only_up_to_the_margin(client, company_headers):
    _guarded(client, company_headers, radius=30)  # el punto NEAR está a ~55 m
    assert _login(client, {**NEAR, "accuracy": 5}).json()["code"] == "LOCATION_OUT_OF_RANGE"
    assert _login(client, {**NEAR, "accuracy": 40}).status_code == 200  # 55 − 40 ≤ 30


def test_other_roles_and_validators_without_the_rule_ignore_the_location(client, company_headers):
    assert create_validator(client, company_headers).status_code == 201
    assert _login(client).status_code == 200
    assert _login(client, FAR).status_code == 200
    company = client.post(
        "/api/auth/login", json={"email": COMPANY_EMAIL, "password": COMPANY_PASSWORD, "location": FAR}
    )
    assert company.status_code == 200


def test_requiring_or_moving_the_location_closes_open_sessions(client, company_headers, admin_headers):
    validator = create_validator(client, company_headers).json()["data"]
    url = f"{URL}/{validator['id']}"
    headers = login(client, EMAIL, PASSWORD)

    renamed = client.put(url, json={"name": "Acceso norte"}, headers=company_headers)
    assert renamed.status_code == 200
    assert client.get("/api/checkpoint/me", headers=headers).status_code == 200  # nada de ubicación: sigue

    no_radius = client.put(url, json={"location_required": True}, headers=company_headers)
    assert no_radius.status_code == 422 and no_radius.json()["errors"][0]["field"] == "location_radius_m"

    guarded = client.put(url, json={"location_required": True, "location_radius_m": 80}, headers=company_headers)
    assert guarded.status_code == 200 and guarded.json()["data"]["location_required"] is True
    closed = client.get("/api/checkpoint/me", headers=headers)
    assert closed.status_code == 401

    inside = _login(client, NEAR).json()["data"]
    headers = {"Authorization": f"Bearer {inside['access_token']}"}
    moved = {**ADDRESS, "latitude": FAR["latitude"], "longitude": FAR["longitude"]}
    assert client.put(url, json={"address": moved}, headers=company_headers).status_code == 200
    assert client.get("/api/checkpoint/me", headers=headers).status_code == 401
    assert _login(client, NEAR).json()["code"] == "LOCATION_OUT_OF_RANGE"
    assert _login(client, FAR).status_code == 200

    # Otra empresa no ve ni edita el validador.
    create_company(client, admin_headers)
    other = login(client, "admin@panificadora.com", "Empresa1234")
    assert client.get(url, headers=other).status_code == 404
    assert client.put(url, json={"location_required": False}, headers=other).status_code == 404
