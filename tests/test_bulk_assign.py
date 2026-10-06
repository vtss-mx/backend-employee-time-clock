"""Asignar el mismo turno a varios empleados a la vez (con las reglas de asignar a uno) y elegirlos de la
lista de empleados con sus filtros."""

from datetime import timedelta

import pytest

from app.core.clock import business_today
from app.core.database import SessionLocal
from app.models import Employee
from tests.conftest import create_company, create_employee, login
from tests.test_shifts import assign, create_shift, create_site

BULK = "/api/shift-assignments/bulk"
TODAY = business_today()


@pytest.fixture
def crew(client, company_headers) -> list[int]:
    """Cuatro empleados de la empresa (sin rostro: la asignación no lo necesita)."""
    ids = []
    for number, email in (("EMP-001", "juan"), ("EMP-002", "ana"), ("EMP-003", "beto"), ("EMP-004", "carla")):
        created = create_employee(client, company_headers, number=number, email=f"{email}@empresa.com")
        assert created.status_code == 201, created.text
        ids.append(created.json()["data"]["id"])
    return ids


def bulk_body(shift_id: int, employee_ids, *, valid_from=TODAY) -> dict:
    """Solo el turno, desde cuándo y a quiénes: dónde checan lo dice el turno."""
    return {"shift_id": shift_id, "employee_ids": list(employee_ids), "valid_from": valid_from.isoformat()}


def test_one_shift_for_several_employees(client, company_headers, crew):
    juan, ana, beto, carla = crew
    site = create_site(client, company_headers)
    morning = create_shift(client, company_headers, sites=[site["id"]])
    night = create_shift(client, company_headers, name="Nocturno", start_time="22:00", end_time="06:00")
    vespertino = create_shift(client, company_headers, name="Vespertino", start_time="14:00", end_time="22:00")
    # Ana ya tiene turno (el cambio requiere un día de anticipación); Beto, un cambio programado; Carla, inactiva.
    assert assign(client, company_headers, ana, night["id"], TODAY).status_code == 201
    assert assign(client, company_headers, beto, night["id"], TODAY).status_code == 201
    assert assign(client, company_headers, beto, vespertino["id"], TODAY + timedelta(days=3)).status_code == 201
    with SessionLocal() as db:
        db.get(Employee, carla).active = False  # type: ignore[union-attr]
        db.commit()

    response = client.post(BULK, json=bulk_body(morning["id"], crew), headers=company_headers)
    assert response.status_code == 200 and response.json()["code"] == "SHIFT_BULK_ASSIGNED"
    result = response.json()["data"]
    assert (result["done"], result["unchanged"], result["skipped"]) == (1, 0, 3)
    outcomes = {r["employee"]["id"]: (r["result"], r["code"]) for r in result["results"]}
    assert outcomes == {
        juan: ("DONE", None),
        ana: ("SKIPPED", "ASSIGNMENT_NOTICE_REQUIRED"),
        beto: ("SKIPPED", "ASSIGNMENT_NOTICE_REQUIRED"),
        carla: ("SKIPPED", "EMPLOYEE_INACTIVE"),
    }
    # El resultado viene por nombre (apellidos, nombre) y cada omitido trae su motivo.
    assert all(r["message"] for r in result["results"] if r["result"] == "SKIPPED")

    # Desde mañana: a Ana se le programa el cambio; Beto ya tiene uno programado; Juan ya lo tiene igual.
    tomorrow = bulk_body(morning["id"], [juan, ana, beto], valid_from=TODAY + timedelta(days=1))
    later = client.post(BULK, json=tomorrow, headers=company_headers).json()["data"]
    outcomes = {r["employee"]["id"]: (r["result"], r["code"]) for r in later["results"]}
    assert outcomes == {
        juan: ("UNCHANGED", None),  # rige desde hoy, idéntica: nada que hacer
        ana: ("DONE", None),
        beto: ("SKIPPED", "ASSIGNMENT_ALREADY_SCHEDULED"),
    }
    # Un reintento de la red no duplica ni falla.
    retry = client.post(BULK, json=tomorrow, headers=company_headers).json()["data"]
    assert (retry["done"], retry["unchanged"], retry["skipped"]) == (0, 2, 1)
    history = client.get(f"/api/employees/{ana}/shift-assignments", headers=company_headers).json()["data"]
    assert [a["shift"]["name"] for a in history["items"]] == ["Matutino", "Nocturno"]
    assert history["items"][1]["valid_to"] == TODAY.isoformat()  # la anterior termina el día antes
    assert [s["name"] for s in history["items"][0]["shift"]["sites"]] == ["Planta Norte"]


