"""Cada persona que muestra una respuesta lleva su foto de perfil (`avatar`, la ruta versionada) para que la app la
dibuje en lugar de las iniciales (decisión del dueño, 2026-10-06: «hay tablas que muestran iconos con iniciales; ahí se
deben cargar las imágenes de los usuarios cuando tengan»). La versión viaja con la cuenta que la consulta ya carga (sin
consultas de más: `tests/test_performance.py`); la integración no la recibe."""

import json
from datetime import timedelta

from sqlalchemy import select

from app.core.clock import business_today
from app.core.database import SessionLocal
from app.models import User
from app.services.department_service import DepartmentService
from app.services.usage_meter import UsageMeter
from tests.avatar_support import fetch, upload
from tests.conftest import create_employee, login, qr_content
from tests.test_api_keys import call, new_key
from tests.test_attendance import server_clock
from tests.test_attendance_calendar import remote_worker
from tests.test_error_reports import _break, _reports
from tests.test_fraud_cases import open_case
from tests.test_shifts import create_shift
from tests.test_validators import validator_headers


def _company_user() -> User:
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == "admin@empresa.com"))
        assert user is not None
        return user


def test_every_list_of_the_company_carries_the_photo_of_each_person(client, company_headers, monkeypatch):
    clock = server_clock(monkeypatch)
    worker = remote_worker(client, company_headers)
    ana, day = worker["id"], worker["day"]
    url = upload(client, worker["headers"]).json()["data"]["avatar"]
    assert url

    # Tablero del día (su consulta no carga las cuentas: la foto llega en una consulta aparte) y jornadas.
    board = client.get("/api/attendance/board", params={"date": day.isoformat()}, headers=company_headers).json()
    assert board["data"]["items"][0]["employee"]["avatar"] == url
    clock(day, "18:00")
    manual = {"employee_id": ana, "work_date": day.isoformat(), "check_in": "08:05", "reason": "Olvidó checar"}
    created = client.post("/api/attendance/sessions", json=manual, headers=company_headers)
    assert created.status_code == 201 and created.json()["data"]["employee"]["avatar"] == url
    sessions = client.get("/api/attendance/sessions", headers=company_headers).json()["data"]["items"]
    assert sessions[0]["employee"]["avatar"] == url

    # Calendario: la operación masiva, las ausencias y los días laborables.
    start = business_today() + timedelta(days=20)
    absence = {"employee_ids": [ana], "type": "VACATION", "starts_on": start.isoformat(), "ends_on": start.isoformat()}
    bulk = client.post("/api/calendar/absences", json=absence, headers=company_headers).json()["data"]
    assert bulk["results"][0]["employee"]["avatar"] == url
    absences = client.get("/api/calendar/absences", headers=company_headers).json()["data"]["items"]
    assert absences[0]["employee"]["avatar"] == url
    festive = (start + timedelta(days=5)).isoformat()
    assert client.post(
        "/api/calendar/holidays", json={"holiday_date": festive, "name": "Fiesta"}, headers=company_headers
    )
    workday = {"employee_id": ana, "work_date": festive}
    assert client.post("/api/calendar/workdays", json=workday, headers=company_headers).status_code == 201
    workdays = client.get("/api/calendar/workdays", headers=company_headers).json()["data"]["items"]
    assert workdays[0]["employee"]["avatar"] == url

    # Solicitud de cambio de turno.
    night = create_shift(client, company_headers, name="Nocturno", start_time="22:00", end_time="06:00")
    asked = {
        "shift_id": night["id"],
        "valid_from": (business_today() + timedelta(days=3)).isoformat(),
        "reason": "Cambio",
    }
    assert client.post("/api/me/shift-requests", json=asked, headers=worker["headers"]).status_code == 201
    requests = client.get("/api/shift-requests", headers=company_headers).json()["data"]["items"]
    assert requests[0]["employee"]["avatar"] == url

    # Responsables de un departamento.
    department = client.post("/api/departments", json={"name": "Ventas"}, headers=company_headers).json()["data"]
    managers = f"/api/departments/{department['id']}/managers"
    assert client.post(managers, json={"employee_id": ana}, headers=company_headers).status_code in (200, 201)
    listed = client.get("/api/departments", headers=company_headers).json()["data"]["items"]
    assert listed[0]["managers"][0]["avatar"] == url

    # Registro facial: la bandeja (historial de aprobados) y su detalle llevan la foto de PERFIL.
    enrollments = client.get("/api/enrollments", params={"status": "APPROVED"}, headers=company_headers).json()
    enrollment = enrollments["data"]["items"][0]
    assert enrollment["avatar"] == url
    detail = client.get(f"/api/enrollments/{enrollment['id']}", headers=company_headers).json()["data"]
    assert detail["avatar"] == url

    # Validadores: la foto de su cuenta.
    validator = validator_headers(client, company_headers, mode="QR_AND_FACE")
    validator_url = upload(client, validator).json()["data"]["avatar"]
    validators = client.get("/api/validators", headers=company_headers).json()["data"]["items"]
    assert [v["avatar"] for v in validators] == [validator_url]


