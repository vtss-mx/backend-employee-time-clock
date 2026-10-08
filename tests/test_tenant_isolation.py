"""Aislamiento total entre empresas (regla 14 del AGENTS.md raíz: por ninguna razón se mezclan sus datos).

Dos empresas con datos de TODO tipo: la A (la de las pruebas) y la B, cuyo cada dato lleva la marca "zzb". Con las
credenciales de A (administrador, empleado, validador y llave de integración):

- cada ruta con un id en la ruta, llamada con el id de un dato de B, responde 404 (`ROUTES`; una ruta nueva con id
  que no esté aquí hace fallar `test_every_route_with_an_id_is_in_the_matrix`);
- un id de B dentro del cuerpo (asignar, departamentos, ausencias, jornadas...) también responde 404;
- ningún listado, conteo, tablero, búsqueda, exportación ni la API de integración de A muestra algo de B;
- el validador de A no reconoce el QR de un empleado de B y la validación en vivo de A no ve los datos únicos de B;
- la base rechaza ligar filas de dos empresas (llaves foráneas compuestas), aunque el código se equivocara;
- en PostgreSQL (`./scripts/quality.sh --postgres`, con el usuario de la API), la seguridad por fila: una consulta
  sin su empresa no ve nada, una deliberadamente sin filtro solo ve la suya y no puede escribir en otra.
"""

import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.clock import business_today
from app.core.database import SessionLocal, engine
from app.core.row_security import PLATFORM, SCOPE_KEY
from app.main import API_ROUTERS
from app.models import (
    AttendanceEvent,
    AuthSession,
    Department,
    Employee,
    EmployeeAbsence,
    EmployeeQr,
    EnrollmentVoiceAnswer,
    FaceEmbedding,
    FaceEnrollment,
    Passkey,
    ShiftAssignment,
    User,
    ValidatorDevice,
    VerificationLog,
    WorkBreak,
    WorkSession,
)
from tests.avatar_support import fetch, me, upload
from tests.conftest import create_company, login, qr_content
from tests.document_support import PDF
from tests.test_api_keys import call, new_key
from tests.test_shifts import address, assign, create_shift, create_site, shift_body
from tests.test_validators import PASSWORD, approved, validator_headers

MARK = "zzb"
TODAY = business_today()
NOW = datetime.now(UTC)


def _created(response) -> dict:
    assert response.status_code in (200, 201), response.text
    return response.json()["data"]


def _work_session(company_id: int, employee_id: int, assignment_id: int) -> int:
    """Una jornada cerrada con su descanso, su registro y su intento en la bitácora (directo en la base)."""
    with SessionLocal() as db:
        start = NOW - timedelta(days=2)
        session = WorkSession(
            company_id=company_id,
            employee_id=employee_id,
            assignment_id=assignment_id,
            work_date=start.date(),
            shift_name="ZZB Turno",
            scheduled_start=start,
            scheduled_end=start + timedelta(hours=8),
            check_out_deadline=start + timedelta(hours=9),
            breaks_allowed=1,
            break_minutes_allowed=30,
            early_check_out_minutes=5,
            status="CLOSED",
            check_in_at=start,
            check_in_mode="REMOTE",
            check_out_at=start + timedelta(hours=8),
            check_out_mode="REMOTE",
            worked_minutes=450,
        )
        db.add(session)
        db.flush()
        log = VerificationLog(
            company_id=company_id, employee_id=employee_id, method="FACE", success=True, score=0.99, created_at=start
        )
        db.add_all([log, WorkBreak(company_id=company_id, session_id=session.id, started_at=start)])
        db.flush()
        db.add(
            AttendanceEvent(
                company_id=company_id,
                employee_id=employee_id,
                session_id=session.id,
                action="CHECK_IN",
                mode="REMOTE",
                occurred_at=start,
                verification_log_id=log.id,
                note="ZZB evidencia",
            )
        )
        db.commit()
        return session.id


