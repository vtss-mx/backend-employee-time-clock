"""Borrado lógico de las personas (regla 20 de la raíz, `app/core/soft_delete.py`): empleados, validadores y empresas.

Eliminar manda a «Eliminados» (con quién y cuándo), saca de listados, conteos, búsquedas y validaciones de datos únicos
(que se pueden volver a usar), conserva el historial y borra DE VERDAD los datos biométricos y las fotos (regla 13);
restaurar revisa de nuevo lo que pudo cambiar (409 `RESTORE_CONFLICT`, límites) y nunca regresa la biometría.
"""

from datetime import date, timedelta

from sqlalchemy import func, select

from app.core.clock import business_today
from app.core.database import SessionLocal
from app.core.soft_delete import WITH_DELETED, with_deleted
from app.models import (
    AuthSession,
    Company,
    Employee,
    EmployeeAbsence,
    EmployeeQr,
    EmployeeStatusEvent,
    FaceEmbedding,
    FaceEnrollment,
    FaceStatus,
    RememberedAccount,
    SessionRevocationReason,
    ShiftChangeRequest,
    StorageDeletion,
    User,
    UserAvatar,
    Validator,
    ValidatorStatusEvent,
)
from tests.avatar_support import upload
from tests.conftest import (
    COMPANY_EMAIL,
    create_company,
    create_employee,
    curp_for,
    login,
    nss_for,
    phone_for,
    rfc_for,
)
from tests.test_shifts import create_shift
from tests.test_validators import PASSWORD as VALIDATOR_PASSWORD
from tests.test_validators import URL as VALIDATORS
from tests.test_validators import approved, create_validator

EMPLOYEES = "/api/employees"
COMPANIES = "/api/admin/companies"


def _queue() -> set[str]:
    with SessionLocal() as db:
        return set(db.scalars(select(StorageDeletion.object_name)))


def _count(model, *conditions) -> int:
    with SessionLocal() as db:
        return int(db.scalar(with_deleted(select(func.count()).select_from(model).where(*conditions))) or 0)


# ---------------------------------------------------------------- el mecanismo


def test_queries_only_see_live_rows_unless_they_ask_for_the_deleted(client, company_headers):
    ana = create_employee(client, company_headers, number="EMP-001", email="ana@empresa.com").json()["data"]
    create_employee(client, company_headers, number="EMP-002", email="luis@empresa.com")
    assert client.delete(f"{EMPLOYEES}/{ana['id']}", headers=company_headers).status_code == 200
    with SessionLocal() as db:
        # Consultas, conteos, `get` y UPDATE masivos: solo lo vigente.
        assert db.scalar(select(func.count()).select_from(Employee)) == 1
        assert db.scalar(with_deleted(select(func.count()).select_from(Employee))) == 2
        assert db.get(Employee, ana["id"]) is None
        employee = db.get(Employee, ana["id"], execution_options=WITH_DELETED)
        assert employee is not None and employee.deleted and employee.deleted_by == COMPANY_EMAIL
        # La referencia muchos a uno del historial carga lo eliminado (su cuenta); una recarga también.
        assert employee.user.email == "ana@empresa.com" and employee.user.deleted
        db.refresh(employee)
        # La colección de la cuenta no trae el empleo eliminado (no se elige al iniciar sesión).
        assert db.get(User, employee.user_id, execution_options=WITH_DELETED).employees == []


def test_the_history_keeps_naming_a_deleted_employee(client, company_headers):
    """Una referencia muchos a uno (el QR que se usó) sigue cargando al empleado eliminado."""
    ana = create_employee(client, company_headers).json()["data"]
    from tests.conftest import qr_content

    qr_content(ana["id"])
    assert client.delete(f"{EMPLOYEES}/{ana['id']}", headers=company_headers).status_code == 200
    with SessionLocal() as db:
        qr = db.scalar(select(EmployeeQr).where(EmployeeQr.employee_id == ana["id"]))
        assert qr is not None and qr.active is False  # su QR dejó de servir
        assert qr.employee.full_name == "Juan Pérez" and qr.employee.deleted


# ---------------------------------------------------------------- empleados


