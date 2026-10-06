"""Borrado lógico de los registros de la empresa (regla 20 de la raíz): departamentos, sitios, turnos, cambios de turno
programados, festivos y días laborables. Cada uno va a «Eliminados», sale de sus listados y de las reglas (un festivo
eliminado deja de ser día libre), libera su dato único y se restaura revisando de nuevo sus reglas."""

from datetime import timedelta

from sqlalchemy import select

from app.core.clock import business_today
from app.core.database import SessionLocal
from app.core.soft_delete import WITH_DELETED, with_deleted
from app.models import Employee, ShiftAssignment, ShiftChangeRequest
from tests.conftest import COMPANY_EMAIL, create_employee, login
from tests.test_shifts import SHIFTS, SITES, assign, create_shift, create_site, shift_body
from tests.test_validators import approved

DEPARTMENTS = "/api/departments"
CAL = "/api/calendar"
TODAY = business_today()


def _trash(client, url: str, headers: dict, **params) -> list[dict]:
    response = client.get(url, params={"deleted": "true", **params}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]["items"]


# ---------------------------------------------------------------- departamentos


def test_departments_go_to_the_trash_and_come_back(client, company_headers):
    sales = client.post(DEPARTMENTS, json={"name": "Ventas"}, headers=company_headers).json()["data"]
    url = f"{DEPARTMENTS}/{sales['id']}"
    ana = create_employee(client, company_headers).json()["data"]
    members = f"{url}/employees"
    assert client.post(members, json={"employee_id": ana["id"]}, headers=company_headers).status_code == 200
    blocked = client.delete(url, headers=company_headers).json()
    assert blocked["code"] == "DEPARTMENT_HAS_EMPLOYEES"
    # Un empleado en «Eliminados» no lo detiene: queda sin departamento (así se puede depurar después).
    assert client.delete(f"/api/employees/{ana['id']}", headers=company_headers).status_code == 200
    assert client.post(f"{url}/managers", json={"employee_id": ana["id"]}, headers=company_headers).status_code == 404
    assert client.delete(url, headers=company_headers).json()["code"] == "DEPARTMENT_DELETED"
    with SessionLocal() as db:
        assert db.scalar(with_deleted(select(Employee.department_id).where(Employee.id == ana["id"]))) is None
    assert client.get(DEPARTMENTS, headers=company_headers).json()["data"]["total"] == 0
    trash = _trash(client, DEPARTMENTS, company_headers, search="vent")
    assert [d["id"] for d in trash] == [sales["id"]] and trash[0]["deleted_by"] == COMPANY_EMAIL
    assert client.get(url, headers=company_headers).json()["data"]["deleted_at"]
    assert client.put(url, json={"name": "Otra"}, headers=company_headers).status_code == 404
    assert client.delete(url, headers=company_headers).json()["code"] == "ALREADY_DELETED"
    # Su nombre queda libre; restaurarlo con el nombre ocupado: 409 con el campo.
    again = client.post(DEPARTMENTS, json={"name": "VENTAS"}, headers=company_headers).json()["data"]
    taken = client.post(f"{url}/restore", headers=company_headers).json()
    assert taken["code"] == "RESTORE_CONFLICT" and taken["errors"][0]["field"] == "name"
    assert "VENTAS" not in taken["message"] and "Ventas" in taken["message"]
    assert client.delete(f"{DEPARTMENTS}/{again['id']}", headers=company_headers).status_code == 200
    restored = client.post(f"{url}/restore", headers=company_headers)
    assert restored.status_code == 200 and restored.json()["code"] == "DEPARTMENT_RESTORED"
    assert restored.json()["data"]["deleted_at"] is None and restored.json()["data"]["managers"] == []
    assert client.post(f"{url}/restore", headers=company_headers).json()["code"] == "NOT_DELETED"


def _managers(client, headers, department_id: int) -> list[int]:
    shown = client.get(f"{DEPARTMENTS}/{department_id}", headers=headers).json()["data"]
    return sorted(person["employee_id"] for person in shown["managers"])


def _managed(client, headers, employee_id: int) -> list[int]:
    shown = client.get(f"/api/employees/{employee_id}", headers=headers).json()["data"]
    return sorted(ref["id"] for ref in shown["managed_departments"])