@pytest.fixture
def tenants(client, company_headers, admin_headers) -> dict:
    """Credenciales de A y los ids de cada dato de B."""
    # B con los mismos módulos que A (Integraciones y validadores): un 404 es por ser de otra empresa, no un 403.
    created = create_company(client, admin_headers, name="ZZB Panificadora", api_enabled=True, max_validators=5)
    assert created.status_code == 201
    b = login(client, "admin@panificadora.com", "Empresa1234")
    employee = approved(client, b, "zzbana", number="ZZB-001")
    b_employee = login(client, "zzbana@empresa.com", "Empleado123")
    assert upload(client, b_employee).status_code == 200  # su foto de perfil (de la persona; la ve solo B)
    department = _created(client.post("/api/departments", json={"name": "ZZB Depto"}, headers=b))
    _created(
        client.post(f"/api/departments/{department['id']}/employees", json={"employee_id": employee["id"]}, headers=b)
    )
    _created(
        client.post(f"/api/departments/{department['id']}/managers", json={"employee_id": employee["id"]}, headers=b)
    )
    site = create_site(client, b, name="ZZB Sitio")
    kiosk = _created(client.post(f"/api/sites/{site['id']}/kiosks", json={"name": "ZZB Kiosco"}, headers=b))
    # Un documento de B (decisión del dueño, 2026-10-06): su archivo cifrado en el bucket y su referencia en la base.
    document = _created(
        client.post(
            "/api/documents",
            files={"file": ("zzb-constancia.pdf", PDF, "application/pdf")},
            data={"type": "TAX_CERTIFICATE", "note": "ZZB para facturar"},
            headers=b,
        )
    )
    shift = create_shift(client, b, name="ZZB Turno")
    night = create_shift(client, b, name="ZZB Nocturno", start_time="22:00", end_time="06:00")
    assignment = _created(assign(client, b, employee["id"], shift["id"], TODAY))
    request = _created(
        client.post(
            "/api/me/shift-requests",
            json={
                "shift_id": night["id"],
                "valid_from": (TODAY + timedelta(days=3)).isoformat(),
                "reason": "ZZB cambio",
            },
            headers=b_employee,
        )
    )
    holiday_day = TODAY + timedelta(days=40)
    holiday = _created(
        client.post(
            "/api/calendar/holidays", json={"holiday_date": holiday_day.isoformat(), "name": "ZZB Festivo"}, headers=b
        )
    )
    workday = _created(
        client.post(
            "/api/calendar/workdays",
            json={"employee_id": employee["id"], "work_date": holiday_day.isoformat(), "note": "ZZB guardia"},
            headers=b,
        )
    )
    absence_start = TODAY + timedelta(days=60)
    _created(
        client.post(
            "/api/calendar/absences",
            json={
                "employee_ids": [employee["id"]],
                "type": "VACATION",
                "starts_on": absence_start.isoformat(),
                "ends_on": (absence_start + timedelta(days=2)).isoformat(),
                "note": "ZZB vacaciones",
            },
            headers=b,
        )
    )
    requested = _created(
        client.post(
            "/api/me/absences",
            json={
                "type": "VACATION",
                "starts_on": (absence_start + timedelta(days=10)).isoformat(),
                "ends_on": (absence_start + timedelta(days=11)).isoformat(),
                "note": "ZZB viaje",
            },
            headers=b_employee,
        )
    )
    b_validator = validator_headers(client, b, email="zzbval@empresa.com", name="ZZB Validador")
    validator = client.get("/api/validators", headers=b).json()["data"]["items"][0]
    device = client.get(f"/api/validators/{validator['id']}/devices", headers=b).json()["data"]["items"][0]
    b_key = new_key(client, b, name="ZZB Llave")
    b_qr = qr_content(employee["id"])
    with SessionLocal() as db:
        b_company = db.get(Employee, employee["id"]).company_id
        qr_id = db.scalar(select(func.max(EmployeeQr.id)).where(EmployeeQr.employee_id == employee["id"]))
        enrollment_id = db.scalar(select(FaceEnrollment.id).where(FaceEnrollment.employee_id == employee["id"]))
        answer_id = db.scalar(
            select(func.min(EnrollmentVoiceAnswer.id)).where(EnrollmentVoiceAnswer.enrollment_id == enrollment_id)
        )
        auth_session = db.scalar(select(AuthSession.id).where(AuthSession.user_id == employee["user_id"]))
        # Una llave de acceso (WebAuthn) del administrador de B, directo en la base: la ceremonia real vive en
        # tests/test_passkeys.py; aquí solo importa que un id ajeno responda 404.
        b_admin = db.scalar(select(User.id).where(User.email == "admin@panificadora.com"))
        passkey = Passkey(user_id=b_admin, credential_id="zzb-credencial", public_key="zzb-llave", name="ZZB llave")
        db.add(passkey)
        db.flush()
        passkey_id = passkey.id
        db.commit()
        absence_id = db.scalar(select(EmployeeAbsence.id).where(EmployeeAbsence.note == "ZZB vacaciones"))
    session_id = _work_session(b_company, employee["id"], assignment["id"])
    assert b_validator  # su cuenta y su dispositivo existen (y operan) en B

    # Los actores de A.
    a_employee = approved(client, company_headers, "ana", number="A-001")
    a_department = _created(client.post("/api/departments", json={"name": "Depto A"}, headers=company_headers))
    a_shift = create_shift(client, company_headers, name="Turno A")
    return {
        "a": {
            "company": company_headers,
            "employee": login(client, "ana@empresa.com", "Empleado123"),
            "validator": validator_headers(client, company_headers, email="vala@empresa.com", name="Recepción A"),
            "key": new_key(client, company_headers, name="Llave A")["secret"],
        },
        "a_ids": {"employee": a_employee["id"], "department": a_department["id"], "shift": a_shift["id"]},
        "b_company": b_company,
        "b_qr": b_qr,
        "b_key": b_key,
        "b": {
            "employee_id": employee["id"],
            "user_id": employee["user_id"],
            "department_id": department["id"],
            "enrollment_id": enrollment_id,
            "answer_id": answer_id,
            "validator_id": validator["id"],
            "device_id": device["id"],
            "key_id": b_key["id"],
            "site_id": site["id"],
            "kiosk_id": kiosk["kiosk"]["id"],
            "document_id": document["id"],
            "shift_id": shift["id"],
            "assignment_id": assignment["id"],
            "request_id": request["id"],
            "absence_id": absence_id,
            "requested_absence_id": requested["id"],
            "holiday_id": holiday["id"],
            "workday_id": workday["id"],
            "qr_id": qr_id,
            "work_session_id": session_id,
            "auth_session_id": auth_session,
            "passkey_id": passkey_id,
        },
    }