def test_deleting_an_employee_sends_it_to_the_trash_and_keeps_its_history(client, company_headers, bucket):
    data = approved(client, company_headers, "ana", number="EMP-001")
    employee_id = data["id"]
    headers = login(client, "ana@empresa.com", "Empleado123")
    assert upload(client, headers).status_code == 200  # su foto de perfil
    with SessionLocal() as db:
        photo = db.scalar(select(FaceEnrollment.photo_object).where(FaceEnrollment.employee_id == employee_id))
        avatars = set(db.scalars(select(UserAvatar.object_name).where(UserAvatar.user_id == data["user_id"])))
    assert photo and avatars

    deleted = client.delete(f"{EMPLOYEES}/{employee_id}", headers=company_headers)
    assert deleted.status_code == 200 and deleted.json()["message"] == "Empleado eliminado"
    # Fuera de los listados, búsquedas y conteos; en la papelera, con quién y cuándo.
    listed = client.get(EMPLOYEES, headers=company_headers).json()["data"]
    assert listed["total"] == 0
    assert client.get(EMPLOYEES, params={"search": "EMP-001"}, headers=company_headers).json()["data"]["total"] == 0
    trash = client.get(EMPLOYEES, params={"deleted": "true"}, headers=company_headers).json()["data"]
    assert [e["id"] for e in trash["items"]] == [employee_id] and trash["total"] == 1
    assert trash["items"][0]["deleted_by"] == COMPANY_EMAIL and trash["items"][0]["deleted_at"]
    found = client.get(EMPLOYEES, params={"deleted": "true", "search": "ana@"}, headers=company_headers)
    assert found.json()["data"]["total"] == 1  # la papelera también se busca
    # Su detalle se ve con la marca; todo lo demás responde como si no existiera.
    detail = client.get(f"{EMPLOYEES}/{employee_id}", headers=company_headers).json()["data"]
    assert detail["deleted_at"] and detail["face_status"] == FaceStatus.NOT_ENROLLED and detail["avatar"] is None
    assert (
        client.put(f"{EMPLOYEES}/{employee_id}", json={"first_name": "X"}, headers=company_headers).status_code == 404
    )
    assert client.get(f"{EMPLOYEES}/{employee_id}/qr", headers=company_headers).status_code == 404
    again = client.delete(f"{EMPLOYEES}/{employee_id}", headers=company_headers)
    assert again.status_code == 409 and again.json()["code"] == "ALREADY_DELETED"
    # Biometría y fotos: borradas DE VERDAD (los objetos, a la cola de borrado del bucket).
    assert _count(FaceEmbedding, FaceEmbedding.employee_id == employee_id) == 0
    assert _count(FaceEnrollment, FaceEnrollment.employee_id == employee_id) == 0
    assert _count(UserAvatar, UserAvatar.user_id == data["user_id"]) == 0
    assert {photo, *avatars} <= _queue()
    # Su cuenta (sin otro empleo) también se elimina: no entra y sus sesiones se cerraron.
    assert client.get("/api/users/me", headers=headers).status_code == 401
    login_again = client.post("/api/auth/login", json={"email": "ana@empresa.com", "password": "Empleado123"})
    assert login_again.status_code == 401
    with SessionLocal() as db:
        sessions = db.scalars(select(AuthSession).where(AuthSession.user_id == data["user_id"])).all()
        assert all(s.revoked_at for s in sessions)
        assert SessionRevocationReason.EMPLOYEE_REMOVED in {s.revoked_reason for s in sessions}
        assert db.scalar(select(func.count()).select_from(RememberedAccount)) == 0
        events = db.scalars(select(EmployeeStatusEvent.active).where(EmployeeStatusEvent.employee_id == employee_id))
        assert list(events) == [True, False]  # deja de cobrarse desde hoy


