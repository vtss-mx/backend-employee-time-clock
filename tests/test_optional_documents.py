"""RFC, CURP y NSS del empleado y el RFC de la empresa son OPCIONALES (decisión del dueño del producto: la plataforma se
abre a otros países, donde esos documentos no existen).

- Al registrar: omitidos, en null, vacíos o solo con espacios y guiones se guardan como NULL (nunca "").
- Con valor se validan completos (formato, longitud, dígito verificador, fecha de nacimiento) y son únicos en la
  empresa con su código (`RFC_TAKEN`, `CURP_TAKEN`, `NSS_TAKEN`).
- Al editar: omitidos no cambian; null o vacío los borra; se pueden agregar y cambiar.
- La validación en vivo de uno vacío no consulta nada y no impide guardar; la API de integración y el perfil los
  entregan en null.
- El RFC de la empresa (consola del ADMIN) sigue el mismo contrato: con valor, 12 o 13 caracteres, sin genéricos y único
  entre las empresas vigentes (`COMPANY_TAX_ID_TAKEN`); restaurar una empresa sin RFC no choca con otra sin RFC. Desde
  la migración 0074 es el identificador fiscal de tipo `MX_RFC` (`tests/test_company_tax_id.py`): estas pruebas lo
  envían con el campo anterior `rfc`, que sigue aceptándose (obsoleto) y deja sus errores en `tax_id`.
"""

import pytest

from app.core.database import SessionLocal
from app.models import Company, Employee
from tests.conftest import create_company, create_employee, curp_for, login, nss_for, phone_for, rfc_for
from tests.test_api_keys import call, new_key

DOCUMENTS = ("rfc", "curp", "nss")
EN = {"Accept-Language": "en-US"}


def payload(number: str = "INT-001", email: str = "ana@empresa.com", **documents) -> dict:
    """Alta de una persona sin documentos mexicanos (salvo los que se pasen)."""
    return {
        "first_name": "Ana",
        "last_name": "Smith",
        "birth_date": "1990-05-10",
        "employee_number": number,
        "phone": phone_for(number),
        "email": email,
        "password": "Empleado123",
        **documents,
    }


def stored(employee_id: int) -> tuple[str | None, str | None, str | None]:
    """Lo que quedó en la base (NULL, nunca una cadena vacía)."""
    with SessionLocal() as db:
        employee = db.get(Employee, employee_id)
        assert employee is not None
        return employee.rfc, employee.curp, employee.nss


def documents_of(response) -> dict:
    data = response.json()["data"]
    return {field: data[field] for field in DOCUMENTS}


# ---------------------------------------------------------------- alta


def test_an_employee_is_registered_without_rfc_curp_or_nss(client, company_headers):
    created = client.post("/api/employees", json=payload(), headers=company_headers)
    assert created.status_code == 201, created.text
    assert documents_of(created) == {"rfc": None, "curp": None, "nss": None}
    assert stored(created.json()["data"]["id"]) == (None, None, None)


@pytest.mark.parametrize("blank", [None, "", "   ", " - "])
def test_null_or_blank_documents_are_stored_as_null(client, company_headers, blank):
    """Dos personas sin documentos en la misma empresa: los índices únicos admiten varios NULL."""
    for number, email in (("INT-001", "ana@empresa.com"), ("INT-002", "bob@empresa.com")):
        body = payload(number, email, rfc=blank, curp=blank, nss=blank)
        created = client.post("/api/employees", json=body, headers=company_headers)
        assert created.status_code == 201, created.text
        assert stored(created.json()["data"]["id"]) == (None, None, None)