_REASON = {"reason": "ZZB intento de otra empresa"}
_NOTE = {"note": "No corresponde a esta empresa"}
#: (método, ruta) → (actor de A, cuerpo). `{x}` se llena con el id del dato de B; "action" no es un id.
ROUTES: dict[tuple[str, str], tuple[str, object]] = {
    ("DELETE", "/auth/sessions/{session_id}"): ("company", None),
    ("PATCH", "/auth/passkeys/{passkey_id}"): ("company", {"name": "Intruso"}),
    ("DELETE", "/auth/passkeys/{passkey_id}"): ("company", None),
    ("GET", "/users/me/qr/{qr_id}"): ("employee", None),
    ("GET", "/users/{user_id}/avatar"): ("company", None),
    ("GET", "/employees/{employee_id}"): ("company", None),
    ("PUT", "/employees/{employee_id}"): ("company", {"first_name": "Intruso"}),
    ("PATCH", "/employees/{employee_id}/status"): ("company", {"active": False}),
    ("DELETE", "/employees/{employee_id}"): ("company", None),
    ("POST", "/employees/{employee_id}/restore"): ("company", None),
    ("POST", "/employees/{employee_id}/face/reset"): ("company", {"reason": "Intruso"}),
    ("POST", "/employees/{employee_id}/face/enroll"): ("company", "images"),
    ("POST", "/employees/{employee_id}/face/verify"): ("company", "images"),
    ("GET", "/employees/{employee_id}/qr"): ("company", None),
    ("DELETE", "/employees/{employee_id}/qr"): ("company", None),
    ("GET", "/employees/{employee_id}/verifications"): ("company", None),
    ("GET", "/employees/{employee_id}/devices"): ("company", None),
    ("PATCH", "/employees/{employee_id}/devices/{device_id}/status"): ("company", {"status": "REVOKED"}),
    ("GET", "/departments/{department_id}"): ("company", None),
    ("PUT", "/departments/{department_id}"): ("company", {"name": "Intruso"}),
    ("DELETE", "/departments/{department_id}"): ("company", None),
    ("POST", "/departments/{department_id}/restore"): ("company", None),
    ("POST", "/departments/{department_id}/employees"): ("company", {"employee_id": "a:employee"}),
    ("DELETE", "/departments/{department_id}/employees/{employee_id}"): ("company", None),
    ("POST", "/departments/{department_id}/managers"): ("company", {"employee_id": "a:employee"}),
    ("DELETE", "/departments/{department_id}/managers/{employee_id}"): ("company", None),
    ("GET", "/enrollments/{enrollment_id}"): ("company", None),
    ("GET", "/enrollments/{enrollment_id}/voice/{answer_id}/clip"): ("company", None),
    ("POST", "/enrollments/{enrollment_id}/approve"): ("company", None),
    ("POST", "/enrollments/{enrollment_id}/reject"): ("company", {"reason": "Intruso"}),
    ("GET", "/validators/{validator_id}"): ("company", None),
    ("PUT", "/validators/{validator_id}"): ("company", {"name": "Intruso"}),
    ("PATCH", "/validators/{validator_id}/status"): ("company", {"active": False}),
    ("PUT", "/validators/{validator_id}/password"): ("company", {"password": "Intruso12345"}),
    ("DELETE", "/validators/{validator_id}"): ("company", None),
    ("POST", "/validators/{validator_id}/restore"): ("company", None),
    ("GET", "/validators/{validator_id}/devices"): ("company", None),
    ("PATCH", "/validators/{validator_id}/devices/{device_id}/status"): ("company", {"status": "REVOKED"}),
    ("POST", "/api-keys/{key_id}/rotate"): ("company", None),
    ("DELETE", "/api-keys/{key_id}"): ("company", None),
    ("GET", "/integrations/v1/employees/{employee_id}"): ("key", None),
    ("GET", "/sites/{site_id}"): ("company", None),
    ("PUT", "/sites/{site_id}"): ("company", {"name": "Intruso", "address": address(), "radius_m": 100}),
    ("PATCH", "/sites/{site_id}/status"): ("company", {"active": False}),
    ("DELETE", "/sites/{site_id}"): ("company", None),
    ("POST", "/sites/{site_id}/restore"): ("company", None),
    ("GET", "/sites/{site_id}/kiosks"): ("company", None),
    ("POST", "/sites/{site_id}/kiosks"): ("company", {"name": "Intruso"}),
    ("POST", "/sites/{site_id}/kiosks/{kiosk_id}/pairing"): ("company", None),
    ("DELETE", "/sites/{site_id}/kiosks/{kiosk_id}"): ("company", None),
    ("POST", "/sites/{site_id}/kiosks/{kiosk_id}/restore"): ("company", None),
    ("GET", "/documents/{document_id}/file"): ("company", None),
    ("DELETE", "/documents/{document_id}"): ("company", None),
    ("POST", "/documents/{document_id}/restore"): ("company", None),
    # Documentos de identidad del empleado (onboarding con OCR): el empleado solo los suyos; la empresa, por el
    # expediente de un empleado de su empresa (un empleado de B: 404 EMPLOYEE_NOT_FOUND antes de tocar el documento).
    ("GET", "/me/documents/{document_id}/file"): ("employee", None),
    ("DELETE", "/me/documents/{document_id}"): ("employee", None),
    ("POST", "/me/documents/{document_id}/restore"): ("employee", None),
    ("GET", "/validations/employees/{employee_id}/documents"): ("company", None),
    ("GET", "/validations/employees/{employee_id}/documents/{document_id}/file"): ("company", None),
    ("PATCH", "/validations/employees/{employee_id}/documents/{document_id}/data"): ("company", {}),
    ("GET", "/shifts/{shift_id}"): ("company", None),
    ("PUT", "/shifts/{shift_id}"): ("company", shift_body("Intruso")),
    ("PATCH", "/shifts/{shift_id}/status"): ("company", {"active": False}),
    ("DELETE", "/shifts/{shift_id}"): ("company", None),
    ("POST", "/shifts/{shift_id}/restore"): ("company", None),
    ("GET", "/employees/{employee_id}/shift-assignments"): ("company", None),
    ("POST", "/employees/{employee_id}/shift-assignments"): (
        "company",
        {"shift_id": "a:shift", "valid_from": (TODAY + timedelta(days=5)).isoformat()},
    ),
    ("DELETE", "/shift-assignments/{assignment_id}"): ("company", None),
    ("POST", "/shift-assignments/{assignment_id}/restore"): ("company", None),
    ("POST", "/shift-requests/{request_id}/approve"): ("company", {}),
    ("POST", "/shift-requests/{request_id}/reject"): ("company", _NOTE),
    ("GET", "/attendance/sessions/{session_id}"): ("company", None),
    ("POST", "/attendance/sessions/{session_id}/review"): ("company", {"decision": "CONFIRMED"}),
    ("PUT", "/attendance/sessions/{session_id}"): (
        "company",
        {"check_in": "08:00", "check_out": "16:00", "breaks": [], **_REASON},
    ),
    ("POST", "/me/shift-requests/{request_id}/cancel"): ("employee", None),
    ("DELETE", "/calendar/holidays/{holiday_id}"): ("company", None),
    ("POST", "/calendar/holidays/{holiday_id}/restore"): ("company", None),
    ("POST", "/calendar/absences/{absence_id}/approve"): ("company", None),
    ("POST", "/calendar/absences/{absence_id}/reject"): ("company", _NOTE),
    ("POST", "/calendar/absences/{absence_id}/cancel"): ("company", None),
    ("DELETE", "/calendar/workdays/{workday_id}"): ("company", None),
    ("POST", "/calendar/workdays/{workday_id}/restore"): ("company", None),
    ("POST", "/me/absences/{absence_id}/cancel"): ("employee", None),
}
#: Rutas con un parámetro en la ruta que NO es un id de un dato (no aplica la matriz).
NOT_AN_ID = {("POST", "/me/attendance/{action}")}