def test_deleting_an_employee_cancels_what_was_pending(client, company_headers):
    data = approved(client, company_headers, "ana", number="EMP-001")
    headers = login(client, "ana@empresa.com", "Empleado123")
    night = create_shift(client, company_headers, name="Nocturno", start_time="22:00", end_time="06:00")
    later = business_today() + timedelta(days=5)
    request = {"shift_id": night["id"], "valid_from": later.isoformat(), "reason": "Estudios"}
    assert client.post("/api/me/shift-requests", json=request, headers=headers).status_code == 201
    leave = {"type": "VACATION", "starts_on": later.isoformat(), "ends_on": later.isoformat()}
    assert client.post("/api/me/absences", json=leave, headers=headers).status_code == 201
    department = client.post("/api/departments", json={"name": "Ventas"}, headers=company_headers).json()["data"]
    managers = f"/api/departments/{department['id']}/managers"
    assert client.post(managers, json={"employee_id": data["id"]}, headers=company_headers).status_code == 200

    assert client.delete(f"{EMPLOYEES}/{data['id']}", headers=company_headers).status_code == 200
    with SessionLocal() as db:
        assert set(db.scalars(select(ShiftChangeRequest.status))) == {"CANCELLED"}
        assert set(db.scalars(select(EmployeeAbsence.status))) == {"CANCELLED"}
    shown = client.get(f"/api/departments/{department['id']}", headers=company_headers).json()["data"]
    assert shown["managers"] == []
    for counter in ("/api/shift-requests/summary", "/api/calendar/absences/summary"):  # contadores del menú
        assert client.get(counter, headers=company_headers).json()["data"]["pending"] == 0


def test_a_deleted_employee_frees_its_identifiers(client, company_headers):
    first = create_employee(client, company_headers).json()["data"]
    assert client.delete(f"{EMPLOYEES}/{first['id']}", headers=company_headers).status_code == 200
    # Un empleado nuevo con el MISMO correo, teléfono, número, RFC, CURP y NSS (índices únicos parciales).
    again = create_employee(client, company_headers)
    assert again.status_code == 201, again.text
    # Validación en vivo: el dato del eliminado ya no está ocupado... pero el del nuevo sí.
    params = {"field": "employee_number", "value": "EMP-001"}
    assert client.get("/api/validation", params=params, headers=company_headers).json()["data"]["available"] is False


def test_restoring_an_employee_checks_what_changed(client, company_headers):
    ana = create_employee(client, company_headers, number="EMP-001", email="ana@empresa.com").json()["data"]
    url = f"{EMPLOYEES}/{ana['id']}"
    live = client.post(f"{url}/restore", headers=company_headers)
    assert live.status_code == 409 and live.json()["code"] == "NOT_DELETED"
    assert client.delete(url, headers=company_headers).status_code == 200
    # Cada dato que otro empleado vigente ya tomó, con su campo.
    conflicts = {
        "employee_number": {
            "number": "EMP-001",
            "email": "b@empresa.com",
            "phone": phone_for("B"),
            "rfc": rfc_for("B"),
        },
        "rfc": {"number": "EMP-B", "email": "b@empresa.com", "rfc": rfc_for("EMP-001")},
    }
    for field, body in conflicts.items():
        other = create_employee(client, company_headers, **body).json()["data"]
        refused = client.post(f"{url}/restore", headers=company_headers)
        assert refused.status_code == 409 and refused.json()["code"] == "RESTORE_CONFLICT", field
        assert refused.json()["errors"][0]["field"] == field
        assert client.delete(f"{EMPLOYEES}/{other['id']}", headers=company_headers).status_code == 200
    restored = client.post(f"{url}/restore", headers=company_headers)
    assert restored.status_code == 200, restored.text
    body = restored.json()
    assert body["message"] == "Empleado restaurado. Debe registrar su rostro de nuevo."
    assert body["data"]["deleted_at"] is None and body["data"]["face_status"] == FaceStatus.NOT_ENROLLED
    headers = login(client, "ana@empresa.com", "Empleado123")  # su cuenta volvió con él
    me = client.get("/api/users/me", headers=headers).json()["data"]
    assert me["home"]  # entra (a registrar su rostro de nuevo)


def test_restoring_checks_each_document_and_the_account(client, company_headers):
    ana = create_employee(client, company_headers, number="EMP-001", email="ana@empresa.com").json()["data"]
    url = f"{EMPLOYEES}/{ana['id']}"
    assert client.delete(url, headers=company_headers).status_code == 200
    cases = (
        ("curp", {"number": "B-1", "email": "b1@empresa.com", "rfc": rfc_for("B-1")}, {"curp": curp_for("EMP-001")}),
        ("nss", {"number": "B-2", "email": "b2@empresa.com", "rfc": rfc_for("B-2")}, {"nss": nss_for("EMP-001")}),
        ("email", {"number": "B-3", "email": "ana@empresa.com", "rfc": rfc_for("B-3")}, {}),
        (
            "phone",
            {"number": "B-4", "email": "b4@empresa.com", "rfc": rfc_for("B-4"), "phone": phone_for("EMP-001")},
            {},
        ),
    )
    for field, body, update in cases:
        other = create_employee(client, company_headers, **body)
        assert other.status_code == 201, other.text
        other_id = other.json()["data"]["id"]
        if update:
            edited = client.put(f"{EMPLOYEES}/{other_id}", json=update, headers=company_headers)
            assert edited.status_code == 200, edited.text
        refused = client.post(f"{url}/restore", headers=company_headers)
        assert refused.json()["code"] == "RESTORE_CONFLICT" and refused.json()["errors"][0]["field"] == field, field
        assert client.delete(f"{EMPLOYEES}/{other_id}", headers=company_headers).status_code == 200