def test_department_managers_come_back_exactly_when_restored(client, company_headers):
    """Decisión del dueño (2026-10-06): eliminar un departamento o a uno de sus responsables no borra la relación (el
    borrado lógico la oculta) y restaurar la regresa tal cual, en cuanto los dos lados están vigentes y en cualquier
    orden. Lo que la empresa quita a mano sí se va."""
    sales = client.post(DEPARTMENTS, json={"name": "Ventas"}, headers=company_headers).json()["data"]["id"]
    ops = client.post(DEPARTMENTS, json={"name": "Operaciones"}, headers=company_headers).json()["data"]["id"]
    ana = create_employee(client, company_headers, number="EMP-001", email="ana@empresa.com").json()["data"]["id"]
    luis = create_employee(client, company_headers, number="EMP-002", email="luis@empresa.com").json()["data"]["id"]
    for department, person in ((sales, ana), (sales, luis), (ops, ana)):
        body = {"employee_id": person}
        assert (
            client.post(f"{DEPARTMENTS}/{department}/managers", json=body, headers=company_headers).status_code == 200
        )
    employee = f"/api/employees/{ana}"

    # El responsable a «Eliminados»: sus departamentos dejan de mostrarlo; al restaurarlo vuelve a ambos.
    assert client.delete(employee, headers=company_headers).status_code == 200
    assert _managers(client, company_headers, sales) == [luis] and _managers(client, company_headers, ops) == []
    assert client.post(f"{employee}/restore", headers=company_headers).status_code == 200
    assert _managers(client, company_headers, sales) == sorted([ana, luis])
    assert _managed(client, company_headers, ana) == sorted([sales, ops])

    # El departamento a «Eliminados»: su responsable deja de verlo en su expediente; restaurado, vuelve con él.
    assert client.delete(f"{DEPARTMENTS}/{ops}", headers=company_headers).status_code == 200
    assert _managed(client, company_headers, ana) == [sales]
    restored = client.post(f"{DEPARTMENTS}/{ops}/restore", headers=company_headers).json()["data"]
    assert [person["employee_id"] for person in restored["managers"]] == [ana]

    # Los dos eliminados: restaurar al empleado primero no lo muestra en un departamento que sigue eliminado; restaurar
    # después el departamento lo regresa (el orden no importa).
    assert client.delete(f"{DEPARTMENTS}/{ops}", headers=company_headers).status_code == 200
    assert client.delete(employee, headers=company_headers).status_code == 200
    assert client.post(f"{employee}/restore", headers=company_headers).status_code == 200
    assert _managed(client, company_headers, ana) == [sales]
    assert client.post(f"{DEPARTMENTS}/{ops}/restore", headers=company_headers).status_code == 200
    assert _managers(client, company_headers, ops) == [ana] and _managed(client, company_headers, ana) == [sales, ops]

    # Lo que la empresa quitó a mano no regresa al restaurar.
    assert client.delete(f"{DEPARTMENTS}/{sales}/managers/{luis}", headers=company_headers).status_code == 200
    assert client.delete(f"/api/employees/{luis}", headers=company_headers).status_code == 200
    assert client.post(f"/api/employees/{luis}/restore", headers=company_headers).status_code == 200
    assert _managers(client, company_headers, sales) == [ana]


def test_a_restore_that_conflicts_brings_back_no_manager(client, company_headers):
    """Un departamento que no se puede restaurar (su nombre ya lo tiene otro) no regresa a sus responsables."""
    sales = client.post(DEPARTMENTS, json={"name": "Ventas"}, headers=company_headers).json()["data"]["id"]
    ana = create_employee(client, company_headers).json()["data"]["id"]
    body = {"employee_id": ana}
    assert client.post(f"{DEPARTMENTS}/{sales}/managers", json=body, headers=company_headers).status_code == 200
    assert client.delete(f"{DEPARTMENTS}/{sales}", headers=company_headers).status_code == 200
    again = client.post(DEPARTMENTS, json={"name": "Ventas"}, headers=company_headers).json()["data"]["id"]
    conflict = client.post(f"{DEPARTMENTS}/{sales}/restore", headers=company_headers).json()
    assert conflict["code"] == "RESTORE_CONFLICT"
    assert _managed(client, company_headers, ana) == [] and _managers(client, company_headers, again) == []