def _routes_with_ids() -> set[tuple[str, str]]:
    found = set()
    for router in API_ROUTERS:
        for route in router.routes:
            if isinstance(route, APIRoute) and "{" in route.path and not route.path.startswith("/admin/"):
                found |= {(method, route.path) for method in route.methods}
    return found


def test_every_route_with_an_id_is_in_the_matrix():
    """Las rutas del ADMIN cruzan empresas a propósito (la plataforma); las demás con un id, todas aquí."""
    assert _routes_with_ids() - set(ROUTES) - NOT_AN_ID == set()


def _url(path: str, b: dict) -> str:
    ids = {
        **b,
        "session_id": b["auth_session_id"] if path.startswith("/auth/") else b["work_session_id"],
        "absence_id": b["requested_absence_id"] if path.startswith("/me/") else b["absence_id"],
    }
    return "/api" + re.sub(r"\{(\w+)\}", lambda m: str(ids[m.group(1)]), path)


def _send(client, method: str, url: str, headers: dict, body: object, a_ids: dict):
    if body == "images":
        files = [("images", (f"f{i}.jpg", b"face:ana", "image/jpeg")) for i in range(3)]
        return client.request(method, url, files=files, headers=headers)
    if isinstance(body, dict):
        body = {k: a_ids[v[2:]] if isinstance(v, str) and v.startswith("a:") else v for k, v in body.items()}
        return client.request(method, url, json=body, headers=headers)
    return client.request(method, url, headers=headers)