def test_an_inactive_employee_comes_back_inactive_and_is_not_charged(client, company_headers):
    ana = create_employee(client, company_headers).json()["data"]
    url = f"{EMPLOYEES}/{ana['id']}"
    assert client.patch(f"{url}/status", json={"active": False}, headers=company_headers).status_code == 200
    assert client.delete(url, headers=company_headers).status_code == 200
    restored = client.post(f"{url}/restore", headers=company_headers).json()["data"]
    assert restored["active"] is False
    with SessionLocal() as db:
        events = db.scalars(select(EmployeeStatusEvent.active).where(EmployeeStatusEvent.employee_id == ana["id"]))
        assert list(events) == [True, False]  # ni la eliminación ni la restauración de uno inactivo cambian el cobro


def test_a_restore_that_collides_at_commit_is_a_conflict(monkeypatch):
    """Dos peticiones que restauran o dan de alta el mismo dato a la vez: el índice único parcial rechaza la segunda al
    confirmar y responde 409 RESTORE_CONFLICT (nada cambia)."""
    import pytest
    from sqlalchemy.exc import IntegrityError

    from app.core.exceptions import ConflictError
    from app.services.trash import commit_restore

    class Clash:
        rolled_back = False

        def commit(self) -> None:
            raise IntegrityError("UPDATE", {}, Exception("duplicate key"))

        def rollback(self) -> None:
            self.rolled_back = True

    session = Clash()
    with pytest.raises(ConflictError) as raised:
        commit_restore(session)  # type: ignore[arg-type]
    assert raised.value.code == "RESTORE_CONFLICT" and session.rolled_back


def test_restoring_respects_the_employee_limit(client, company_headers):
    ana = create_employee(client, company_headers).json()["data"]
    assert client.delete(f"{EMPLOYEES}/{ana['id']}", headers=company_headers).status_code == 200
    create_employee(client, company_headers, number="EMP-2", email="b@empresa.com")
    with SessionLocal() as db:
        db.scalar(select(Company)).max_employees = 1
        db.commit()
    full = client.post(f"{EMPLOYEES}/{ana['id']}/restore", headers=company_headers)
    assert full.status_code == 409 and full.json()["code"] == "EMPLOYEE_LIMIT_REACHED"


def test_a_person_in_two_companies_keeps_the_account_until_the_last_job(client, admin_headers, company_headers):
    from tests.test_multi_company import EMAIL, PASSWORD, PHONE

    create_company(client, admin_headers)
    other = login(client, "admin@panificadora.com", "Empresa1234")
    in_a = create_employee(client, company_headers).json()["data"]
    in_b = create_employee(client, other, number="PAN-7", phone=PHONE).json()["data"]
    assert in_a["user_id"] == in_b["user_id"]
    assert client.delete(f"{EMPLOYEES}/{in_a['id']}", headers=company_headers).status_code == 200
    # Sigue entrando a B: su cuenta no se eliminó.
    assert client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD}).status_code == 200
    # A la vuelve a contratar con la MISMA cuenta (empleo nuevo): el anterior ya no se puede restaurar.
    rehired = create_employee(client, company_headers, number="EMP-9", phone=PHONE)
    assert rehired.status_code == 201 and rehired.json()["data"]["user_id"] == in_a["user_id"]
    twice = client.post(f"{EMPLOYEES}/{in_a['id']}/restore", headers=company_headers)
    assert twice.status_code == 409 and twice.json()["errors"][0]["field"] == "email"
    # Su último empleo: la cuenta va a «Eliminados»; restaurar el empleo la regresa.
    assert client.delete(f"{EMPLOYEES}/{rehired.json()['data']['id']}", headers=company_headers).status_code == 200
    assert client.delete(f"{EMPLOYEES}/{in_b['id']}", headers=other).status_code == 200
    assert client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD}).status_code == 401
    assert client.post(f"{EMPLOYEES}/{in_b['id']}/restore", headers=other).status_code == 200
    assert client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD}).status_code == 200


