"""El número de empleado es OPCIONAL (decisión del dueño del producto, migración 0076: «el número de empleado debe ser
opcional también»), con el mismo contrato que el RFC, la CURP y el NSS (`tests/test_optional_documents.py`):

- Al registrar: omitido, en null, vacío o solo con espacios y guiones se guarda como NULL (nunca ""); dos empleados sin
  número conviven en la empresa (el único parcial admite varios NULL).
- Con valor conserva todas sus reglas: formato (letras, números, guion o guion bajo), 30 caracteres y único entre los
  empleados vigentes de la empresa (`EMPLOYEE_NUMBER_TAKEN`).
- Al editar: omitido no cambia; null o vacío lo borra; con valor se agrega o se cambia.
- La validación en vivo de uno vacío responde `EMPTY` válido sin consultar nada; restaurar a quien no tiene número no
  choca con otro sin número.
- Todo lo que lo muestra lo entrega en null (expediente, listados, perfil, API de integración, consola del ADMIN,
  tablero, operaciones masivas, departamentos, validaciones, QR y punto de control) y la búsqueda encuentra a la persona
  por su nombre (la expresión de la búsqueda lleva `coalesce`).
"""

import pytest

from app.core.clock import business_today
from app.core.database import SessionLocal
from app.models import Employee
from tests.conftest import create_employee, login, phone_for, qr_content, submit_enrollment
from tests.test_api_keys import call, new_key
from tests.test_in_person_face import in_person
from tests.test_shifts import create_shift
from tests.test_validators import validator_headers

EMPLOYEES = "/api/employees"
EN = {"Accept-Language": "en-US"}
#: Sin la llave en el cuerpo (el campo omitido).
OMITTED = object()


def payload(person: str = "ana", number: object = OMITTED) -> dict:
    """Alta de una persona; sin `number`, sin la llave `employee_number`."""
    body = {
        "first_name": person.capitalize(),
        "last_name": "Ruiz",
        "birth_date": "1990-05-10",
        "phone": phone_for(person),
        "email": f"{person}@empresa.com",
        "password": "Empleado123",
    }
    return body if number is OMITTED else {**body, "employee_number": number}


def register(client, headers, person: str = "ana", number: object = OMITTED) -> dict:
    created = client.post(EMPLOYEES, json=payload(person, number), headers=headers)
    assert created.status_code == 201, created.text
    return created.json()["data"]


def stored(employee_id: int) -> str | None:
    """Lo que quedó en la base (NULL, nunca una cadena vacía)."""
    with SessionLocal() as db:
        employee = db.get(Employee, employee_id)
        assert employee is not None
        return employee.employee_number


def approved(client, company_headers, person: str = "ana") -> tuple[dict, dict]:
    """(empleado sin número con su rostro aprobado, headers de su sesión)."""
    employee = register(client, company_headers, person)
    headers = login(client, f"{person}@empresa.com", "Empleado123")
    enrollment = submit_enrollment(client, headers, frontal=(f"face:{person}".encode(),) * 3, turn_person=person)
    assert enrollment.status_code == 201, enrollment.text
    enrollment_id = enrollment.json()["data"]["enrollment_id"]
    pending = client.get("/api/enrollments", headers=company_headers).json()["data"]["items"]
    assert [(e["employee_id"], e["employee_number"]) for e in pending] == [(employee["id"], None)]
    approve = client.post(f"/api/enrollments/{enrollment_id}/approve", headers=company_headers)
    assert approve.status_code == 200, approve.text
    return employee, headers


# ---------------------------------------------------------------- alta


@pytest.mark.parametrize("blank", [OMITTED, None, "", "   ", " - "])
def test_an_employee_is_registered_without_a_number(client, company_headers, blank):
    employee = register(client, company_headers, number=blank)
    assert employee["employee_number"] is None and stored(employee["id"]) is None


def test_two_employees_without_a_number_coexist(client, company_headers):
    """El único parcial `(company_id, employee_number)` admite varios NULL: no chocan."""
    ana = register(client, company_headers, "ana", None)
    luis = register(client, company_headers, "luis", "")
    assert (stored(ana["id"]), stored(luis["id"])) == (None, None)


def test_a_captured_number_is_normalized(client, company_headers):
    employee = register(client, company_headers, number="  emp-7_a ")
    assert employee["employee_number"] == "EMP-7_A" == stored(employee["id"])


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("_ABC", "letras, números, guion o guion bajo"),
        ("AB CD", "letras, números, guion o guion bajo"),
        ("Ñ-1", "letras, números, guion o guion bajo"),
        ("A" * 31, "30"),
    ],
)
def test_a_captured_number_keeps_every_rule(client, company_headers, value, message):
    invalid = client.post(EMPLOYEES, json=payload(number=value), headers=company_headers)
    assert invalid.status_code == 422
    error = invalid.json()["errors"][0]
    assert error["field"] == "employee_number" and message in error["message"]