def test_an_id_of_another_company_is_never_found(client, tenants):
    a, b = tenants["a"], tenants["b"]
    leaks = {}
    for (method, path), (actor, body) in ROUTES.items():
        headers = {"X-API-Key": a["key"]} if actor == "key" else a[actor]
        response = _send(client, method, _url(path, b), headers, body, tenants["a_ids"])
        if response.status_code != 404:
            leaks[f"{method} {path}"] = (response.status_code, response.text[:160])
    assert leaks == {}


def test_ids_of_another_company_inside_the_body_are_not_found(client, tenants):
    company, b, a_ids = tenants["a"]["company"], tenants["b"], tenants["a_ids"]
    later = (TODAY + timedelta(days=90)).isoformat()
    attempts = {
        "asignar un empleado de B a un departamento de A": client.post(
            f"/api/departments/{a_ids['department']}/employees", json={"employee_id": b["employee_id"]}, headers=company
        ),
        "responsable de B en un departamento de A": client.post(
            f"/api/departments/{a_ids['department']}/managers", json={"employee_id": b["employee_id"]}, headers=company
        ),
        "turno de B a un empleado de A": assign(client, company, a_ids["employee"], b["shift_id"], TODAY),
        "turno de A a un empleado de B (masivo)": client.post(
            "/api/shift-assignments/bulk",
            json={"employee_ids": [b["employee_id"]], "shift_id": a_ids["shift"], "valid_from": later},
            headers=company,
        ),
        "vacaciones a un empleado de B": client.post(
            "/api/calendar/absences",
            json={"employee_ids": [b["employee_id"]], "type": "VACATION", "starts_on": later, "ends_on": later},
            headers=company,
        ),
        "día laborable de un empleado de B": client.post(
            "/api/calendar/workdays", json={"employee_id": b["employee_id"], "work_date": later}, headers=company
        ),
        "jornada de un empleado de B": client.post(
            "/api/attendance/sessions",
            json={"employee_id": b["employee_id"], "work_date": TODAY.isoformat(), "check_in": "08:00", **_REASON},
            headers=company,
        ),
    }
    assert {name: r.status_code for name, r in attempts.items() if r.status_code != 404} == {}
    # Un sitio de otra empresa en un turno: el mismo 422 que un sitio que no existe (nada dice que existe en otra).
    foreign = client.post("/api/shifts", json=shift_body("Con sitio ajeno", sites=[b["site_id"]]), headers=company)
    missing = client.post("/api/shifts", json=shift_body("Con sitio ajeno", sites=[10**6]), headers=company)
    assert foreign.status_code == missing.status_code == 422
    assert foreign.json()["code"] == missing.json()["code"] and foreign.json()["message"] == missing.json()["message"]