def test_the_department_trash_is_only_for_its_screen(client, company_headers):
    from app.models import RoleScreen
    from app.services.catalog_service import clear_catalog_cache

    with SessionLocal() as db:
        db.delete(db.get(RoleScreen, ("COMPANY", "COMPANY_DEPARTMENTS")))
        db.commit()
    clear_catalog_cache()
    assert client.get(DEPARTMENTS, headers=company_headers).status_code == 200  # Turnos y Calendario lo usan
    assert client.get(DEPARTMENTS, params={"deleted": "true"}, headers=company_headers).status_code == 403


# ---------------------------------------------------------------- sitios y turnos


def test_sites_go_to_the_trash_and_come_back(client, company_headers):
    site = create_site(client, company_headers)
    url = f"{SITES}/{site['id']}"
    assert client.delete(url, headers=company_headers).json()["code"] == "SITE_DELETED"
    assert client.get(SITES, headers=company_headers).json()["data"]["total"] == 0
    assert [s["id"] for s in _trash(client, SITES, company_headers)] == [site["id"]]
    assert client.get(url, headers=company_headers).json()["data"]["deleted_at"]
    assert client.delete(url, headers=company_headers).json()["code"] == "ALREADY_DELETED"
    # Un turno no puede usar un sitio eliminado (el mismo 422 que uno que no existe).
    refused = client.post(SHIFTS, json=shift_body("Con sitio", sites=[site["id"]]), headers=company_headers)
    assert refused.status_code == 422 and refused.json()["code"] == "SITE_NOT_AVAILABLE"
    twin = create_site(client, company_headers)  # mismo nombre: quedó libre
    taken = client.post(f"{url}/restore", headers=company_headers).json()
    assert taken["code"] == "RESTORE_CONFLICT" and taken["errors"][0]["field"] == "name"
    assert client.delete(f"{SITES}/{twin['id']}", headers=company_headers).status_code == 200
    restored = client.post(f"{url}/restore", headers=company_headers)
    assert restored.status_code == 200 and restored.json()["data"]["active"] is True
    assert client.post(f"{url}/restore", headers=company_headers).json()["code"] == "NOT_DELETED"


def test_shifts_go_to_the_trash_and_need_their_sites_to_come_back(client, company_headers):
    site = create_site(client, company_headers)
    shift = create_shift(client, company_headers, sites=[site["id"]])
    url = f"{SHIFTS}/{shift['id']}"
    ana_id = approved(client, company_headers, "ana", number="EMP-001")["id"]
    headers = login(client, "ana@empresa.com", "Empleado123")
    current = create_shift(client, company_headers, name="Vespertino")
    assert assign(client, company_headers, ana_id, current["id"], TODAY).status_code == 201
    later = (TODAY + timedelta(days=5)).isoformat()
    request = {"shift_id": shift["id"], "valid_from": later, "reason": "Estudios"}
    assert client.post("/api/me/shift-requests", json=request, headers=headers).status_code == 201

    assert client.delete(url, headers=company_headers).json()["code"] == "SHIFT_DELETED"
    with SessionLocal() as db:  # su solicitud pendiente se canceló (antes se borraba con el turno)
        assert db.scalar(select(ShiftChangeRequest.status)) == "CANCELLED"
    # Las solicitudes siguen nombrando al turno eliminado, con su marca.
    requests = client.get("/api/shift-requests", headers=company_headers).json()["data"]["items"]
    assert requests[0]["shift"]["deleted"] is True
    assert client.get(SHIFTS, params={"search": "matutino"}, headers=company_headers).json()["data"]["total"] == 0
    trash = _trash(client, SHIFTS, company_headers)
    assert [s["id"] for s in trash] == [shift["id"]] and trash[0]["deleted"] is True
    assert trash[0]["sites"][0]["id"] == site["id"]
    assert client.get(url, headers=company_headers).json()["data"]["deleted_at"]
    assert client.delete(url, headers=company_headers).json()["code"] == "ALREADY_DELETED"
    assert assign(client, company_headers, ana_id, shift["id"], TODAY + timedelta(days=3)).status_code == 404
    # Su sitio se eliminó después: restaurar el turno lo pide de vuelta primero.
    assert client.delete(f"{SITES}/{site['id']}", headers=company_headers).status_code == 200
    blocked = client.post(f"{url}/restore", headers=company_headers).json()
    assert blocked["code"] == "RESTORE_CONFLICT" and "Planta Norte" in blocked["message"]
    assert client.post(f"{SITES}/{site['id']}/restore", headers=company_headers).status_code == 200
    twin = create_shift(client, company_headers)  # mismo nombre: quedó libre
    taken = client.post(f"{url}/restore", headers=company_headers).json()
    assert taken["code"] == "RESTORE_CONFLICT" and taken["errors"][0]["field"] == "name"
    assert client.delete(f"{SHIFTS}/{twin['id']}", headers=company_headers).status_code == 200
    restored = client.post(f"{url}/restore", headers=company_headers)
    assert restored.status_code == 200 and restored.json()["code"] == "SHIFT_RESTORED"
    assert restored.json()["data"]["deleted"] is False
    assert client.post(f"{url}/restore", headers=company_headers).json()["code"] == "NOT_DELETED"