def test_a_captured_number_is_unique_among_live_employees(client, company_headers):
    first = register(client, company_headers, "ana", "EMP-9")
    taken = client.post(EMPLOYEES, json=payload("luis", " emp-9 "), headers=company_headers)
    assert taken.status_code == 409 and taken.json()["code"] == "EMPLOYEE_NUMBER_TAKEN"
    assert taken.json()["errors"][0]["field"] == "employee_number"
    # Uno en «Eliminados» no lo bloquea (regla 20 de la raíz).
    assert client.delete(f"{EMPLOYEES}/{first['id']}", headers=company_headers).status_code == 200
    assert register(client, company_headers, "luis", "EMP-9")["employee_number"] == "EMP-9"


def test_the_errors_of_a_captured_number_come_in_english(client, company_headers):
    headers = {**company_headers, **EN}
    invalid = client.post(EMPLOYEES, json=payload(number="_X"), headers=headers)
    assert invalid.json()["errors"][0]["message"] == (
        "The employee number must have 1–30 characters: letters, numbers, hyphens, or underscores"
    )
    register(client, company_headers, "ana", "EMP-1")
    taken = client.post(EMPLOYEES, json=payload("luis", "EMP-1"), headers=headers)
    assert taken.json()["message"] == "The employee number is already registered"


# ---------------------------------------------------------------- edición


def test_the_number_can_be_added_changed_and_cleared(client, company_headers):
    employee_id = register(client, company_headers)["id"]
    url = f"{EMPLOYEES}/{employee_id}"

    def edit(body: dict) -> dict:
        response = client.put(url, json=body, headers=company_headers)
        assert response.status_code == 200, response.text
        return response.json()["data"]

    assert edit({"employee_number": "emp-20"})["employee_number"] == "EMP-20"
    assert edit({"employee_number": "EMP-21"})["employee_number"] == "EMP-21"
    # Omitido no cambia; en otro dato, null tampoco cambia nada.
    kept = edit({"first_name": "Ana María", "last_name": None})
    assert (kept["employee_number"], kept["last_name"]) == ("EMP-21", "Ruiz")
    # Null o vacío lo borra (NULL en la base, nunca "").
    assert edit({"employee_number": None})["employee_number"] is None and stored(employee_id) is None
    edit({"employee_number": "EMP-22"})
    assert edit({"employee_number": "  "})["employee_number"] is None and stored(employee_id) is None


def test_editing_keeps_every_rule_of_a_captured_number(client, company_headers):
    register(client, company_headers, "luis", "EMP-30")
    employee_id = register(client, company_headers)["id"]
    url = f"{EMPLOYEES}/{employee_id}"
    taken = client.put(url, json={"employee_number": "emp-30"}, headers=company_headers)
    assert taken.status_code == 409 and taken.json()["code"] == "EMPLOYEE_NUMBER_TAKEN"
    invalid = client.put(url, json={"employee_number": "-X"}, headers=company_headers)
    assert invalid.status_code == 422 and invalid.json()["errors"][0]["field"] == "employee_number"
    assert stored(employee_id) is None
    # Su propio número no es un duplicado.
    client.put(url, json={"employee_number": "EMP-31"}, headers=company_headers)
    assert client.put(url, json={"employee_number": "EMP-31"}, headers=company_headers).status_code == 200


# ---------------------------------------------------------------- validación en vivo y «Eliminados»


def test_live_validation_of_an_empty_number_checks_nothing(client, company_headers):
    def check(value: str, headers: dict = company_headers) -> dict:
        response = client.get("/api/validation", params={"field": "employee_number", "value": value}, headers=headers)
        assert response.status_code == 200
        return response.json()["data"]

    for value in ("", "   ", "-"):
        result = check(value)
        assert (result["code"], result["valid"], result["available"]) == ("EMPTY", True, True)
        assert result["message"] == "Opcional: puede quedar vacío" and result["normalized"] is None
    assert check(" ", {**company_headers, **EN})["message"] == "Optional: can be left blank"
    # Con valor se valida como siempre.
    assert check(" emp-40 ")["code"] == "AVAILABLE" and check(" emp-40 ")["normalized"] == "EMP-40"
    assert check("_X")["code"] == "INVALID_FORMAT"
    register(client, company_headers, number="EMP-40")
    assert check("emp-40")["code"] == "TAKEN"


def test_an_employee_without_a_number_is_restored_beside_another_without_one(client, company_headers):
    """El conflicto del número al restaurar solo existe si el empleado tiene uno."""
    ana = register(client, company_headers, "ana")
    url = f"{EMPLOYEES}/{ana['id']}"
    assert client.delete(url, headers=company_headers).status_code == 200
    register(client, company_headers, "luis")
    restored = client.post(f"{url}/restore", headers=company_headers)
    assert restored.status_code == 200, restored.text
    assert restored.json()["code"] == "EMPLOYEE_RESTORED" and restored.json()["data"]["employee_number"] is None


# ---------------------------------------------------------------- quien lo muestra