def test_a_job_restored_while_the_account_kept_working_elsewhere(client, admin_headers, company_headers):
    """Su cuenta nunca se eliminó (trabaja en otra empresa): restaurar el empleo solo lo regresa a esta empresa."""
    from tests.test_multi_company import EMAIL, PASSWORD, PHONE

    create_company(client, admin_headers)
    other = login(client, "admin@panificadora.com", "Empresa1234")
    in_a = create_employee(client, company_headers).json()["data"]
    assert create_employee(client, other, number="PAN-7", phone=PHONE).status_code == 201
    assert client.delete(f"{EMPLOYEES}/{in_a['id']}", headers=company_headers).status_code == 200
    restored = client.post(f"{EMPLOYEES}/{in_a['id']}/restore", headers=company_headers)
    assert restored.status_code == 200 and restored.json()["data"]["shared_account"] is True
    user = client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD}).json()["data"]["user"]
    assert len(user["memberships"]) == 2


def test_the_employee_trash_needs_the_employees_screen(client, company_headers):
    from app.models import RoleScreen
    from app.services.catalog_service import clear_catalog_cache

    with SessionLocal() as db:
        row = db.get(RoleScreen, ("COMPANY", "COMPANY_EMPLOYEES"))
        db.delete(row)
        db.commit()
    clear_catalog_cache()
    assert client.get(EMPLOYEES, headers=company_headers).status_code == 200  # la usan otras pantallas
    denied = client.get(EMPLOYEES, params={"deleted": "true"}, headers=company_headers)
    assert denied.status_code == 403 and denied.json()["code"] == "FORBIDDEN"


def test_messages_in_english(client, company_headers):
    english = {**company_headers, "Accept-Language": "en-US"}
    ana = create_employee(client, company_headers).json()["data"]
    url = f"{EMPLOYEES}/{ana['id']}"
    assert client.delete(url, headers=english).json()["message"] == "Employee deleted"
    assert client.delete(url, headers=english).json()["message"] == "This record is already in Deleted"
    create_employee(client, company_headers, email="otra@empresa.com", phone=phone_for("X"), rfc=rfc_for("X"))
    refused = client.post(f"{url}/restore", headers=english).json()
    assert refused["message"] == "Can't restore: another employee already has the number EMP-001"


# ---------------------------------------------------------------- validadores


def test_deleting_and_restoring_a_validator(client, company_headers):
    assert create_validator(client, company_headers).status_code == 201
    headers = login(client, "recepcion@empresa.com", VALIDATOR_PASSWORD)
    assert upload(client, headers).status_code == 200
    validator = client.get(VALIDATORS, headers=company_headers).json()["data"]["items"][0]
    url = f"{VALIDATORS}/{validator['id']}"
    assert client.delete(url, headers=company_headers).json()["code"] == "VALIDATOR_DELETED"
    # Fuera del listado y del límite; su cuenta no entra; su foto se borró; su correo queda libre.
    listed = client.get(VALIDATORS, headers=company_headers).json()["data"]
    assert listed["total"] == 0 and listed["active"] == 0
    assert client.get("/api/users/me", headers=headers).status_code == 401
    with SessionLocal() as db:
        user_id = db.scalar(with_deleted(select(Validator.user_id)))
        assert db.scalar(select(func.count()).select_from(UserAvatar)) == 0
        reasons = set(db.scalars(select(AuthSession.revoked_reason).where(AuthSession.user_id == user_id)))
        assert reasons == {SessionRevocationReason.ACCOUNT_DELETED}
        events = list(db.scalars(select(ValidatorStatusEvent.active).order_by(ValidatorStatusEvent.id)))
        assert events == [True, False]
    trash = client.get(VALIDATORS, params={"deleted": "true"}, headers=company_headers).json()["data"]
    assert [v["id"] for v in trash["items"]] == [validator["id"]] and trash["items"][0]["deleted_by"] == COMPANY_EMAIL
    assert client.get(url, headers=company_headers).json()["data"]["deleted_at"]
    assert client.delete(url, headers=company_headers).json()["code"] == "ALREADY_DELETED"
    # Otro validador con el mismo correo: el eliminado ya no se puede restaurar.
    assert create_validator(client, company_headers, name="Otra").status_code == 201
    taken = client.post(f"{url}/restore", headers=company_headers).json()
    assert taken["code"] == "RESTORE_CONFLICT" and taken["errors"][0]["field"] == "email"
    replacement = client.get(VALIDATORS, headers=company_headers).json()["data"]["items"][0]
    assert client.delete(f"{VALIDATORS}/{replacement['id']}", headers=company_headers).status_code == 200
    restored = client.post(f"{url}/restore", headers=company_headers)
    assert restored.status_code == 200 and restored.json()["code"] == "VALIDATOR_RESTORED"
    assert restored.json()["data"]["active"] is True
    assert client.post(f"{url}/restore", headers=company_headers).json()["code"] == "NOT_DELETED"
    assert login(client, "recepcion@empresa.com", VALIDATOR_PASSWORD)