def _get_routes_without_ids() -> list[str]:
    paths = set()
    for router in API_ROUTERS:
        for route in router.routes:
            listed = isinstance(route, APIRoute) and "GET" in route.methods and "{" not in route.path
            if listed and not route.path.startswith(("/admin/", "/health", "/auth/jwks")):
                paths.add(route.path)
    return sorted(paths)


#: Retos al azar que emite el servidor (antifraude 2b: el de la firma del validador): no son datos de una empresa y, por
#: azar, su texto puede contener la marca de B.
SERVER_TOKENS = frozenset({"device_nonce"})


def _without_server_tokens(body: object) -> str:
    """El cuerpo en minúsculas sin los retos que emite el servidor."""

    def clean(value: object) -> object:
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items() if key not in SERVER_TOKENS}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    return json.dumps(clean(body), ensure_ascii=False).lower()


def test_no_list_count_board_search_or_export_of_a_shows_data_of_b(client, tenants):
    a = tenants["a"]
    seen = {}
    for path in _get_routes_without_ids():
        actors = {"key": {"X-API-Key": a["key"]}} if path.startswith("/integrations/") else a
        for actor, headers in actors.items():
            if actor == "key" and not path.startswith("/integrations/"):
                continue
            for params in ({}, {"search": MARK}, {"size": 50}):
                response = client.get(f"/api{path}", params=params, headers=headers)
                assert response.status_code != 500, (path, response.text[:200])
                if response.status_code == 200 and MARK in _without_server_tokens(response.json()):
                    seen[f"{actor} GET {path} {params}"] = response.text[:200]
    assert seen == {}
    # Ni siquiera el CONTEO delata lo de B: buscar lo que solo B tiene da cero (también en los ids "seleccionar todos").
    company = a["company"]
    for path in ("/api/employees", "/api/employees/ids"):
        assert client.get(path, params={"search": "ZZB-001"}, headers=company).json()["data"]["total"] == 0
    assert client.get("/api/employees", params={"search": "zzbana"}, headers=company).json()["data"]["total"] == 0


def test_the_trash_of_a_never_shows_or_restores_what_b_deleted(client, tenants):
    """Borrado lógico (regla 20): lo que B mandó a «Eliminados» no aparece en ninguna papelera de A y A no lo puede
    restaurar (404, igual que un id que no existe), aunque sea un id válido de la otra empresa."""
    b, company = tenants["b"], tenants["a"]["company"]
    b_headers = login(client, "admin@panificadora.com", "Empresa1234")
    deleted = {
        "/api/calendar/workdays/{}": b["workday_id"],
        "/api/calendar/holidays/{}": b["holiday_id"],
        "/api/validators/{}": b["validator_id"],
        "/api/employees/{}": b["employee_id"],
    }
    for url, record_id in deleted.items():
        assert client.delete(url.format(record_id), headers=b_headers).status_code == 200, url
    # Cada papelera de A (la llave de la API se omite: su prefijo es aleatorio y podría contener la marca).
    trash: tuple[tuple[str, dict], ...] = (
        ("/employees", {}),
        ("/validators", {}),
        ("/departments", {}),
        ("/sites", {}),
        ("/shifts", {}),
        ("/calendar/holidays", {"year": (TODAY + timedelta(days=40)).year}),
        ("/calendar/workdays", {}),
        (f"/employees/{tenants['a_ids']['employee']}/shift-assignments", {}),
    )
    for path, params in trash:
        response = client.get(f"/api{path}", params={"deleted": "true", "size": 50, **params}, headers=company)
        assert response.status_code == 200 and response.json()["data"]["total"] == 0, (path, response.text[:200])
        assert MARK not in response.text.lower(), path
    for url, record_id in deleted.items():
        assert client.post(url.format(record_id) + "/restore", headers=company).status_code == 404, url
        assert client.get(url.format(record_id), headers=company).status_code in (404, 405), url