def test_employees_without_a_number_are_listed_searched_and_read(client, company_headers, admin_headers):
    ana = register(client, company_headers, "ana")
    juan = create_employee(client, company_headers, number="EMP-001").json()["data"]  # Juan Pérez, con número

    def search(term: str) -> list[tuple[int, str | None]]:
        page = client.get(EMPLOYEES, params={"search": term}, headers=company_headers).json()["data"]
        return [(item["id"], item["employee_number"]) for item in page["items"]]

    # La búsqueda la encuentra por su nombre aunque no tenga número (`coalesce` en la expresión de la búsqueda).
    assert search("ana ruiz") == [(ana["id"], None)]
    assert search("emp-001") == [(juan["id"], "EMP-001")]
    assert set(search("")) == {(ana["id"], None), (juan["id"], "EMP-001")}
    ids = client.get(f"{EMPLOYEES}/ids", params={"search": "ruiz"}, headers=company_headers).json()["data"]
    assert ids["ids"] == [ana["id"]]
    assert client.get(f"{EMPLOYEES}/{ana['id']}", headers=company_headers).json()["data"]["employee_number"] is None
    me = client.get("/api/users/me", headers=login(client, "ana@empresa.com", "Empleado123")).json()["data"]
    assert me["employee"]["employee_number"] is None
    # La consola del ADMIN (ficha de trabajo) también.
    companies = client.get("/api/admin/companies", headers=admin_headers).json()["data"]["items"]
    company_id = next(c["id"] for c in companies)
    staff = client.get(f"/api/admin/companies/{company_id}/employees", headers=admin_headers).json()["data"]
    assert {(e["id"], e["employee_number"]) for e in staff["items"]} == {(ana["id"], None), (juan["id"], "EMP-001")}


def test_the_integration_api_delivers_a_missing_number_as_null(client, company_headers):
    employee_id = register(client, company_headers)["id"]
    secret = new_key(client, company_headers)["secret"]
    one = call(client, secret, f"/employees/{employee_id}")
    assert one.status_code == 200 and one.json()["data"]["employee_number"] is None
    assert [e["employee_number"] for e in call(client, secret, "/employees").json()["data"]["items"]] == [None]


def test_shifts_board_and_departments_name_an_employee_without_a_number(client, company_headers):
    ana = register(client, company_headers)
    shift = create_shift(client, company_headers)  # remoto todos sus días: no necesita sitio
    today = business_today().isoformat()
    bulk = client.post(
        "/api/shift-assignments/bulk",
        json={"shift_id": shift["id"], "employee_ids": [ana["id"]], "valid_from": today},
        headers=company_headers,
    )
    assert bulk.status_code == 200, bulk.text
    assert [r["employee"] for r in bulk.json()["data"]["results"]] == [
        {"id": ana["id"], "full_name": "Ana Ruiz", "employee_number": None, "deleted": False, "avatar": None}
    ]
    board = client.get(
        "/api/attendance/board", params={"date": today, "search": "ruiz"}, headers=company_headers
    ).json()["data"]
    rows = [(row["employee"]["id"], row["employee"]["employee_number"]) for row in board["items"]]
    assert rows == [(ana["id"], None)]

    department = client.post("/api/departments", json={"name": "Producción"}, headers=company_headers).json()["data"]
    managers = f"/api/departments/{department['id']}/managers"
    assert client.post(managers, json={"employee_id": ana["id"]}, headers=company_headers).status_code == 200
    detail = client.get(f"/api/departments/{department['id']}", headers=company_headers).json()["data"]
    assert [(m["employee_id"], m["employee_number"]) for m in detail["managers"]] == [(ana["id"], None)]


def test_the_qr_and_the_checkpoint_work_without_a_number(client, company_headers):
    ana, headers = approved(client, company_headers)
    qr = client.post("/api/users/me/qr", headers=headers)
    assert qr.status_code == 201, qr.text
    assert qr.json()["data"]["employee_number"] is None
    validator = validator_headers(client, company_headers, mode="QR_AND_FACE")
    inspected = client.post("/api/checkpoint/qr/inspect", json={"qr_content": qr_content(ana["id"])}, headers=validator)
    assert inspected.status_code == 200, inspected.text
    holder = inspected.json()["data"]
    assert (holder["employee_id"], holder["name"], holder["employee_number"]) == (ana["id"], "Ana Ruiz", None)


@pytest.mark.parametrize(("headers", "expected"), [({}, "es"), (EN, "en")])
def test_a_duplicate_face_without_a_number_is_named_by_its_name(client, company_headers, headers, expected):
    """En persona, el mismo rostro registrado como otro empleado se bloquea y el mensaje lo nombra: sin número, solo
    con su nombre («… (Juan Ruiz)»), nunca «None»."""
    approved(client, company_headers, "juan")
    luis = register(client, company_headers, "luis")
    duplicate = in_person(client, {**company_headers, **headers}, luis["id"], "enroll", frames=3)
    assert duplicate.status_code == 409 and duplicate.json()["code"] == "FACE_ALREADY_REGISTERED"
    message = duplicate.json()["message"]
    assert message.endswith(" (Juan Ruiz)") and "None" not in message, (expected, message)