@pytest.mark.parametrize(
    ("field", "raw", "normalized"),
    [
        ("rfc", "pexj-900510-ab1", "PEXJ900510AB1"),
        ("curp", curp_for("INT-001").lower(), curp_for("INT-001")),
        ("nss", "1234 5678 903", "12345678903"),
    ],
)
def test_each_document_can_be_captured_alone(client, company_headers, field, raw, normalized):
    created = client.post("/api/employees", json=payload(**{field: raw}), headers=company_headers)
    assert created.status_code == 201, created.text
    assert documents_of(created) == {name: normalized if name == field else None for name in DOCUMENTS}


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("rfc", "PEGJ", "13 caracteres"),
        ("rfc", "XAXX010101000", "genérico"),
        ("curp", "HEGG560427MVZRRL05", "dígito verificador"),
        ("nss", "12345678904", "dígito verificador"),
        ("nss", "1234", "11 dígitos"),
    ],
)
def test_a_captured_document_keeps_every_rule(client, company_headers, field, value, message):
    invalid = client.post("/api/employees", json=payload(**{field: value}), headers=company_headers)
    assert invalid.status_code == 422
    error = invalid.json()["errors"][0]
    assert error["field"] == field and message in error["message"]


def test_a_captured_rfc_or_curp_must_match_the_birth_date(client, company_headers):
    rfc = client.post("/api/employees", json=payload(rfc="PEXJ910510AB1"), headers=company_headers)
    assert rfc.status_code == 422 and rfc.json()["code"] == "RFC_BIRTH_DATE_MISMATCH"
    curp = client.post("/api/employees", json=payload(curp="HEGG560427MVZRRL04"), headers=company_headers)
    assert curp.status_code == 422 and curp.json()["code"] == "CURP_BIRTH_DATE_MISMATCH"
    assert curp.json()["errors"][0]["field"] == "curp"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("rfc", rfc_for("EMP-001"), "RFC_TAKEN"),
        ("curp", curp_for("EMP-001"), "CURP_TAKEN"),
        ("nss", nss_for("EMP-001"), "NSS_TAKEN"),
    ],
)
def test_a_captured_document_is_unique_in_the_company(client, company_headers, field, value, code):
    assert create_employee(client, company_headers).status_code == 201  # EMP-001 con sus tres documentos
    duplicate = client.post("/api/employees", json=payload(**{field: value}), headers=company_headers)
    assert duplicate.status_code == 409 and duplicate.json()["code"] == code
    assert duplicate.json()["errors"][0]["field"] == field


def test_the_errors_of_a_captured_document_come_in_english(client, company_headers):
    headers = {**company_headers, **EN}
    invalid = client.post("/api/employees", json=payload(nss="12345678904"), headers=headers)
    assert invalid.json()["errors"][0]["message"] == "The NSS isn't valid: the check digit doesn't match"
    assert create_employee(client, company_headers).status_code == 201
    taken = client.post("/api/employees", json=payload(rfc=rfc_for("EMP-001")), headers=headers)
    assert taken.json()["message"] == "The RFC is already registered to another employee"


# ---------------------------------------------------------------- edición


def test_documents_can_be_added_changed_and_cleared(client, company_headers):
    employee_id = client.post("/api/employees", json=payload(), headers=company_headers).json()["data"]["id"]
    url = f"/api/employees/{employee_id}"

    added = client.put(url, json={"rfc": "PEXJ900510AB1", "nss": nss_for("A")}, headers=company_headers)
    assert added.status_code == 200, added.text
    assert documents_of(added) == {"rfc": "PEXJ900510AB1", "curp": None, "nss": nss_for("A")}

    changed = client.put(url, json={"rfc": "PEXJ900510ZZ9", "curp": curp_for("A")}, headers=company_headers)
    assert documents_of(changed) == {"rfc": "PEXJ900510ZZ9", "curp": curp_for("A"), "nss": nss_for("A")}

    # Omitido no cambia; null o vacío lo borra (NULL en la base, nunca "").
    cleared = client.put(url, json={"rfc": None, "curp": "  ", "first_name": "Ana María"}, headers=company_headers)
    assert cleared.status_code == 200, cleared.text
    assert documents_of(cleared) == {"rfc": None, "curp": None, "nss": nss_for("A")}
    assert cleared.json()["data"]["first_name"] == "Ana María"
    assert stored(employee_id) == (None, None, nss_for("A"))

    gone = client.put(url, json={"nss": ""}, headers=company_headers)
    assert documents_of(gone) == {"rfc": None, "curp": None, "nss": None}