def _validator_id(client, headers, email: str) -> int:
    items = client.get(VALIDATORS, headers=headers).json()["data"]["items"]
    return next(v["id"] for v in items if v["email"] == email)


def test_restoring_a_validator_respects_the_limit(client, company_headers):
    # Uno activo y otro inactivo, los dos en «Eliminados»; el límite queda lleno con uno nuevo.
    assert create_validator(client, company_headers).status_code == 201
    assert create_validator(client, company_headers, email="noche@empresa.com", name="Noche").status_code == 201
    active = _validator_id(client, company_headers, "recepcion@empresa.com")
    inactive = _validator_id(client, company_headers, "noche@empresa.com")
    assert client.patch(f"{VALIDATORS}/{inactive}/status", json={"active": False}, headers=company_headers)
    for validator_id in (active, inactive):
        assert client.delete(f"{VALIDATORS}/{validator_id}", headers=company_headers).status_code == 200
    with SessionLocal() as db:
        db.scalar(select(Company)).max_validators = 1
        db.commit()
    assert create_validator(client, company_headers, email="otra@empresa.com", name="Otra").status_code == 201
    full = client.post(f"{VALIDATORS}/{active}/restore", headers=company_headers)
    assert full.status_code == 409 and full.json()["code"] == "VALIDATOR_LIMIT_REACHED"
    # Uno inactivo no ocupa lugar: se restaura (inactivo) aunque el límite esté lleno.
    back = client.post(f"{VALIDATORS}/{inactive}/restore", headers=company_headers)
    assert back.status_code == 200 and back.json()["data"]["active"] is False


# ---------------------------------------------------------------- empresas (ADMIN)


def _company_with_people(client, admin_headers) -> tuple[int, dict]:
    created = create_company(client, admin_headers, max_validators=3).json()["data"]
    headers = login(client, "admin@panificadora.com", "Empresa1234")
    assert upload(client, headers).status_code == 200
    assert create_validator(client, headers, email="caseta@panificadora.com").status_code == 201
    return created["id"], headers