def test_a_site_used_by_a_deleted_shift_can_be_deleted(client, company_headers):
    site = create_site(client, company_headers)
    shift = create_shift(client, company_headers, sites=[site["id"]])
    assert client.delete(f"{SITES}/{site['id']}", headers=company_headers).json()["code"] == "SITE_IN_USE"
    assert client.delete(f"{SHIFTS}/{shift['id']}", headers=company_headers).status_code == 200
    assert client.delete(f"{SITES}/{site['id']}", headers=company_headers).status_code == 200


def test_restoring_two_deleted_sites_names_both(client, company_headers):
    north = create_site(client, company_headers)
    south = create_site(client, company_headers, name="Planta Sur", point=(29.07, -110.95))
    shift = create_shift(client, company_headers, sites=[north["id"], south["id"]])
    assert client.delete(f"{SHIFTS}/{shift['id']}", headers=company_headers).status_code == 200
    for site in (north, south):
        assert client.delete(f"{SITES}/{site['id']}", headers=company_headers).status_code == 200
    english = {**company_headers, "Accept-Language": "en-US"}
    blocked = client.post(f"{SHIFTS}/{shift['id']}/restore", headers=english).json()
    assert (
        blocked["message"] == "Can't restore: the sites Planta Norte and Planta Sur are in Deleted. Restore them first."
    )


# ---------------------------------------------------------------- cambios de turno programados


def _scheduled_change(client, company_headers) -> tuple[int, dict, dict, dict]:
    """(empleado, turno vigente, turno nuevo, cambio programado para dentro de 5 días)."""
    employee = create_employee(client, company_headers).json()["data"]
    current = create_shift(client, company_headers, name="Matutino")
    night = create_shift(client, company_headers, name="Nocturno", start_time="22:00", end_time="06:00")
    assert assign(client, company_headers, employee["id"], current["id"], TODAY).status_code == 201
    change = assign(client, company_headers, employee["id"], night["id"], TODAY + timedelta(days=5))
    assert change.status_code == 201, change.text
    return employee["id"], current, night, change.json()["data"]


def test_a_cancelled_shift_change_goes_to_the_trash_and_comes_back(client, company_headers):
    employee_id, _, _, change = _scheduled_change(client, company_headers)
    url = f"/api/shift-assignments/{change['id']}"
    listed = f"/api/employees/{employee_id}/shift-assignments"
    assert client.delete(url, headers=company_headers).json()["code"] == "SHIFT_ASSIGNMENT_CANCELLED"
    items = client.get(listed, headers=company_headers).json()["data"]["items"]
    assert len(items) == 1 and items[0]["valid_to"] is None  # la anterior volvió a no tener fin
    trash = _trash(client, listed, company_headers)
    assert [a["id"] for a in trash] == [change["id"]] and trash[0]["deleted_by"] == COMPANY_EMAIL
    assert client.delete(url, headers=company_headers).json()["code"] == "ALREADY_DELETED"
    restored = client.post(f"{url}/restore", headers=company_headers)
    assert restored.status_code == 200 and restored.json()["code"] == "SHIFT_ASSIGNMENT_RESTORED"
    items = client.get(listed, headers=company_headers).json()["data"]["items"]
    assert [a["valid_to"] for a in items] == [None, (TODAY + timedelta(days=4)).isoformat()]
    assert client.post(f"{url}/restore", headers=company_headers).json()["code"] == "NOT_DELETED"
    assert client.post("/api/shift-assignments/999999/restore", headers=company_headers).status_code == 404