def test_integration_key_of_a_never_reaches_b(client, tenants):
    a_key, b = tenants["a"]["key"], tenants["b"]
    assert call(client, a_key, f"/employees/{b['employee_id']}").status_code == 404
    assert call(client, a_key, "/attendance", employee_id=b["employee_id"]).status_code == 404
    assert call(client, a_key, "/attendance/feed", employee_id=b["employee_id"]).status_code == 404
    # La verificación facial de la aplicación móvil (SDK): el empleado de B no existe para la llave de A.
    from tests.test_verification_api import attempt

    crossed = attempt(client, a_key, reference={"employee_id": str(b["employee_id"])})
    assert crossed.status_code == 404 and crossed.json()["code"] == "EMPLOYEE_NOT_FOUND"
    # Y la llave de B, con la de A en la mano, nunca ve lo de A (la empresa sale de la llave, no del cliente).
    mine = call(client, tenants["b_key"]["secret"], "/employees").json()["data"]["items"]
    assert {item["employee_number"] for item in mine} == {"ZZB-001"}


def test_validator_and_live_validation_of_a_do_not_see_b(client, tenants):
    a = tenants["a"]
    scanned = client.post("/api/checkpoint/identify/qr", json={"qr_content": tenants["b_qr"]}, headers=a["validator"])
    # Sin los retos al azar del servidor (`device_nonce`): por azar su texto puede contener la marca de B.
    assert scanned.status_code == 200 and scanned.json()["data"]["verified"] is False
    assert MARK not in _without_server_tokens(scanned.json())
    inspected = client.post("/api/checkpoint/qr/inspect", json={"qr_content": tenants["b_qr"]}, headers=a["validator"])
    unknown = client.post(
        "/api/checkpoint/qr/inspect", json={"qr_content": "TCQR2:" + "x" * 40}, headers=a["validator"]
    )
    # El QR de B es, para el validador de A, igual a uno que no existe (mismo código y mensaje; nada de B).
    assert inspected.status_code == unknown.status_code and inspected.json()["code"] == unknown.json()["code"]
    assert MARK not in _without_server_tokens(inspected.json())
    # El QR de B sigue sirviendo en B: el intento de A no lo consumió.
    with SessionLocal() as db:
        assert db.scalar(select(EmployeeQr.used_at).where(EmployeeQr.id == tenants["b"]["qr_id"])) is None
    # El número de empleado es único POR EMPRESA: en A está libre aunque B lo use.
    available = client.get(
        "/api/validation", params={"field": "employee_number", "value": "ZZB-001"}, headers=a["company"]
    )
    assert available.json()["code"] == "AVAILABLE"


def test_a_never_sees_a_profile_photo_of_the_people_of_b(client, tenants):
    """La empresa ve las fotos de SU gente (decisión del dueño, 2026-10-06), nunca las de otra: el administrador, el
    validador y el empleado de B —también inactivo— responden a cada actor de A 404 (igual que sin foto) y ningún
    listado de A trae la ruta de sus fotos."""
    b_admin = login(client, "admin@panificadora.com", "Empresa1234")
    urls = {me(client, login(client, "zzbana@empresa.com", "Empleado123"))["avatar"]}
    for headers in (b_admin, login(client, "zzbval@empresa.com", PASSWORD)):
        urls.add(upload(client, headers).json()["data"]["avatar"])
    inactive = {"active": False}
    assert client.patch(
        f"/api/employees/{tenants['b']['employee_id']}/status", json=inactive, headers=b_admin
    ).is_success
    actors = {name: headers for name, headers in tenants["a"].items() if name != "key"}
    for url in urls:
        assert url and fetch(client, b_admin, url).status_code == 200  # B sí la ve
        for name, headers in actors.items():
            assert fetch(client, headers, url).json()["code"] == "AVATAR_NOT_FOUND", (name, url)
    paths = {url.split("?")[0] for url in urls}
    for path in _get_routes_without_ids():
        for name, headers in actors.items():
            response = client.get(f"/api{path}", params={"size": 50}, headers=headers)
            assert not any(photo in response.text for photo in paths), (name, path)


