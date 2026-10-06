"""RFC del empleado, acceso solo desde teléfono celular e integridad garantizada por índices."""

from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.database import SessionLocal
from app.core.devices import classify_device
from app.i18n import use_locale
from app.models import Employee, EmployeeQr
from app.schemas.validators import curp_check_digit, normalize_rfc, rfc_matches_birth_date
from app.schemas.validators import normalize_curp as normalize_curp_value
from tests.conftest import (
    DESKTOP_UA,
    IPHONE_UA,
    approved_employee,
    create_employee,
    curp_for,
    nss_for,
    phone_for,
    qr_content,
    rfc_for,
)

UA = {
    "android_phone": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/130.0 Mobile Safari/537.36",
    "android_tablet": "Mozilla/5.0 (Linux; Android 14; SM-X710) AppleWebKit/537.36 Chrome/130.0 Safari/537.36",
    "firefox_tablet": "Mozilla/5.0 (Android 14; Tablet; rv:131.0) Gecko/131.0 Firefox/131.0",
    "firefox_phone": "Mozilla/5.0 (Android 14; Mobile; rv:131.0) Gecko/131.0 Firefox/131.0",
    "old_ipad": "Mozilla/5.0 (iPad; CPU OS 12_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
    "windows": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36",
}


# ---------------------------------------------------------------- dispositivos


@pytest.mark.parametrize(
    ("user_agent", "hint", "expected"),
    [
        (IPHONE_UA, None, "phone"),
        (UA["android_phone"], None, "phone"),
        (UA["firefox_phone"], None, "phone"),
        (UA["android_tablet"], None, "tablet"),
        (UA["firefox_tablet"], None, "tablet"),
        (UA["old_ipad"], None, "tablet"),
        (DESKTOP_UA, None, "desktop"),  # también iPadOS moderno, que se presenta como Mac
        (UA["windows"], None, "desktop"),
        (None, None, "desktop"),
        # Client Hint de Chrome: manda sobre el User-Agent.
        (UA["android_phone"], "?1", "phone"),
        (UA["android_tablet"], "?0", "tablet"),
        ("Mozilla/5.0 (X11; Linux x86_64) Chrome/130.0", "?0", "desktop"),  # "sitio de escritorio"
        (UA["old_ipad"], "?1", "tablet"),
    ],
)
def test_classify_device(user_agent, hint, expected):
    assert classify_device(user_agent, hint) == expected


def test_employees_and_admins_use_any_device(client, company_headers):
    """Solo los validadores tienen restricción de dispositivo: empleados y administradores no."""
    assert create_employee(client, company_headers).status_code == 201
    credentials = {"email": "juan@empresa.com", "password": "Empleado123"}
    for user_agent in (DESKTOP_UA, UA["android_tablet"], UA["old_ipad"], UA["android_phone"]):
        session = client.post("/api/auth/login", json=credentials, headers={"User-Agent": user_agent})
        assert session.status_code == 200, user_agent
        token = {"Authorization": f"Bearer {session.json()['data']['access_token']}", "User-Agent": user_agent}
        assert client.get("/api/users/me", headers=token).status_code == 200
    company = client.post(
        "/api/auth/login",
        json={"email": "admin@empresa.com", "password": "Admin1234"},
        headers={"User-Agent": DESKTOP_UA},
    )
    assert company.status_code == 200
    desktop_headers = {"Authorization": f"Bearer {company.json()['data']['access_token']}", "User-Agent": DESKTOP_UA}
    assert (
        "employee_mobile_only" not in client.get("/api/settings/verification", headers=desktop_headers).json()["data"]
    )


# ---------------------------------------------------------------- RFC


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [("pegj900515ab1", "PEGJ900515AB1"), (" PEGJ-900515-AB1 ", "PEGJ900515AB1"), ("ÑAÑE000229XYA", "ÑAÑE000229XYA")],
)
def test_normalize_rfc(raw, normalized):
    assert normalize_rfc(raw) == normalized


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("XAXX010101000", "genérico"),
        ("PEG900515AB1", "13 caracteres"),  # 12: persona moral
        ("PEGJ9005ABAB1", "formato"),
        ("PEGJ901315AB1", "fecha"),  # mes 13
        ("PEGJ010229AB1", "fecha"),  # 29 de febrero de 1901 / 2001
    ],
)
def test_invalid_rfc(raw, message):
    with pytest.raises(ValueError, match=message):
        normalize_rfc(raw)


