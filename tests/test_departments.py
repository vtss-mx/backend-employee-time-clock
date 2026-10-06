"""Departamentos de cada empresa: responsables y empleados asignados (rol COMPANY)."""

from tests.conftest import create_company, create_employee, login

URL = "/api/departments"


def new_department(client, headers, name="Producción", **extra):
    response = client.post(URL, json={"name": name, **extra}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def employee(client, headers, number: str, email: str) -> dict:
    response = create_employee(client, headers, number=number, email=email)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def test_company_manages_its_departments(client, company_headers):
    created = client.post(
        URL, json={"name": "  Recursos   Humanos ", "description": " Nómina y personal "}, headers=company_headers
    )
    assert created.status_code == 201 and created.json()["code"] == "DEPARTMENT_CREATED"
    hr = created.json()["data"]
    assert (hr["name"], hr["description"], hr["employee_count"], hr["managers"]) == (
        "Recursos Humanos",
        "Nómina y personal",
        0,
        [],
    )
    # Nombre único en la empresa sin distinguir mayúsculas ni espacios.
    taken = client.post(URL, json={"name": "recursos humanos"}, headers=company_headers)
    assert taken.status_code == 409 and taken.json()["code"] == "DEPARTMENT_NAME_TAKEN"
    assert client.post(URL, json={"name": "   "}, headers=company_headers).status_code == 422

    new_department(client, company_headers, "Almacén")
    page = client.get(URL, params={"size": 1}, headers=company_headers).json()["data"]
    assert page["total"] == 2 and [d["name"] for d in page["items"]] == ["Almacén"]  # orden alfabético
    found = client.get(URL, params={"search": "HUMA"}, headers=company_headers).json()["data"]
    assert [d["id"] for d in found["items"]] == [hr["id"]]

    edited = client.put(f"{URL}/{hr['id']}", json={"name": "Capital Humano"}, headers=company_headers)
    assert edited.status_code == 200 and edited.json()["data"]["name"] == "Capital Humano"
    assert edited.json()["data"]["description"] is None
    clash = client.put(f"{URL}/{hr['id']}", json={"name": "ALMACÉN"}, headers=company_headers)
    assert clash.status_code == 409  # "ALMACÉN" ya existe
    same = client.put(f"{URL}/{hr['id']}", json={"name": "capital humano"}, headers=company_headers)
    assert same.status_code == 200  # su propio nombre no choca consigo

    assert client.delete(f"{URL}/{hr['id']}", headers=company_headers).json()["code"] == "DEPARTMENT_DELETED"
    assert client.get(f"{URL}/{hr['id']}", headers=company_headers).json()["data"]["deleted_at"]  # en «Eliminados»


def test_employees_and_managers_of_a_department(client, company_headers):
    production = new_department(client, company_headers, "Producción")
    warehouse = new_department(client, company_headers, "Almacén")
    ana = employee(client, company_headers, "EMP-1", "ana@empresa.com")
    luis = employee(client, company_headers, "EMP-2", "luis@empresa.com")
    eva = employee(client, company_headers, "EMP-3", "eva@empresa.com")
    members = f"{URL}/{production['id']}/employees"

    for person in (ana, luis):
        assigned = client.post(members, json={"employee_id": person["id"]}, headers=company_headers)
        assert assigned.json()["code"] == "DEPARTMENT_EMPLOYEE_ASSIGNED"
    again = client.post(members, json={"employee_id": ana["id"]}, headers=company_headers)  # reintento
    assert again.status_code == 200 and again.json()["data"]["employee_count"] == 2

    # Responsables: varios, empleados de la empresa (pueden no ser miembros).
    managers = f"{URL}/{production['id']}/managers"
    for person in (eva, ana, eva):
        assert client.post(managers, json={"employee_id": person["id"]}, headers=company_headers).status_code == 200
    detail = client.get(f"{URL}/{production['id']}", headers=company_headers).json()["data"]
    assert sorted(m["employee_id"] for m in detail["managers"]) == sorted([ana["id"], eva["id"]])

    # El listado de empleados filtra por departamento y dice a cuál pertenece cada uno.
    listed = client.get("/api/employees", params={"department_id": production["id"]}, headers=company_headers)
    assert {e["id"] for e in listed.json()["data"]["items"]} == {ana["id"], luis["id"]}
    assert {e["department_name"] for e in listed.json()["data"]["items"]} == {"Producción"}
    profile = client.get(f"/api/employees/{eva['id']}", headers=company_headers).json()["data"]
    assert profile["department_id"] is None and profile["managed_departments"] == [
        {"id": production["id"], "name": "Producción"}
    ]

    # Cada empleado está en un solo departamento: asignarlo a otro lo cambia.
    client.post(f"{URL}/{warehouse['id']}/employees", json={"employee_id": luis["id"]}, headers=company_headers)
    moved = client.get(f"/api/employees/{luis['id']}", headers=company_headers).json()["data"]
    assert (moved["department_id"], moved["department_name"]) == (warehouse["id"], "Almacén")

    # Con empleados no se borra; al quitarlos, sí (y sus responsables se van con él).
    blocked = client.delete(f"{URL}/{production['id']}", headers=company_headers)
    assert blocked.status_code == 409 and blocked.json()["code"] == "DEPARTMENT_HAS_EMPLOYEES"
    removed = client.delete(f"{members}/{ana['id']}", headers=company_headers)
    assert removed.json()["code"] == "DEPARTMENT_EMPLOYEE_REMOVED" and removed.json()["data"]["employee_count"] == 0
    assert client.delete(f"{members}/{ana['id']}", headers=company_headers).status_code == 200  # idempotente
    # Quitar de ESTE departamento a quien está en otro no lo mueve.
    client.delete(f"{members}/{luis['id']}", headers=company_headers)
    assert client.get(f"/api/employees/{luis['id']}", headers=company_headers).json()["data"]["department_id"]

    retired = client.delete(f"{managers}/{eva['id']}", headers=company_headers)
    assert [m["employee_id"] for m in retired.json()["data"]["managers"]] == [ana["id"]]
    assert client.delete(f"{managers}/{eva['id']}", headers=company_headers).status_code == 200  # idempotente
    assert client.delete(f"{URL}/{production['id']}", headers=company_headers).status_code == 200

    # Borrar a un empleado lo retira como responsable (cascada en la BD).
    hr = new_department(client, company_headers, "Recursos Humanos")
    client.post(f"{URL}/{hr['id']}/managers", json={"employee_id": eva["id"]}, headers=company_headers)
    assert client.delete(f"/api/employees/{eva['id']}", headers=company_headers).status_code == 200
    assert client.get(f"{URL}/{hr['id']}", headers=company_headers).json()["data"]["managers"] == []


def test_departments_are_isolated_per_company(client, admin_headers, company_headers):
    mine = new_department(client, company_headers, "Producción")
    ana = employee(client, company_headers, "EMP-1", "ana@empresa.com")
    create_company(client, admin_headers)
    other = login(client, "admin@panificadora.com", "Empresa1234")
    theirs = new_department(client, other, "Producción")  # el mismo nombre en otra empresa sí
    outsider = employee(client, other, "PAN-1", "eva@panificadora.com")

    # Un departamento o un empleado de otra empresa se comporta como inexistente (404).
    assert client.get(f"{URL}/{theirs['id']}", headers=company_headers).status_code == 404
    assert client.put(f"{URL}/{theirs['id']}", json={"name": "X"}, headers=company_headers).status_code == 404
    assert client.delete(f"{URL}/{theirs['id']}", headers=company_headers).status_code == 404
    for kind in ("employees", "managers"):
        foreign = client.post(
            f"{URL}/{mine['id']}/{kind}", json={"employee_id": outsider["id"]}, headers=company_headers
        )
        assert foreign.status_code == 404 and foreign.json()["code"] == "EMPLOYEE_NOT_FOUND"
        into_theirs = client.post(
            f"{URL}/{theirs['id']}/{kind}", json={"employee_id": ana["id"]}, headers=company_headers
        )
        assert into_theirs.status_code == 404 and into_theirs.json()["code"] == "DEPARTMENT_NOT_FOUND"
    assert [d["id"] for d in client.get(URL, headers=company_headers).json()["data"]["items"]] == [mine["id"]]


def test_only_the_company_manages_departments(client, company_headers):
    department = new_department(client, company_headers)
    create_employee(client, company_headers)
    staff = login(client, "juan@empresa.com", "Empleado123")
    assert client.get(URL, headers=staff).status_code == 403
    assert client.post(URL, json={"name": "Ventas"}, headers=staff).status_code == 403
    assert client.get(f"{URL}/{department['id']}", headers=staff).status_code == 403
    assert client.get(URL).status_code == 401


def test_department_name_is_validated_live(client, company_headers):
    hr = new_department(client, company_headers, "Recursos Humanos")

    def check(value: str, exclude_id: int | None = None) -> dict:
        params = {"field": "department_name", "value": value, **({"exclude_id": exclude_id} if exclude_id else {})}
        response = client.get("/api/validation", params=params, headers=company_headers)
        assert response.status_code == 200, response.text
        return response.json()["data"]

    assert check("recursos  humanos")["code"] == "TAKEN"
    assert check("recursos humanos", hr["id"])["code"] == "AVAILABLE"  # editando el mismo
    assert check("Ventas")["code"] == "AVAILABLE"
    assert check("  ")["code"] == "EMPTY"
    assert check("x" * 101)["code"] == "INVALID_FORMAT"