def test_the_database_refuses_to_link_rows_of_two_companies(tenants):
    """Las llaves foráneas compuestas: aunque el código se equivocara, la base no acepta la mezcla. (SQLite no crea
    FK entre esquemas: ahí solo se revisan las del mismo esquema; PostgreSQL, todas.)"""
    b = tenants["b"]
    with SessionLocal() as db:
        a_company = db.get(Employee, tenants["a_ids"]["employee"]).company_id
    cross_schema = (FaceEmbedding, VerificationLog)
    mixes = [
        EmployeeQr(employee_id=b["employee_id"], company_id=a_company, token_hash="9" * 64),
        FaceEmbedding(
            employee_id=b["employee_id"],
            company_id=a_company,
            embedding_encrypted=b"x",
            model_name="x",
            dimension=1,
            detection_score=1.0,
            quality_score=1.0,
        ),
        WorkBreak(company_id=a_company, session_id=b["work_session_id"], started_at=NOW),
        VerificationLog(company_id=a_company, employee_id=b["employee_id"], method="FACE", success=False),
        ValidatorDevice(
            validator_id=b["validator_id"], company_id=a_company, key_hash="8" * 64, public_key="k", name="x"
        ),
        ShiftAssignment(
            company_id=a_company, employee_id=b["employee_id"], shift_id=tenants["a_ids"]["shift"], valid_from=TODAY
        ),
    ]
    for row in mixes:
        if engine.dialect.name == "sqlite" and isinstance(row, cross_schema):
            continue
        with SessionLocal() as db:
            db.add(row)
            with pytest.raises((IntegrityError, DBAPIError)):
                db.commit()


postgresql_only = pytest.mark.skipif(
    engine.dialect.name != "postgresql", reason="La seguridad por fila es de PostgreSQL (quality.sh --postgres)"
)


@postgresql_only
def test_row_security_is_the_last_barrier(tenants):
    """Con el usuario de la API: una consulta deliberadamente SIN filtro de empresa solo ve la empresa de su
    transacción; sin empresa no ve nada; no puede escribir en otra; la plataforma ve todas."""
    b_company = tenants["b_company"]
    with SessionLocal() as db:
        a_company = db.get(Employee, tenants["a_ids"]["employee"]).company_id
    with SessionLocal(info={SCOPE_KEY: None}) as db:
        assert db.scalar(select(func.count()).select_from(Employee)) == 0
        assert db.scalar(select(func.count()).select_from(WorkSession)) == 0
    with SessionLocal(info={SCOPE_KEY: a_company}) as db:
        assert set(db.scalars(select(Employee.company_id))) == {a_company}  # sin WHERE a propósito
        assert db.scalar(select(func.count()).where(VerificationLog.company_id == b_company)) == 0
        db.add(Department(company_id=b_company, name="Intruso"))
        with pytest.raises(DBAPIError, match="row-level security"):
            db.commit()
    with SessionLocal(info={SCOPE_KEY: PLATFORM}) as db:
        assert set(db.scalars(select(Employee.company_id))) == {a_company, b_company}


@postgresql_only
def test_the_api_role_has_least_privilege():
    with engine.connect() as conn:
        role = conn.execute(
            text("SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = current_user")
        ).one()
        assert tuple(role) == (False, False, False, False)
        with pytest.raises(DBAPIError, match="permission denied"):
            conn.execute(text("CREATE TABLE workforce.intruso (id int)"))
        conn.rollback()
        # Una partición no se lee directo (solo por su tabla padre, donde vive la política).
        with pytest.raises(DBAPIError, match="permission denied"):
            conn.execute(text("SELECT count(*) FROM attendance.verification_logs_default"))
        conn.rollback()
        # La función de particiones solo la ejecuta el rol de la plataforma.
        with pytest.raises(DBAPIError, match="permission denied"):
            conn.execute(text("SELECT * FROM ops.ensure_partitions('ops.usage_users', CAST(now() AS date), 1, NULL)"))


def test_a_kiosk_of_b_is_never_reached_from_a_site_of_a(client, tenants):
    """Antifraude 2b: un kiosco de B no se alcanza ni con un sitio de A (404 igual que uno que no existe), ninguna
    papelera ni conteo de A lo muestra y su sitio no cuenta kioscos de B."""
    company, b = tenants["a"]["company"], tenants["b"]
    own = _created(
        client.post("/api/sites", json={"name": "Sitio A", "address": address(), "radius_m": 100}, headers=company)
    )
    base = f"/api/sites/{own['id']}/kiosks/{b['kiosk_id']}"
    for method, url in (("POST", f"{base}/pairing"), ("DELETE", base), ("POST", f"{base}/restore")):
        response = client.request(method, url, headers=company)
        assert response.status_code == 404 and response.json()["code"] == "KIOSK_NOT_FOUND", url
    for deleted in ("false", "true"):
        listed = client.get(f"/api/sites/{own['id']}/kiosks", params={"deleted": deleted}, headers=company)
        assert listed.json()["data"]["total"] == 0 and MARK not in listed.text.lower()
    sites = client.get("/api/sites", headers=company).json()["data"]["items"]
    assert [site["kiosks"] for site in sites] == [0]