def test_rfc_matches_birth_date():
    assert rfc_matches_birth_date("PEGJ900515AB1", date(1990, 5, 15))
    assert not rfc_matches_birth_date("PEGJ900515AB1", date(1990, 5, 16))


def test_birth_date_mismatch_says_which_date_each_document_has():
    """El mensaje dice qué fecha indica el documento y cuál se capturó (para corregir la que esté mal)."""
    from app.schemas.validators import curp_birth_date_error, rfc_birth_date_error

    assert rfc_birth_date_error("TARS030901AB1", date(2003, 9, 1)) is None
    assert str(rfc_birth_date_error("TARS030901AB1", date(2003, 9, 3))) == (
        "El RFC indica nacimiento el 01/09/2003, pero la fecha de nacimiento es 03/09/2003"
    )
    assert str(curp_birth_date_error("HEGG560427MVZRRL04", date(1956, 4, 28))) == (
        "La CURP indica nacimiento el 27/04/1956, pero la fecha de nacimiento es 28/04/1956"
    )
    assert "siglo" in str(curp_birth_date_error("HEGG560427MVZRRL04", date(2056, 4, 27)))
    # Un 29 de febrero que no existe con el siglo de la fecha capturada se dice tal cual (no hay fecha que escribir).
    assert "29/02/1900" in str(rfc_birth_date_error("TARS000229AB1", date(1900, 3, 1)))
    with use_locale("en-US"):
        assert str(rfc_birth_date_error("TARS030901AB1", date(2003, 9, 3))) == (
            "The RFC shows a birth date of 09/01/2003, but the date of birth is 09/03/2003"
        )
    with pytest.raises(ValueError, match="se escribieron 17"):
        normalize_curp_value("TARS030901HSRNZB1")