def test_restoring_a_shift_change_applies_the_assignment_rules(client, company_headers):
    employee_id, current, night, change = _scheduled_change(client, company_headers)
    url = f"/api/shift-assignments/{change['id']}/restore"
    assert client.delete(f"/api/shift-assignments/{change['id']}", headers=company_headers).status_code == 200
    # Otro cambio quedó programado en su lugar.
    other = assign(client, company_headers, employee_id, night["id"], TODAY + timedelta(days=7)).json()["data"]
    scheduled = client.post(url, headers=company_headers).json()
    assert scheduled["code"] == "ASSIGNMENT_ALREADY_SCHEDULED"
    assert client.delete(f"/api/shift-assignments/{other['id']}", headers=company_headers).status_code == 200
    # Su turno desactivado o eliminado.
    status = f"{SHIFTS}/{night['id']}/status"
    assert client.patch(status, json={"active": False}, headers=company_headers).status_code == 200
    assert client.post(url, headers=company_headers).json()["code"] == "SHIFT_INACTIVE"
    assert client.patch(status, json={"active": True}, headers=company_headers).status_code == 200
    assert client.delete(f"{SHIFTS}/{night['id']}", headers=company_headers).status_code == 200
    gone = client.post(url, headers=company_headers).json()
    assert gone["code"] == "RESTORE_CONFLICT" and gone["errors"][0]["field"] == "shift_id"
    assert client.post(f"{SHIFTS}/{night['id']}/restore", headers=company_headers).status_code == 200
    # El empleado ya tiene ese turno sin fin (el mismo cambio ya no tiene sentido).
    with SessionLocal() as db:
        row = db.get(ShiftAssignment, change["id"], execution_options=WITH_DELETED)
        row.shift_id = current["id"]
        db.commit()
    identical = client.post(url, headers=company_headers).json()
    assert identical["code"] == "RESTORE_CONFLICT" and "sin fecha de fin" in identical["message"]
    # Su empleado inactivo o en «Eliminados».
    with SessionLocal() as db:
        row = db.get(ShiftAssignment, change["id"], execution_options=WITH_DELETED)
        row.shift_id = night["id"]
        db.commit()
    employee = f"/api/employees/{employee_id}"
    assert client.patch(f"{employee}/status", json={"active": False}, headers=company_headers).status_code == 200
    assert client.post(url, headers=company_headers).json()["code"] == "EMPLOYEE_INACTIVE"
    assert client.delete(employee, headers=company_headers).status_code == 200
    deleted = client.post(url, headers=company_headers).json()
    assert deleted["code"] == "RESTORE_CONFLICT" and "empleado" in deleted["message"]


# ---------------------------------------------------------------- festivos y días laborables