def test_a_null_in_another_field_changes_nothing(client, company_headers):
    """Solo los documentos opcionales se borran con null; en un dato obligatorio null es "no cambiarlo"."""
    employee_id = create_employee(client, company_headers).json()["data"]["id"]
    kept = client.put(
        f"/api/employees/{employee_id}", json={"first_name": None, "birth_date": None}, headers=company_headers
    )
    assert kept.status_code == 200
    assert kept.json()["data"]["first_name"] == "Juan" and kept.json()["data"]["birth_date"] == "1990-05-10"
    assert documents_of(kept) == {"rfc": rfc_for("EMP-001"), "curp": curp_for("EMP-001"), "nss": nss_for("EMP-001")}


def test_clearing_the_rfc_and_curp_frees_the_birth_date(client, company_headers):
    """Sin RFC ni CURP no hay fecha que coincida: la fecha de nacimiento cambia sola."""
    employee_id = create_employee(client, company_headers).json()["data"]["id"]
    url = f"/api/employees/{employee_id}"
    assert client.put(url, json={"birth_date": "1991-05-10"}, headers=company_headers).status_code == 422
    moved = client.put(url, json={"birth_date": "1991-05-10", "rfc": None, "curp": None}, headers=company_headers)
    assert moved.status_code == 200, moved.text
    assert moved.json()["data"]["birth_date"] == "1991-05-10" and moved.json()["data"]["nss"] == nss_for("EMP-001")


def test_editing_keeps_every_rule_of_a_captured_document(client, company_headers):
    create_employee(client, company_headers)  # EMP-001 con sus tres documentos
    employee_id = client.post("/api/employees", json=payload(), headers=company_headers).json()["data"]["id"]
    url = f"/api/employees/{employee_id}"
    taken = client.put(url, json={"curp": curp_for("EMP-001")}, headers=company_headers)
    assert taken.status_code == 409 and taken.json()["code"] == "CURP_TAKEN"
    invalid = client.put(url, json={"nss": "1234"}, headers=company_headers)
    assert invalid.status_code == 422 and invalid.json()["errors"][0]["field"] == "nss"
    assert stored(employee_id) == (None, None, None)


# ---------------------------------------------------------------- validación en vivo, búsqueda, perfil e integración


@pytest.mark.parametrize("field", DOCUMENTS)
def test_live_validation_of_an_empty_document_checks_nothing(client, company_headers, field):
    def check(value: str, headers: dict) -> dict:
        response = client.get("/api/validation", params={"field": field, "value": value}, headers=headers)
        assert response.status_code == 200
        return response.json()["data"]

    for value in ("", "   ", "-"):
        result = check(value, company_headers)
        assert (result["code"], result["valid"], result["available"]) == ("EMPTY", True, True)
        assert result["message"] == "Opcional: puede quedar vacío"
    assert check(" ", {**company_headers, **EN})["message"] == "Optional: can be left blank"


def test_employees_without_documents_are_searched_listed_and_read(client, company_headers):
    employee_id = client.post("/api/employees", json=payload(), headers=company_headers).json()["data"]["id"]
    found = client.get("/api/employees", params={"search": "ana smith"}, headers=company_headers).json()["data"]
    assert [item["id"] for item in found["items"]] == [employee_id]
    assert documents_of(client.get(f"/api/employees/{employee_id}", headers=company_headers)) == dict.fromkeys(
        DOCUMENTS
    )
    me = client.get("/api/users/me", headers=login(client, "ana@empresa.com", "Empleado123")).json()["data"]
    assert {field: me["employee"][field] for field in DOCUMENTS} == dict.fromkeys(DOCUMENTS)


def test_the_integration_api_delivers_missing_documents_as_null(client, company_headers):
    employee_id = client.post("/api/employees", json=payload(), headers=company_headers).json()["data"]["id"]
    secret = new_key(client, company_headers)["secret"]
    one = call(client, secret, f"/employees/{employee_id}")
    assert one.status_code == 200 and documents_of(one) == dict.fromkeys(DOCUMENTS)
    listed = call(client, secret, "/employees").json()["data"]["items"]
    assert {field: listed[0][field] for field in DOCUMENTS} == dict.fromkeys(DOCUMENTS)