def test_create_employee_requires_valid_unique_rfc(client, company_headers):
    created = create_employee(client, company_headers, rfc="pexj-900510-ab1")
    assert created.status_code == 201
    assert created.json()["data"]["rfc"] == "PEXJ900510AB1"

    duplicate = create_employee(
        client, company_headers, number="EMP-002", email="otro@empresa.com", rfc="PEXJ900510AB1"
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "RFC_TAKEN"
    assert duplicate.json()["errors"][0]["field"] == "rfc"

    mismatch = create_employee(client, company_headers, number="EMP-003", email="tres@empresa.com", rfc="PEXJ910510AB1")
    assert mismatch.status_code == 422
    assert mismatch.json()["code"] == "RFC_BIRTH_DATE_MISMATCH"
    assert mismatch.json()["errors"][0]["field"] == "rfc"

    invalid = create_employee(
        client, company_headers, number="EMP-004", email="cuatro@empresa.com", rfc="XAXX010101000"
    )
    assert invalid.status_code == 422
    assert invalid.json()["errors"][0]["field"] == "rfc"


def test_update_rfc_is_checked_against_birth_date(client, company_headers):
    employee_id = create_employee(client, company_headers).json()["data"]["id"]
    url = f"/api/employees/{employee_id}"
    # Cambiar solo la fecha deja el RFC vigente inconsistente: se rechaza.
    moved = client.put(url, json={"birth_date": "1991-05-10"}, headers=company_headers)
    assert moved.status_code == 422 and moved.json()["code"] == "RFC_BIRTH_DATE_MISMATCH"
    # CURP: también lleva la fecha, así que cambia junto con ella.
    curp_1991 = "PEXJ910510HSRBCD0" + curp_check_digit("PEXJ910510HSRBCD0")
    both = client.put(
        url, json={"birth_date": "1991-05-10", "rfc": "PEXJ910510ZZ9", "curp": curp_1991}, headers=company_headers
    )
    assert both.status_code == 200, both.text
    assert both.json()["data"]["rfc"] == "PEXJ910510ZZ9"


def test_rfc_availability(client, company_headers):
    employee_id = create_employee(client, company_headers).json()["data"]["id"]
    url = "/api/validation"

    def check(value, **extra):
        return client.get(url, params={"field": "rfc", "value": value, **extra}, headers=company_headers).json()["data"]

    assert check(rfc_for("EMP-001").lower())["code"] == "TAKEN"
    assert check(rfc_for("EMP-001"), exclude_id=employee_id)["code"] == "AVAILABLE"
    assert check("PEGJ900515AB1")["code"] == "AVAILABLE"
    assert check("PEGJ")["code"] == "INVALID_FORMAT"
    # Opcional: vacío no se consulta y no impide guardar.
    empty = check(" ")
    assert (empty["code"], empty["valid"], empty["available"]) == ("EMPTY", True, True)


# ---------------------------------------------------------------- búsqueda e índices


def test_search_by_name_number_rfc_and_email(client, company_headers):
    create_employee(client, company_headers, number="EMP-001", email="juan@empresa.com")
    create_employee(client, company_headers, number="EMP-002", email="ana_lopez@empresa.com")

    def search(term):
        data = client.get("/api/employees", params={"search": term}, headers=company_headers).json()["data"]
        return sorted(item["employee_number"] for item in data["items"])

    assert search("juan  pérez") == ["EMP-001", "EMP-002"]  # nombre completo (ambos se llaman igual)
    assert search("emp-002") == ["EMP-002"]
    assert search(rfc_for("EMP-001")[:12].lower()) == ["EMP-001"]
    assert search("ana_lopez") == ["EMP-002"]
    assert search("ana%lopez") == []  # "%" y "_" se buscan literalmente, no como comodines
    assert search("anaxlopez") == []


def test_database_allows_a_single_active_qr_per_employee(client, company_headers):
    employee_id = create_employee(client, company_headers).json()["data"]["id"]
    qr_content(employee_id)  # su QR vigente
    with SessionLocal() as db:
        company_id = db.get(Employee, employee_id).company_id
        db.add(EmployeeQr(employee_id=employee_id, company_id=company_id, token_hash="f" * 64, active=True))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        # Inactivos (revocados) puede haber los que sean.
        db.add(EmployeeQr(employee_id=employee_id, company_id=company_id, token_hash="e" * 64, active=False))
        db.commit()

    # Emitir otro sigue funcionando: apaga el vigente y crea uno nuevo en la misma transacción.
    assert qr_content(employee_id) != qr_content(employee_id)


# ---------------------------------------------------------------- CURP, NSS y teléfono


def test_curp_validation():
    from app.schemas.validators import curp_birth_date_error, normalize_curp

    assert normalize_curp(" hegg-560427-mvzrrl04 ") == "HEGG560427MVZRRL04"  # ejemplo oficial de RENAPO
    for bad, message in [
        ("HEGG560427MVZRRL0", "18 caracteres"),
        ("HEGG560427MXXRRL04", "formato"),  # entidad inexistente
        ("HEGG561327MVZRRL04", "fecha"),
        ("HEGG560427MVZRRL05", "dígito verificador"),
    ]:
        with pytest.raises(ValueError, match=message):
            normalize_curp(bad)
    assert curp_birth_date_error("HEGG560427MVZRRL04", date(1956, 4, 27)) is None
    assert curp_birth_date_error("HEGG560427MVZRRL04", date(2056, 4, 27))  # siglo: dígito = antes de 2000


def test_nss_and_phone_validation():
    from app.schemas.validators import normalize_nss, normalize_phone

    assert normalize_nss("1234 5678 903") == "12345678903"
    with pytest.raises(ValueError, match="dígito verificador"):
        normalize_nss("12345678904")
    with pytest.raises(ValueError, match="11 dígitos"):
        normalize_nss("1234")
    # E.164 con lada internacional; sin lada se asume México y el prefijo antiguo +52 1 se convierte.
    assert normalize_phone("+52 (662) 123-4567") == "+526621234567"
    assert normalize_phone("662 123 4567") == "+526621234567"
    assert normalize_phone("+52 1 662 123 4567") == "+526621234567"
    assert normalize_phone("521 662 123 4567") == "+526621234567"
    assert normalize_phone("+1 (415) 555-2671") == "+14155552671"
    assert normalize_phone("+34 612 34 56 78") == "+34612345678"
    for bad in ("123", "0621234567", "5555555555", "+52 662 123 456", "+1 000 000 0000", "+999 1234567", "abc"):
        with pytest.raises(ValueError):
            normalize_phone(bad)


def test_employee_requires_unique_curp_nss_matching_birth_date(client, company_headers):
    created = create_employee(client, company_headers).json()["data"]
    assert created["curp"] == curp_for("EMP-001") and created["nss"] == nss_for("EMP-001")
    assert created["phone"] == "+52" + phone_for("EMP-001")

    def create(**overrides):
        body = {
            "first_name": "Ana",
            "last_name": "López",
            "birth_date": "1990-05-10",
            "employee_number": "EMP-777",
            "rfc": rfc_for("EMP-777"),
            "curp": curp_for("EMP-777"),
            "nss": nss_for("EMP-777"),
            "phone": "6629876543",
            "email": "ana@empresa.com",
            "password": "Empleado123",
            **overrides,
        }
        return client.post("/api/employees", json=body, headers=company_headers)

    taken = create(curp=created["curp"])
    assert taken.status_code == 409 and taken.json()["code"] == "CURP_TAKEN"
    assert taken.json()["errors"][0]["field"] == "curp"
    assert create(nss=created["nss"]).json()["code"] == "NSS_TAKEN"
    mismatch = create(birth_date="1990-05-11", rfc="PEXJ900511AB1")
    assert mismatch.status_code == 422 and mismatch.json()["code"] == "CURP_BIRTH_DATE_MISMATCH"
    missing = client.post("/api/employees", json={"first_name": "Ana"}, headers=company_headers)
    fields = {e["field"] for e in missing.json()["errors"]}
    # RFC, CURP y NSS son opcionales: faltar no es un error (sí la fecha de nacimiento y el teléfono).
    assert {"birth_date", "phone"} <= fields and not {"curp", "nss", "rfc"} & fields
    assert create().status_code == 201


def test_curp_and_nss_availability_and_search(client, company_headers):
    employee_id = create_employee(client, company_headers).json()["data"]["id"]

    def check(field, value, **extra):
        params = {"field": field, "value": value, **extra}
        return client.get("/api/validation", params=params, headers=company_headers).json()["data"]["code"]

    assert check("curp", curp_for("EMP-001")) == "TAKEN"
    assert check("curp", curp_for("EMP-001"), exclude_id=employee_id) == "AVAILABLE"
    assert check("nss", nss_for("EMP-001")) == "TAKEN"
    assert check("nss", "12345678904") == "INVALID_FORMAT"
    found = client.get("/api/employees", params={"search": curp_for("EMP-001")[:12]}, headers=company_headers)
    assert found.json()["data"]["total"] == 1


def test_company_requests_identity_reverification_with_reason(client, company_headers):
    headers = approved_employee(client, company_headers)
    employee_id = client.get("/api/users/me", headers=headers).json()["data"]["employee"]["id"]
    response = client.post(
        f"/api/employees/{employee_id}/face/reset",
        json={"reason": "Actualización anual de identidad"},
        headers=company_headers,
    )
    assert response.status_code == 200 and response.json()["code"] == "IDENTITY_REVERIFY_REQUESTED"
    me = client.get("/api/users/me", headers=headers).json()["data"]["employee"]
    assert me["face_status"] == "NOT_ENROLLED"
    assert me["face_rejection_reason"] == "Actualización anual de identidad"
    # Sin motivo: se usa uno genérico.
    client.post(f"/api/employees/{employee_id}/face/reset", headers=company_headers)
    me = client.get("/api/users/me", headers=headers).json()["data"]["employee"]
    assert me["face_rejection_reason"] == "Tu empresa pidió que verifiques tu identidad de nuevo"