def test_holidays_go_to_the_trash_and_stop_being_days_off(client, company_headers):
    day = TODAY + timedelta(days=20)
    created = client.post(
        f"{CAL}/holidays", json={"holiday_date": day.isoformat(), "name": "Feria"}, headers=company_headers
    )
    holiday = created.json()["data"]
    url = f"{CAL}/holidays/{holiday['id']}"
    assert client.delete(url, headers=company_headers).json()["code"] == "HOLIDAY_DELETED"
    assert (
        client.get(f"{CAL}/holidays", params={"year": day.year}, headers=company_headers).json()["data"]["total"] == 0
    )
    trash = _trash(client, f"{CAL}/holidays", company_headers, year=day.year)
    assert [h["id"] for h in trash] == [holiday["id"]] and trash[0]["deleted_by"] == COMPANY_EMAIL
    assert _trash(client, f"{CAL}/holidays", company_headers, year=day.year + 1) == []
    assert client.delete(url, headers=company_headers).json()["code"] == "ALREADY_DELETED"
    # Ya no es día libre: un día laborable ahí no hace falta.
    employee = create_employee(client, company_headers).json()["data"]
    body = {"employee_id": employee["id"], "work_date": day.isoformat()}
    assert client.post(f"{CAL}/workdays", json=body, headers=company_headers).json()["code"] == "WORKDAY_NOT_NEEDED"
    # Otro festivo en la misma fecha (quedó libre): restaurar choca con su fecha.
    other = client.post(
        f"{CAL}/holidays", json={"holiday_date": day.isoformat(), "name": "Otro"}, headers=company_headers
    )
    assert other.status_code == 201
    taken = client.post(f"{url}/restore", headers=company_headers).json()
    assert taken["code"] == "RESTORE_CONFLICT" and taken["errors"][0]["field"] == "holiday_date"
    assert client.delete(f"{CAL}/holidays/{other.json()['data']['id']}", headers=company_headers).status_code == 200
    restored = client.post(f"{url}/restore", headers=company_headers)
    assert restored.status_code == 200 and restored.json()["data"]["deleted_at"] is None
    assert client.post(f"{url}/restore", headers=company_headers).json()["code"] == "NOT_DELETED"


def test_workdays_go_to_the_trash_and_come_back_with_their_rules(client, company_headers, monkeypatch):
    day = TODAY + timedelta(days=20)
    client.post(f"{CAL}/holidays", json={"holiday_date": day.isoformat(), "name": "Feria"}, headers=company_headers)
    employee = create_employee(client, company_headers).json()["data"]
    body = {"employee_id": employee["id"], "work_date": day.isoformat(), "note": "Guardia"}
    workday = client.post(f"{CAL}/workdays", json=body, headers=company_headers).json()["data"]
    url = f"{CAL}/workdays/{workday['id']}"
    assert client.delete(url, headers=company_headers).json()["code"] == "WORKDAY_DELETED"
    assert client.get(f"{CAL}/workdays", headers=company_headers).json()["data"]["total"] == 0
    trash = _trash(client, f"{CAL}/workdays", company_headers, employee_id=employee["id"])
    assert [w["id"] for w in trash] == [workday["id"]] and trash[0]["employee"]["deleted"] is False
    assert client.delete(url, headers=company_headers).json()["code"] == "ALREADY_DELETED"
    # Marcado otra vez ese día: restaurar choca.
    again = client.post(f"{CAL}/workdays", json=body, headers=company_headers).json()["data"]
    taken = client.post(f"{url}/restore", headers=company_headers).json()
    assert taken["code"] == "RESTORE_CONFLICT" and taken["errors"][0]["field"] == "work_date"
    assert client.delete(f"{CAL}/workdays/{again['id']}", headers=company_headers).status_code == 200
    # Las mismas reglas que al marcarlo: un día que ya pasó no se restaura.
    monkeypatch.setattr("app.services.calendar_service.business_today", lambda: day + timedelta(days=1))
    assert client.post(f"{url}/restore", headers=company_headers).json()["code"] == "WORKDAY_IN_PAST"
    monkeypatch.undo()
    restored = client.post(f"{url}/restore", headers=company_headers)
    assert restored.status_code == 200 and restored.json()["code"] == "WORKDAY_RESTORED"
    assert client.post(f"{url}/restore", headers=company_headers).json()["code"] == "NOT_DELETED"
    # Su empleado en «Eliminados»: el historial lo nombra con su marca y restaurar lo pide primero.
    assert client.delete(url, headers=company_headers).status_code == 200
    assert client.delete(f"/api/employees/{employee['id']}", headers=company_headers).status_code == 200
    trash = _trash(client, f"{CAL}/workdays", company_headers)
    assert trash[0]["employee"]["deleted"] is True
    gone = client.post(f"{url}/restore", headers=company_headers).json()
    assert gone["code"] == "RESTORE_CONFLICT" and gone["errors"][0]["field"] == "employee_id"