def test_rules_that_apply_to_everyone_reject_the_request(client, company_headers, crew, admin_headers):
    site = create_site(client, company_headers)
    shift = create_shift(client, company_headers, sites=[site["id"]])
    for body, status, code in (
        (bulk_body(999, crew[:1]), 404, "SHIFT_NOT_FOUND"),
        (bulk_body(shift["id"], crew[:1], valid_from=TODAY - timedelta(days=1)), 422, "ASSIGNMENT_IN_PAST"),
        (bulk_body(shift["id"], [crew[0], crew[0]]), 422, "VALIDATION_ERROR"),
        (bulk_body(shift["id"], []), 422, "VALIDATION_ERROR"),
        (bulk_body(shift["id"], range(1, 502)), 422, "VALIDATION_ERROR"),
        (bulk_body(shift["id"], [crew[0], 999]), 404, "EMPLOYEE_NOT_FOUND"),
    ):
        response = client.post(BULK, json=body, headers=company_headers)
        assert (response.status_code, response.json()["code"]) == (status, code), body
    client.patch(f"/api/shifts/{shift['id']}/status", json={"active": False}, headers=company_headers)
    inactive = client.post(BULK, json=bulk_body(shift["id"], crew[:1]), headers=company_headers)
    assert inactive.json()["code"] == "SHIFT_INACTIVE"
    # Nada se asignó a nadie.
    assert (
        client.get(f"/api/employees/{crew[0]}/shift-assignments", headers=company_headers).json()["data"]["total"] == 0
    )

    # Empleados de otra empresa: 404, como si no existieran.
    assert create_company(client, admin_headers).status_code == 201
    other = login(client, "admin@panificadora.com", "Empresa1234")
    other_shift = create_shift(client, other)
    foreign = client.post(BULK, json=bulk_body(other_shift["id"], crew[:2]), headers=other)
    assert foreign.status_code == 404 and foreign.json()["errors"][0]["details"] == {"employee_ids": sorted(crew[:2])}


def test_assigning_one_employee_again_is_idempotent(client, company_headers, crew):
    shift = create_shift(client, company_headers)
    first = assign(client, company_headers, crew[0], shift["id"], TODAY)
    again = assign(client, company_headers, crew[0], shift["id"], TODAY)
    assert again.status_code == 201 and again.json()["data"]["id"] == first.json()["data"]["id"]
    other = create_shift(client, company_headers, name="Vespertino", start_time="14:00", end_time="22:00")
    different = assign(client, company_headers, crew[0], other["id"], TODAY)
    assert different.status_code == 422 and different.json()["code"] == "ASSIGNMENT_NOTICE_REQUIRED"


# ---------------------------------------------------------------- elegir empleados


def test_select_all_employees_of_a_filter(client, company_headers, crew):
    department = client.post("/api/departments", json={"name": "Producción"}, headers=company_headers).json()["data"]
    for employee_id in crew[:2]:
        client.post(
            f"/api/departments/{department['id']}/employees", json={"employee_id": employee_id}, headers=company_headers
        )
    everyone = client.get("/api/employees/ids", headers=company_headers).json()
    assert everyone["code"] == "EMPLOYEE_IDS" and everyone["data"]["total"] == 4 and everyone["data"]["limit"] == 500
    assert sorted(everyone["data"]["ids"]) == sorted(crew)
    produccion = client.get(
        "/api/employees/ids", params={"department_id": department["id"]}, headers=company_headers
    ).json()["data"]
    assert sorted(produccion["ids"]) == sorted(crew[:2]) and produccion["total"] == 2
    searched = client.get(
        "/api/employees/ids", params={"search": "EMP-003", "active": True}, headers=company_headers
    ).json()["data"]
    assert searched["ids"] == [crew[2]]
    # La misma lista (con su filtro de departamento) y los departamentos se leen desde Turnos y Calendario.
    page = client.get("/api/employees", params={"department_id": department["id"]}, headers=company_headers).json()[
        "data"
    ]
    assert page["total"] == 2
    assert client.get("/api/departments", headers=company_headers).json()["data"]["total"] == 1


def test_the_ids_are_bounded(client, company_headers, crew, monkeypatch):
    from app.services import employee_service

    monkeypatch.setattr(employee_service, "BULK_MAX", 2)
    capped = client.get("/api/employees/ids", headers=company_headers).json()["data"]
    assert len(capped["ids"]) == 2 and capped["total"] == 4 and capped["limit"] == 2