def test_the_validator_sees_the_photo_of_whoever_it_identifies(client, company_headers, monkeypatch):
    """El validador es una cuenta de la empresa: «la empresa ve a su gente» también aplica al validador."""
    worker = remote_worker(client, company_headers)
    url = upload(client, worker["headers"]).json()["data"]["avatar"]
    two_steps = validator_headers(client, company_headers, mode="QR_AND_FACE")
    inspected = client.post(
        "/api/checkpoint/qr/inspect", json={"qr_content": qr_content(worker["id"])}, headers=two_steps
    )
    assert inspected.status_code == 200 and inspected.json()["data"]["avatar"] == url
    scanner = validator_headers(client, company_headers, mode="QR", email="caseta@empresa.com")
    identified = client.post(
        "/api/checkpoint/identify/qr", json={"qr_content": qr_content(worker["id"])}, headers=scanner
    ).json()["data"]
    assert identified["verified"] is True and identified["avatar"] == url
    assert fetch(client, scanner, url).status_code == 200
    recent = client.get("/api/checkpoint/recent", headers=scanner).json()["data"]["items"]
    assert recent[0]["avatar"] == url

    # La integración NO recibe fotos (sus esquemas las dejan fuera): ni en su bitácora, ni en sus empleados, ni en sus
    # validadores (aunque todos tengan foto).
    upload(client, scanner)
    secret = new_key(client, company_headers)["secret"]
    for path in ("/attendance", "/employees", f"/employees/{worker['id']}", "/validators"):
        response = call(client, secret, path)
        assert response.status_code == 200 and "avatar" not in json.dumps(response.json()), path


def test_every_screen_of_the_admin_carries_the_photo_of_each_person(
    client, company_headers, admin_headers, monkeypatch
):
    """El ADMIN ve la foto de todos (decisión del dueño, 2026-10-06): empleados de una empresa, sus administradores,
    el consumo por cuenta, quién provocó un error y el empleado de un caso de fraude."""
    company_url = upload(client, company_headers).json()["data"]["avatar"]
    company = _company_user()
    employee = create_employee(client, company_headers, number="EMP-300", email="eva@empresa.com").json()["data"]
    eva = login(client, "eva@empresa.com", "Empleado123")
    eva_url = upload(client, eva).json()["data"]["avatar"]
    base = f"/api/admin/companies/{company.company_id}"

    employees = client.get(f"{base}/employees", headers=admin_headers).json()["data"]["items"]
    assert {e["id"]: e["avatar"] for e in employees}[employee["id"]] == eva_url
    admins = client.get(f"{base}/admins", headers=admin_headers).json()["data"]["items"]
    assert [a["avatar"] for a in admins] == [company_url]
    admin = client.get(f"{base}/admins/{company.id}", headers=admin_headers).json()["data"]
    assert admin["avatar"] == company_url and "avatar_version" not in admin
    assert fetch(client, admin_headers, eva_url).status_code == 200

    # Consumo por cuenta: la de la empresa (con foto) y una que ya no existe (sin foto).
    meter = UsageMeter(1000)
    hit = {"bytes_in": 10, "bytes_out": 10, "duration_ms": 5.0, "status": 200, "day": business_today()}
    meter.record(company_id=company.company_id, user_id=company.id, route="GET /api/employees", **hit)
    meter.record(company_id=company.company_id, user_id=987654, route="GET /api/shifts", **hit)
    meter.flush()
    usage = client.get(f"/api/admin/usage/companies/{company.company_id}/users", headers=admin_headers).json()
    assert {u["user_id"]: u["avatar"] for u in usage["data"]["items"]} == {company.id: company_url, 987654: None}

    # Quién provocó un error: su foto junto a «correo (Rol)».
    _break(monkeypatch, DepartmentService, "list_departments", RuntimeError, "la consulta de departamentos se cayó")
    assert client.get("/api/departments", headers=company_headers).status_code == 500
    report = next(r for r in _reports(client, admin_headers) if r["location"] == "/api/departments")
    occurrence = client.get(f"/api/admin/errors/{report['id']}/occurrences", headers=admin_headers).json()
    assert occurrence["data"]["items"][0]["user_avatar"] == company_url


def test_a_fraud_case_shows_the_photo_of_the_employee(client, company_headers, admin_headers):
    headers, _ = open_case(client, company_headers)
    url = upload(client, headers).json()["data"]["avatar"]
    listed = client.get("/api/admin/fraud-cases", headers=admin_headers).json()["data"]["items"]
    assert listed[0]["employee"]["avatar"] == url
    assert fetch(client, admin_headers, url).status_code == 200