def test_deleting_and_restoring_a_company_with_its_accounts(client, admin_headers):
    company_id, headers = _company_with_people(client, admin_headers)
    url = f"{COMPANIES}/{company_id}"
    assert client.delete(url, headers=admin_headers).json()["code"] == "COMPANY_DELETED"
    # Sus cuentas: sin sesión, sin foto, en «Eliminados»; su identificador fiscal y sus correos quedan libres.
    assert client.get("/api/users/me", headers=headers).status_code == 401
    denied = client.post("/api/auth/login", json={"email": "admin@panificadora.com", "password": "Empresa1234"})
    assert denied.status_code == 401
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(UserAvatar)) == 0
        assert db.scalar(select(func.count()).select_from(User).where(User.company_id == company_id)) == 0
        assert db.scalar(with_deleted(select(func.count()).select_from(Validator))) == 1
        assert list(db.scalars(select(ValidatorStatusEvent.active).order_by(ValidatorStatusEvent.id))) == [True, False]
    listed = client.get(COMPANIES, headers=admin_headers).json()["data"]
    assert company_id not in [c["id"] for c in listed["items"]]
    trash = client.get(COMPANIES, params={"deleted": "true"}, headers=admin_headers).json()["data"]
    assert [c["id"] for c in trash["items"]] == [company_id] and trash["items"][0]["deleted_by"]
    assert client.get(url, headers=admin_headers).json()["data"]["deleted_at"]
    assert client.put(url, json={"name": "Otra"}, headers=admin_headers).status_code == 404
    assert client.delete(url, headers=admin_headers).json()["code"] == "ALREADY_DELETED"
    # Otra empresa con su identificador fiscal: no se restaura; luego, con el correo de su administrador.
    clash = create_company(client, admin_headers, admin_email="otro@panificadora.com").json()["data"]
    refused = client.post(f"{url}/restore", headers=admin_headers).json()
    assert refused["code"] == "RESTORE_CONFLICT" and refused["errors"][0]["field"] == "tax_id"
    assert (
        refused["message"] == "No se puede restaurar: otra empresa ya tiene ese identificador fiscal (RFC PNO120315AB1)"
    )
    assert client.delete(f"{COMPANIES}/{clash['id']}", headers=admin_headers).status_code == 200
    clash = create_company(client, admin_headers, rfc="XAX010101000").json()["data"]
    refused = client.post(f"{url}/restore", headers=admin_headers).json()
    assert refused["code"] == "RESTORE_CONFLICT" and refused["errors"][0]["field"] == "email"
    assert client.delete(f"{COMPANIES}/{clash['id']}", headers=admin_headers).status_code == 200
    restored = client.post(f"{url}/restore", headers=admin_headers)
    assert restored.status_code == 200 and restored.json()["code"] == "COMPANY_RESTORED"
    assert restored.json()["data"]["deleted_at"] is None and restored.json()["data"]["active_validators"] == 1
    assert login(client, "admin@panificadora.com", "Empresa1234")
    assert client.post(f"{url}/restore", headers=admin_headers).json()["code"] == "NOT_DELETED"
    with SessionLocal() as db:
        assert list(db.scalars(select(ValidatorStatusEvent.active).order_by(ValidatorStatusEvent.id))) == [
            True,
            False,
            True,
        ]


def test_a_company_trash_brings_back_only_what_left_with_it(client, admin_headers):
    company_id, headers = _company_with_people(client, admin_headers)
    validator = client.get(VALIDATORS, headers=headers).json()["data"]["items"][0]
    assert client.delete(f"{VALIDATORS}/{validator['id']}", headers=headers).status_code == 200  # antes, aparte
    assert client.delete(f"{COMPANIES}/{company_id}", headers=admin_headers).status_code == 200
    assert client.post(f"{COMPANIES}/{company_id}/restore", headers=admin_headers).status_code == 200
    headers = login(client, "admin@panificadora.com", "Empresa1234")
    assert client.get(VALIDATORS, headers=headers).json()["data"]["total"] == 0  # el que se eliminó antes, no


def test_a_deleted_company_is_not_charged(client, admin_headers):
    from datetime import UTC, datetime

    from app.repositories.billing_repository import BillingRepository
    from tests.billing_support import company_with_plan

    company_id = company_with_plan(client, admin_headers)
    with SessionLocal() as db:
        repo = BillingRepository(db)
        assert company_id in repo.plans_due(date(2100, 1, 1), 100)
        assert company_id in [p.company_id for p in repo.plans_to_forecast(datetime(2100, 1, 1, tzinfo=UTC), 100)]
    assert client.delete(f"{COMPANIES}/{company_id}", headers=admin_headers).status_code == 200
    with SessionLocal() as db:
        repo = BillingRepository(db)
        assert company_id not in repo.plans_due(date(2100, 1, 1), 100)
        assert company_id not in [p.company_id for p in repo.plans_to_forecast(datetime(2100, 1, 1, tzinfo=UTC), 100)]


def test_the_company_trash_needs_the_companies_screen(client, admin_headers):
    from app.models import RoleScreen
    from app.services.catalog_service import clear_catalog_cache

    with SessionLocal() as db:
        row = db.get(RoleScreen, ("ADMIN", "ADMIN_COMPANIES"))
        db.delete(row)
        db.commit()
    clear_catalog_cache()
    assert client.get(COMPANIES, headers=admin_headers).status_code == 200  # el tablero también lista
    assert client.get(COMPANIES, params={"deleted": "true"}, headers=admin_headers).status_code == 403