# ---------------------------------------------------------------- RFC de la empresa (consola del ADMIN)

COMPANIES = "/api/admin/companies"
COMPANY_RFC = "PNO120315AB1"  # el de `create_company`


def foreign_company(client, admin_headers, admin_email: str = "admin@northwind.com"):
    """Alta de una empresa de otro país: el RFC ni se envía."""
    body = {
        "name": "Northwind Traders",
        "legal_name": "Northwind Traders LLC",
        "phone": "6621234567",
        "admin_email": admin_email,
        "admin_password": "Empresa1234",
    }
    return client.post(COMPANIES, json=body, headers=admin_headers)


def stored_company_rfc(company_id: int) -> str | None:
    """El RFC que quedó en la base (NULL, nunca una cadena vacía)."""
    with SessionLocal() as db:
        company = db.get(Company, company_id)
        assert company is not None
        return company.rfc


def test_a_company_is_registered_without_rfc(client, admin_headers):
    created = foreign_company(client, admin_headers)
    assert created.status_code == 201, created.text
    company = created.json()["data"]
    assert company["rfc"] is None and stored_company_rfc(company["id"]) is None
    assert client.get(f"{COMPANIES}/{company['id']}", headers=admin_headers).json()["data"]["rfc"] is None


@pytest.mark.parametrize("blank", [None, "", "   ", " - "])
def test_two_companies_without_rfc_are_stored_as_null(client, admin_headers, blank):
    """El índice único parcial del RFC admite varios NULL: dos empresas sin RFC no chocan."""
    for email in ("admin@uno.com", "admin@dos.com"):
        created = create_company(client, admin_headers, rfc=blank, admin_email=email)
        assert created.status_code == 201, created.text
        assert stored_company_rfc(created.json()["data"]["id"]) is None


@pytest.mark.parametrize(("raw", "normalized"), [("pno-120315-ab1", COMPANY_RFC), ("pexj 900510 ab1", "PEXJ900510AB1")])
def test_a_captured_company_rfc_is_normalized(client, admin_headers, raw, normalized):
    """Persona moral (12) o persona física con actividad empresarial (13), en mayúsculas y sin separadores."""
    created = create_company(client, admin_headers, rfc=raw)
    assert created.status_code == 201, created.text
    assert created.json()["data"]["rfc"] == normalized == stored_company_rfc(created.json()["data"]["id"])


@pytest.mark.parametrize(
    ("value", "message"),
    [("PNO12", "12 caracteres"), ("XAXX010101000", "genérico"), ("ABC991332XX1", "fecha")],
)
def test_a_captured_company_rfc_keeps_every_rule(client, admin_headers, value, message):
    invalid = create_company(client, admin_headers, rfc=value)
    assert invalid.status_code == 422
    error = invalid.json()["errors"][0]
    assert error["field"] == "tax_id" and message in error["message"]


def test_a_captured_company_rfc_is_unique_among_live_companies(client, admin_headers):
    assert create_company(client, admin_headers).status_code == 201
    taken = create_company(client, admin_headers, rfc="pno-120315-ab1", admin_email="otra@empresa.com")
    assert taken.status_code == 409 and taken.json()["code"] == "COMPANY_TAX_ID_TAKEN"
    assert taken.json()["errors"][0]["field"] == "tax_id"


def test_the_errors_of_a_captured_company_rfc_come_in_english(client, admin_headers):
    headers = {**admin_headers, **EN}
    invalid = create_company(client, headers, rfc="PNO12")
    assert invalid.json()["errors"][0]["message"] == "The RFC must have 12 characters (legal entity) or 13 (individual)"
    assert create_company(client, admin_headers).status_code == 201
    taken = create_company(client, headers, admin_email="otra@empresa.com")
    assert taken.json()["message"] == "A company with that tax ID already exists"


