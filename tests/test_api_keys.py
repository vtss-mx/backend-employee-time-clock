"""Llaves de la API de integración: cada empresa accede ÚNICAMENTE a su propia información."""

import base64
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.exceptions import NotFoundError
from app.models import CompanyApiKey
from app.services.api_key_service import ApiClient
from app.services.integration_service import IntegrationService
from tests.conftest import approved_employee, create_company, create_employee, login, qr_content
from tests.test_validators import validator_headers

KEYS = "/api/api-keys"
API = "/api/integrations/v1"
ALL_SCOPES = ["EMPLOYEES_READ", "ATTENDANCE_READ", "VALIDATORS_READ", "VERIFICATION"]


def new_key(client, headers, *, scopes=None, name="Nómina", **extra) -> dict:
    body = {"name": name, "scopes": ALL_SCOPES if scopes is None else scopes, **extra}
    response = client.post(KEYS, json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def call(client, secret: str, path: str, **params):
    return client.get(f"{API}{path}", params=params, headers={"X-API-Key": secret})


def other_company(client, admin_headers) -> dict[str, str]:
    assert create_company(client, admin_headers, api_enabled=True).status_code == 201
    return login(client, "admin@panificadora.com", "Empresa1234")


# ---------------------------------------------------------------- administración de llaves


def test_company_creates_lists_and_revokes_its_keys(client, company_headers):
    created = new_key(
        client, company_headers, name="  Nómina   quincenal ", scopes=["EMPLOYEES_READ", "EMPLOYEES_READ"]
    )
    assert created["secret"].startswith("tck_") and len(created["secret"]) >= 40
    assert created["prefix"] == created["secret"][:12] and created["name"] == "Nómina quincenal"
    assert created["scopes"] == ["EMPLOYEES_READ"] and created["status"] == "ACTIVE"
    assert created["created_by"] == "admin@empresa.com" and created["expires_at"] is None

    listed = client.get(KEYS, headers=company_headers).json()["data"]
    assert listed["total"] == 1 and "secret" not in listed["items"][0]  # el secreto solo se ve al crearla
    with SessionLocal() as db:
        stored = db.get(CompanyApiKey, created["id"])
        assert stored is not None and created["secret"] not in (stored.key_hash, stored.prefix)

    revoked = client.delete(f"{KEYS}/{created['id']}", headers=company_headers)
    assert revoked.json()["code"] == "API_KEY_REVOKED" and revoked.json()["data"]["status"] == "REVOKED"
    assert revoked.json()["data"]["revoked_by"] == "admin@empresa.com"
    assert client.delete(f"{KEYS}/{created['id']}", headers=company_headers).status_code == 200  # idempotente
    assert call(client, created["secret"], "/company").json()["code"] == "API_KEY_REVOKED"


def test_key_rules(client, company_headers, monkeypatch):
    def create(**body):
        return client.post(KEYS, json={"name": "ERP", "scopes": ["EMPLOYEES_READ"], **body}, headers=company_headers)

    assert create(scopes=[]).status_code == 422
    unknown = create(scopes=["EMPLOYEES_WRITE"])
    assert unknown.status_code == 422 and unknown.json()["code"] == "API_SCOPE_INVALID"
    assert unknown.json()["errors"][0]["field"] == "scopes"
    assert create(name="   ").status_code == 422
    assert create(expires_in_days=0).status_code == 422 and create(expires_in_days=731).status_code == 422
    expiring = create(expires_in_days=30).json()["data"]
    remaining = datetime.fromisoformat(expiring["expires_at"]) - datetime.now(UTC)
    assert timedelta(days=29) < remaining <= timedelta(days=30)

    monkeypatch.setattr(settings, "API_KEYS_MAX_ACTIVE", 2)
    second = create().json()["data"]
    limit = create()
    assert limit.status_code == 409 and limit.json()["code"] == "API_KEY_LIMIT"
    client.delete(f"{KEYS}/{second['id']}", headers=company_headers)
    assert create().status_code == 201  # las revocadas no cuentan


def test_only_the_company_admin_manages_keys(client, company_headers, admin_headers):
    employee = approved_employee(client, company_headers)
    validator = validator_headers(client, company_headers, mode="QR")
    for headers in (employee, validator, admin_headers):  # ni la plataforma: no ve datos de las empresas
        assert client.get(KEYS, headers=headers).status_code == 403
        assert client.post(KEYS, json={"name": "x", "scopes": ["EMPLOYEES_READ"]}, headers=headers).status_code == 403


def test_rotation_issues_a_new_secret_and_kills_the_old_one(client, company_headers, admin_headers):
    old = new_key(client, company_headers, scopes=["VALIDATORS_READ"], expires_in_days=90)
    rotated = client.post(f"{KEYS}/{old['id']}/rotate", headers=company_headers)
    assert rotated.status_code == 201 and rotated.json()["code"] == "API_KEY_ROTATED"
    fresh = rotated.json()["data"]
    assert fresh["secret"] != old["secret"] and fresh["name"] == old["name"] and fresh["scopes"] == ["VALIDATORS_READ"]
    lifetime = datetime.fromisoformat(fresh["expires_at"]) - datetime.now(UTC)
    assert timedelta(days=89) < lifetime <= timedelta(days=90)  # conserva su vigencia
    assert call(client, old["secret"], "/company").json()["code"] == "API_KEY_REVOKED"
    assert call(client, fresh["secret"], "/company").status_code == 200
    again = client.post(f"{KEYS}/{old['id']}/rotate", headers=company_headers)
    assert again.status_code == 409 and again.json()["code"] == "API_KEY_REVOKED"

    # Una llave de otra empresa no existe para esta.
    stranger = new_key(client, other_company(client, admin_headers), scopes=["VALIDATORS_READ"])
    assert client.post(f"{KEYS}/{stranger['id']}/rotate", headers=company_headers).status_code == 404
    assert client.delete(f"{KEYS}/{stranger['id']}", headers=company_headers).status_code == 404


# ---------------------------------------------------------------- API de integración


def test_a_key_only_reaches_its_own_company(client, company_headers, admin_headers):
    mine = create_employee(client, company_headers).json()["data"]
    bakery = other_company(client, admin_headers)
    theirs = create_employee(client, bakery, number="PAN-1", email="ana@pan.com", phone="+52 662 765 4321").json()[
        "data"
    ]
    secret = new_key(client, company_headers)["secret"]

    company = call(client, secret, "/company").json()["data"]
    assert company["name"] == "Mi empresa" and company["timezone"] == "America/Mexico_City"
    assert company["key"]["scopes"] == sorted(ALL_SCOPES)
    employees = call(client, secret, "/employees").json()["data"]
    assert [e["id"] for e in employees["items"]] == [mine["id"]]
    assert call(client, secret, f"/employees/{mine['id']}").json()["data"]["employee_number"] == "EMP-001"
    assert call(client, secret, f"/employees/{theirs['id']}").status_code == 404  # otra empresa: no existe
    assert call(client, secret, "/attendance", employee_id=theirs["id"]).status_code == 404
    assert not {"photo", "embedding", "face_embeddings"} & set(employees["items"][0])  # nada biométrico

    their_secret = new_key(client, bakery)["secret"]
    assert [e["id"] for e in call(client, their_secret, "/employees").json()["data"]["items"]] == [theirs["id"]]


def test_each_endpoint_requires_its_permission(client, company_headers):
    secret = new_key(client, company_headers, scopes=["VALIDATORS_READ"])["secret"]
    for path in ("/employees", "/employees/1", "/attendance"):
        denied = call(client, secret, path)
        assert denied.status_code == 403 and denied.json()["code"] == "API_SCOPE_REQUIRED", path
    assert "«Empleados»" in call(client, secret, "/employees").json()["message"]
    assert call(client, secret, "/validators").status_code == 200
    assert call(client, secret, "/company").status_code == 200


def test_authentication_is_only_by_key_and_only_here(client, company_headers, admin_headers):
    missing = client.get(f"{API}/company")
    assert missing.status_code == 401 and missing.json()["code"] == "API_KEY_REQUIRED"
    assert "ApiKey" in missing.headers["WWW-Authenticate"]
    assert call(client, "tck_inventada", "/company").json()["code"] == "API_KEY_INVALID"
    assert call(client, "otra-cosa", "/company").json()["code"] == "API_KEY_INVALID"
    # La sesión de un usuario no sirve en la API de integración...
    assert client.get(f"{API}/company", headers=company_headers).json()["code"] == "API_KEY_REQUIRED"
    # ...ni la llave en el resto de la API.
    key = new_key(client, company_headers)
    assert client.get("/api/employees", headers={"X-API-Key": key["secret"]}).status_code == 401

    with SessionLocal() as db:
        stored = db.get(CompanyApiKey, key["id"])
        assert stored is not None
        stored.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
    assert call(client, key["secret"], "/company").json()["code"] == "API_KEY_EXPIRED"
    assert client.get(KEYS, headers=company_headers).json()["data"]["items"][0]["status"] == "EXPIRED"

    active = new_key(client, company_headers)["secret"]
    company_id = client.get("/api/users/me", headers=company_headers).json()["data"]["company"]["id"]
    client.patch(f"/api/admin/companies/{company_id}/status", json={"active": False}, headers=admin_headers)
    inactive = call(client, active, "/company")
    assert inactive.status_code == 403 and inactive.json()["code"] == "COMPANY_INACTIVE"


def test_usage_is_recorded_and_rate_limited(client, company_headers, monkeypatch):
    secret = new_key(client, company_headers)["secret"]
    monkeypatch.setattr(settings, "RATE_LIMIT_API_KEY_PER_MINUTE", 2)
    statuses = [call(client, secret, "/company").status_code for _ in range(3)]
    assert statuses == [200, 200, 429]
    used = client.get(KEYS, headers=company_headers).json()["data"]["items"][0]
    assert used["last_used_at"] is not None and used["last_used_ip"]


def test_attendance_log_with_filters(client, company_headers):
    headers = approved_employee(client, company_headers)
    employee_id = client.get("/api/users/me", headers=headers).json()["data"]["employee"]["id"]
    checkpoint = validator_headers(client, company_headers, mode="QR", name="Recepción norte")
    content = qr_content(employee_id)
    for _ in range(2):  # el segundo intento con el mismo QR falla (ya se usó)
        client.post("/api/checkpoint/identify/qr", json={"qr_content": content}, headers=checkpoint)
    secret = new_key(client, company_headers, scopes=["ATTENDANCE_READ"])["secret"]

    events = call(client, secret, "/attendance").json()["data"]
    assert events["total"] == 2
    failed, ok = events["items"]  # la más reciente primero
    assert ok["success"] is True and ok["method"] == "QR" and ok["employee_name"] == "Juan Pérez"
    assert ok["validator_name"] == "Recepción norte" and ok["employee_number"] == "EMP-001"
    assert failed["success"] is False and failed["reason"] == "ALREADY_USED" and failed["employee_id"] is None

    assert call(client, secret, "/attendance", success="true").json()["data"]["total"] == 1
    assert call(client, secret, "/attendance", employee_id=employee_id).json()["data"]["total"] == 1
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    assert call(client, secret, "/attendance", since=future).json()["data"]["total"] == 0
    backwards = call(client, secret, "/attendance", since=future, until="2020-01-01T00:00:00Z")
    assert backwards.status_code == 422 and backwards.json()["code"] == "INVALID_RANGE"


def test_attendance_feed_syncs_by_cursor_without_gaps(client, company_headers, monkeypatch):
    from app.services import integration_service

    headers = approved_employee(client, company_headers)
    employee_id = client.get("/api/users/me", headers=headers).json()["data"]["employee"]["id"]
    checkpoint = validator_headers(client, company_headers, mode="QR")
    for _ in range(3):
        client.post("/api/checkpoint/identify/qr", json={"qr_content": qr_content(employee_id)}, headers=checkpoint)
    secret = new_key(client, company_headers, scopes=["ATTENDANCE_READ"])["secret"]

    settling = call(client, secret, "/attendance/feed").json()["data"]
    assert settling == {"items": [], "next_cursor": None, "has_more": False}  # aún dentro de la ventana

    monkeypatch.setattr(integration_service, "FEED_SETTLE_SECONDS", 0)
    first = call(client, secret, "/attendance/feed", limit=2).json()["data"]
    assert [e["success"] for e in first["items"]] == [True, True] and first["has_more"] is True
    assert first["items"][0]["id"] < first["items"][1]["id"]  # orden de llegada
    rest = call(client, secret, "/attendance/feed", after=first["next_cursor"], limit=2).json()["data"]
    assert len(rest["items"]) == 1 and rest["has_more"] is False
    caught_up = call(client, secret, "/attendance/feed", after=rest["next_cursor"]).json()["data"]
    assert caught_up["items"] == [] and caught_up["next_cursor"] == rest["next_cursor"]

    invalid = call(client, secret, "/attendance/feed", after="no-es-un-cursor")
    assert invalid.status_code == 422 and invalid.json()["code"] == "INVALID_CURSOR"
    assert call(client, secret, "/attendance/feed", limit=501).status_code == 422


def test_rotating_a_key_without_expiry_keeps_it_without_expiry(client, company_headers):
    old = new_key(client, company_headers, scopes=["VALIDATORS_READ"])
    fresh = client.post(f"{KEYS}/{old['id']}/rotate", headers=company_headers).json()["data"]
    assert fresh["expires_at"] is None and fresh["status"] == "ACTIVE"


def test_the_feed_and_the_log_reject_foreign_employees_and_cursors(client, company_headers, admin_headers):
    """El feed tampoco deja ver (ni confirmar que existe) a un empleado de otra empresa, y un cursor
    de otra versión del formato no se interpreta a ciegas."""
    theirs = create_employee(client, other_company(client, admin_headers), number="PAN-1", email="ana@pan.com")
    secret = new_key(client, company_headers, scopes=["ATTENDANCE_READ"])["secret"]
    foreign = call(client, secret, "/attendance/feed", employee_id=theirs.json()["data"]["id"])
    assert foreign.status_code == 404 and foreign.json()["code"] == "EMPLOYEE_NOT_FOUND"

    other_version = base64.urlsafe_b64encode(b"v2:5").decode().rstrip("=")  # un formato futuro
    invalid = call(client, secret, "/attendance/feed", after=other_version)
    assert invalid.status_code == 422 and invalid.json()["code"] == "INVALID_CURSOR"

    past = call(client, secret, "/attendance", until="2020-01-01T00:00:00Z").json()["data"]
    assert past["total"] == 0  # solo hasta una fecha (sin «since»)


def test_a_key_whose_company_disappeared_mid_request_answers_not_found():
    """La llave se valida antes; si la empresa se borra a la mitad de la petición, 404 (no un 500)."""
    client = ApiClient(1, 999_999, "ERP", "tck_x", frozenset({"EMPLOYEES_READ"}), None)
    with SessionLocal() as db, pytest.raises(NotFoundError) as gone:
        IntegrationService(db, client).company()
    assert gone.value.code == "COMPANY_NOT_FOUND"


# ---------------------------------------------------------------- acceso al módulo (lo decide el ADMIN)


def _company_id(client, admin_headers) -> int:
    items = client.get("/api/admin/companies", headers=admin_headers).json()["data"]["items"]
    return next(c["id"] for c in items if c["name"] != "Panificadora del Norte")


def test_the_admin_decides_which_companies_have_the_api(client, company_headers, admin_headers):
    """Sin el módulo: la pantalla no aparece, no se administran llaves y las existentes dejan de
    servir (se conservan: al volver a darlo, funcionan)."""
    secret = new_key(client, company_headers)["secret"]
    assert call(client, secret, "/employees").status_code == 200
    company_id = _company_id(client, admin_headers)

    off = client.put(f"/api/admin/companies/{company_id}", json={"api_enabled": False}, headers=admin_headers)
    assert off.status_code == 200 and off.json()["data"]["api_enabled"] is False
    screens = [s["code"] for s in client.get("/api/users/me", headers=company_headers).json()["data"]["screens"]]
    assert "COMPANY_API" not in screens
    denied = client.get("/api/api-keys", headers=company_headers)
    assert denied.status_code == 403 and denied.json()["code"] == "API_ACCESS_DISABLED"
    blocked = call(client, secret, "/employees")
    assert blocked.status_code == 403 and blocked.json()["code"] == "API_ACCESS_DISABLED"

    client.put(f"/api/admin/companies/{company_id}", json={"api_enabled": True}, headers=admin_headers)
    assert call(client, secret, "/employees").status_code == 200  # la misma llave vuelve a servir
    screens = [s["code"] for s in client.get("/api/users/me", headers=company_headers).json()["data"]["screens"]]
    assert "COMPANY_API" in screens


def test_new_companies_start_without_the_api_unless_the_admin_gives_it(client, admin_headers):
    from tests.conftest import create_company

    plain = create_company(client, admin_headers).json()["data"]
    assert plain["api_enabled"] is False
    with_api = create_company(
        client, admin_headers, rfc="PNO120315AB2", admin_email="otra@panificadora.com", name="Otra", api_enabled=True
    )
    assert with_api.json()["data"]["api_enabled"] is True