def test_the_company_rfc_can_be_added_changed_and_cleared(client, admin_headers):
    company_id = foreign_company(client, admin_headers).json()["data"]["id"]
    url = f"{COMPANIES}/{company_id}"

    def edit(body: dict) -> dict:
        response = client.put(url, json=body, headers=admin_headers)
        assert response.status_code == 200, response.text
        return response.json()["data"]

    assert edit({"rfc": "pno-120315-ab1"})["rfc"] == COMPANY_RFC
    assert edit({"rfc": "PEXJ900510AB1"})["rfc"] == "PEXJ900510AB1"
    # Omitido no cambia; en otro dato, null tampoco cambia nada.
    assert edit({"name": "Northwind México"})["rfc"] == "PEXJ900510AB1"
    assert edit({"name": None})["name"] == "Northwind México"
    # Null o vacío lo borra (NULL en la base, nunca "").
    cleared = edit({"rfc": None})
    assert cleared["rfc"] is None and cleared["name"] == "Northwind México"
    assert stored_company_rfc(company_id) is None
    edit({"rfc": COMPANY_RFC})
    assert edit({"rfc": " - "})["rfc"] is None and stored_company_rfc(company_id) is None


def test_editing_keeps_every_rule_of_a_captured_company_rfc(client, admin_headers):
    assert create_company(client, admin_headers).status_code == 201
    company_id = foreign_company(client, admin_headers).json()["data"]["id"]
    url = f"{COMPANIES}/{company_id}"
    taken = client.put(url, json={"rfc": COMPANY_RFC}, headers=admin_headers)
    assert taken.status_code == 409 and taken.json()["code"] == "COMPANY_TAX_ID_TAKEN"
    invalid = client.put(url, json={"rfc": "ABC"}, headers=admin_headers)
    assert invalid.status_code == 422 and invalid.json()["errors"][0]["field"] == "tax_id"
    assert stored_company_rfc(company_id) is None


def test_live_validation_of_an_empty_company_rfc_checks_nothing(client, admin_headers):
    def check(field: str, value: str, headers: dict = admin_headers) -> dict:
        response = client.get("/api/validation", params={"field": field, "value": value}, headers=headers)
        assert response.status_code == 200
        return response.json()["data"]

    for value in ("", "   ", "-"):
        result = check("company_rfc", value)
        assert (result["code"], result["valid"], result["available"]) == ("EMPTY", True, True)
        assert result["message"] == "Opcional: puede quedar vacío"
    assert check("company_rfc", " ", {**admin_headers, **EN})["message"] == "Optional: can be left blank"
    # El correo del administrador sigue siendo obligatorio.
    email = check("company_admin_email", "")
    assert (email["code"], email["valid"], email["message"]) == ("EMPTY", False, "El correo es obligatorio")


def test_a_company_without_rfc_is_restored_beside_another_without_rfc(client, admin_headers):
    """El conflicto de RFC al restaurar solo existe si la empresa tiene uno."""
    company_id = foreign_company(client, admin_headers).json()["data"]["id"]
    assert client.delete(f"{COMPANIES}/{company_id}", headers=admin_headers).status_code == 200
    assert foreign_company(client, admin_headers, admin_email="admin@contoso.com").status_code == 201
    restored = client.post(f"{COMPANIES}/{company_id}/restore", headers=admin_headers)
    assert restored.status_code == 200, restored.text
    assert restored.json()["code"] == "COMPANY_RESTORED" and restored.json()["data"]["rfc"] is None


def test_a_company_without_rfc_is_searched_billed_and_read_by_the_integration_api(
    client, admin_headers, company_headers
):
    company_id = foreign_company(client, admin_headers).json()["data"]["id"]
    found = client.get(COMPANIES, params={"search": "northwind"}, headers=admin_headers).json()["data"]["items"]
    assert [(c["id"], c["rfc"]) for c in found] == [(company_id, None)]
    billing = client.get("/api/admin/billing/companies", params={"search": "northwind"}, headers=admin_headers)
    assert [(row["company_id"], row["rfc"]) for row in billing.json()["data"]["items"]] == [(company_id, None)]
    # La empresa de las pruebas («Mi empresa») nace sin RFC: la API de integración lo entrega en null.
    secret = new_key(client, company_headers)["secret"]
    assert call(client, secret, "/company").json()["data"]["rfc"] is None
