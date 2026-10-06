"""Errores del sistema: las FALLAS (del servidor, del segundo plano, del canal en vivo y de la app web)
quedan en la base de datos y el ADMIN les da seguimiento (pendiente, en proceso, en revisión,
solucionado). Un 4xx es un resultado normal: se responde con su código, queda en el log del proceso y
no llega a la bandeja."""

import json
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import OperationalError

from app.core.admission import admission
from app.core.database import SessionLocal
from app.core.error_events import ErrorEvent
from app.core.exceptions import ServiceUnavailableError
from app.dependencies import get_pipeline
from app.main import app
from app.models import ErrorOccurrence, ErrorReport
from app.routers import realtime
from app.services import maintenance_service
from app.services.auth_service import AuthService
from app.services.department_service import DepartmentService
from app.services.employee_service import EmployeeService
from app.services.error_reporter import ErrorLogHandler, ErrorReporter, error_reporter
from tests.conftest import create_employee

URL = "/api/admin/errors"


def _reports(client, admin_headers, **params) -> list[dict]:
    error_reporter.flush()
    return client.get(URL, params=params, headers=admin_headers).json()["data"]["items"]


def _break(monkeypatch, owner, name: str, error: type[Exception], *args, **kwargs) -> None:
    """Esa operación falla como falla una dependencia de verdad (una excepción nueva en cada llamada)."""

    def broken(*_args, **_kwargs):
        raise error(*args, **kwargs)

    monkeypatch.setattr(owner, name, broken)


def _database_down(monkeypatch, owner, name: str) -> None:
    """503 DATABASE_UNAVAILABLE controlado: una falla del servidor que sí va a la bandeja (ERROR)."""
    _break(
        monkeypatch, owner, name, ServiceUnavailableError, "La base de datos no responde", code="DATABASE_UNAVAILABLE"
    )


def test_server_failures_are_recorded_and_grouped(client, admin_headers, company_headers, monkeypatch):
    _database_down(monkeypatch, DepartmentService, "get")
    first = client.get("/api/departments/5", headers=company_headers)
    client.get("/api/departments/7", headers=company_headers)  # otro registro, la misma falla
    assert (first.status_code, first.json()["code"]) == (503, "DATABASE_UNAVAILABLE")
    found = [r for r in _reports(client, admin_headers) if r["http_status"] == 503]
    assert len(found) == 1  # dos veces el mismo error = una fila
    report = found[0]
    assert (report["severity"], report["status"], report["source"], report["method"]) == (
        "ERROR",
        "PENDING",
        "HTTP",
        "GET",
    )
    assert report["location"] == "/api/departments/{department_id}" and report["occurrences"] == 2
    assert report["code"] == "DATABASE_UNAVAILABLE" and report["exception_type"] is None  # controlada

    occurrences = client.get(f"{URL}/{report['id']}/occurrences", headers=admin_headers).json()["data"]
    assert occurrences["total"] == 2
    assert occurrences["items"][0]["user_label"] == "admin@empresa.com (Empresa)"  # quién lo provocó
    assert occurrences["items"][-1]["trace_id"] == first.headers["X-Request-ID"]


def test_client_errors_are_answered_and_logged_but_never_recorded(client, admin_headers, company_headers, caplog):
    """403, 404, 401, 422 y 429 son resultados normales: cada uno sale con su código y queda una línea
    en el log del proceso (con su traceId), pero la bandeja del ADMIN no se llena de ellos."""
    with caplog.at_level(logging.INFO, logger="app.access"):
        forbidden = client.get("/api/admin/companies", headers=company_headers)  # no es su API
        missing = client.get("/api/employees/999999", headers=company_headers)  # 404 de negocio
        unknown = client.get("/api/wp-admin/setup.php", headers=company_headers)  # una URL inventada
        anonymous = client.get("/api/users/me")
        invalid = client.post("/api/departments", json={"name": ""}, headers=company_headers)
    answered = [(r.status_code, r.json()["code"]) for r in (forbidden, missing, unknown, anonymous, invalid)]
    assert answered == [
        (403, "FORBIDDEN"),
        (404, "EMPLOYEE_NOT_FOUND"),
        (404, "NOT_FOUND"),
        (401, "UNAUTHORIZED"),
        (422, "VALIDATION_ERROR"),
    ]
    trace = forbidden.headers["X-Request-ID"]
    assert f"Respuesta 403 FORBIDDEN en GET /api/admin/companies [{trace}]" in caplog.text
    assert "Respuesta 404 EMPLOYEE_NOT_FOUND en GET /api/employees/999999" in caplog.text
    assert "Respuesta 422 VALIDATION_ERROR en POST /api/departments" in caplog.text
    assert [r for r in _reports(client, admin_headers) if r["source"] == "HTTP"] == []


def test_a_rate_limit_is_not_recorded_either(client, admin_headers, monkeypatch, caplog):
    from app.core.config import settings

    monkeypatch.setattr(settings, "RATE_LIMIT_LOGIN_PER_MINUTE", 1)
    payload = {"email": "nadie@empresa.com", "password": "Equivocada1"}
    with caplog.at_level(logging.INFO, logger="app.access"):
        assert client.post("/api/auth/login", json=payload).status_code == 401
        limited = client.post("/api/auth/login", json=payload)
    assert (limited.status_code, limited.json()["code"]) == (429, "RATE_LIMITED")
    assert "Respuesta 429 RATE_LIMITED en POST /api/auth/login" in caplog.text
    assert [r for r in _reports(client, admin_headers) if r["source"] == "HTTP"] == []


def test_the_reporter_discards_whatever_is_not_a_failure():
    """La regla vive en el registro mismo: ningún productor la rodea (un WARNING nunca se guarda)."""
    reporter = ErrorReporter(capacity=10)
    reporter.report(ErrorEvent(source="HTTP", severity="WARNING", code="FORBIDDEN", message="m", http_status=403))
    reporter.report(ErrorEvent(source="LOG", severity="ERROR", code="REAL", message="m"))
    assert reporter.flush() == 1
    with SessionLocal() as db:
        assert {r.code for r in db.query(ErrorReport)} == {"REAL"}


def test_the_location_is_the_route_template_even_before_routing(client, admin_headers, company_headers, monkeypatch):
    """Una falla antes de enrutar (la API saturada) usa la URL sin ids: no una fila por registro."""

    async def full(_group):
        return False

    monkeypatch.setattr(admission, "acquire", full)
    for employee_id in (12, 34):
        busy = client.get(f"/api/employees/{employee_id}/qr", headers=company_headers)
        assert (busy.status_code, busy.json()["code"]) == (503, "SERVER_BUSY")
    monkeypatch.undo()
    shed = [r for r in _reports(client, admin_headers) if r["code"] == "SERVER_BUSY"]
    assert [(r["location"], r["occurrences"], r["severity"]) for r in shed] == [("/api/employees/{id}/qr", 2, "ERROR")]


def test_a_failure_to_record_never_breaks_the_response(client, company_headers, monkeypatch, caplog):
    """Si registrar la falla falla, la respuesta sale igual (y queda en el log del proceso)."""
    from app.middleware import request_id

    _break(monkeypatch, request_id, "location_of", RuntimeError, "registro roto")
    _database_down(monkeypatch, DepartmentService, "get")
    response = client.get("/api/departments/3", headers=company_headers)
    assert response.status_code == 503 and response.json()["code"] == "DATABASE_UNAVAILABLE"
    assert "No se pudo registrar el error de GET /api/departments/3" in caplog.text


def test_unexpected_errors_keep_their_stack_trace(client, admin_headers, company_headers, monkeypatch):
    from app.routers import catalogs

    _break(monkeypatch, catalogs, "get_catalogs", RuntimeError, "se rompió algo")
    assert client.get("/api/catalogs", headers=company_headers).status_code == 500
    report = next(r for r in _reports(client, admin_headers) if r["code"] == "INTERNAL_ERROR")
    assert (report["severity"], report["exception_type"]) == ("CRITICAL", "RuntimeError")
    detail = client.get(f"{URL}/{report['id']}", headers=admin_headers).json()["data"]
    assert "Traceback" in detail["detail"] and "se rompió algo" in detail["detail"]


def test_background_errors_from_the_log_are_recorded(client, admin_headers):
    logger = logging.getLogger("app.services.maintenance_service")
    handler = ErrorLogHandler(error_reporter)
    logger.addHandler(handler)
    try:
        try:
            raise ValueError("lote dañado")
        except ValueError:
            logger.exception("Falló la depuración de prueba")
    finally:
        logger.removeHandler(handler)
    report = next(r for r in _reports(client, admin_headers, search="depuración de prueba"))
    assert (report["source"], report["severity"], report["code"]) == (
        "LOG",
        "CRITICAL",
        "app.services.maintenance_service",
    )
    assert report["exception_type"] == "ValueError" and report["location"].endswith(
        ".py:" + report["location"].split(":")[-1]
    )


def test_admin_tracks_errors_and_a_resolved_one_reopens(client, admin_headers, company_headers, monkeypatch):
    _database_down(monkeypatch, DepartmentService, "list_departments")
    client.get("/api/departments", headers=company_headers)
    report = next(r for r in _reports(client, admin_headers) if r["location"] == "/api/departments")
    url = f"{URL}/{report['id']}/status"
    for status in ("IN_PROGRESS", "IN_REVIEW", "RESOLVED"):
        changed = client.patch(url, json={"status": status}, headers=admin_headers)
        assert changed.status_code == 200 and changed.json()["data"]["status"] == status
    assert changed.json()["data"]["status_changed_by"] == "superadmin@plataforma.com"
    assert client.patch(url, json={"status": "OTRO"}, headers=admin_headers).status_code == 422

    summary = client.get(f"{URL}/summary", headers=admin_headers).json()["data"]
    assert summary["by_status"].get("RESOLVED") == 1
    client.get("/api/departments", headers=company_headers)  # vuelve a pasar: la corrección no bastó
    again = next(r for r in _reports(client, admin_headers) if r["id"] == report["id"])
    assert (again["status"], again["reopened"], again["occurrences"]) == ("PENDING", 1, 2)
    summary = client.get(f"{URL}/summary", headers=admin_headers).json()["data"]
    assert summary["pending"] >= 1 and summary["open_by_severity"]["ERROR"] >= 1

    filtered = client.get(URL, params={"status": "RESOLVED"}, headers=admin_headers).json()["data"]
    assert filtered["total"] == 0
    assert client.get(f"{URL}/999999", headers=admin_headers).json()["code"] == "ERROR_REPORT_NOT_FOUND"


def test_admin_resolves_every_error_of_a_specific_filter(client, admin_headers, company_headers, monkeypatch):
    _database_down(monkeypatch, DepartmentService, "get")
    _database_down(monkeypatch, DepartmentService, "update")
    client.get("/api/departments/1", headers=company_headers)  # dos fallas distintas
    client.put("/api/departments/1", json={"name": "Ventas"}, headers=company_headers)
    error_reporter.flush()
    params = {"severity": "ERROR", "search": "/api/departments/"}
    listed = client.get(URL, params=params, headers=admin_headers).json()["data"]
    assert listed["total"] == 2 and listed["as_of"]
    resolve = f"{URL}/resolve"

    # Nunca toda la bandeja: hace falta un estado o una gravedad específicos.
    everything = client.post(resolve, json={"seen_until": listed["as_of"]}, headers=admin_headers)
    assert everything.status_code == 422 and everything.json()["code"] == "ERROR_FILTER_REQUIRED"
    solved = client.post(resolve, json={"status": "RESOLVED", "seen_until": listed["as_of"]}, headers=admin_headers)
    assert solved.status_code == 422 and solved.json()["code"] == "ERROR_FILTER_RESOLVED"
    body = {**params, "seen_until": listed["as_of"]}
    naive = client.post(resolve, json={**body, "seen_until": "2026-01-01T00:00:00"}, headers=admin_headers)
    assert naive.status_code == 422 and naive.json()["code"] == "VALIDATION_ERROR"  # sin zona horaria
    assert client.post(resolve, json=body, headers=company_headers).status_code == 403

    # Vuelve a ocurrir después de que el ADMIN vio la lista: ese sigue abierto.
    client.get("/api/departments/1", headers=company_headers)
    error_reporter.flush()
    done = client.post(resolve, json=body, headers=admin_headers)
    assert done.status_code == 200 and done.json()["code"] == "ERRORS_RESOLVED"
    assert done.json()["data"] == {"resolved": 1}
    after = {r["method"]: r for r in _reports(client, admin_headers, search="/api/departments/")}
    assert after["PUT"]["status"] == "RESOLVED"
    assert after["PUT"]["status_changed_by"] == "superadmin@plataforma.com"
    assert after["GET"]["status"] == "PENDING"
    # Repetirlo no cambia nada más (ya están solucionados).
    again = client.post(resolve, json=body, headers=admin_headers)
    assert again.json()["data"] == {"resolved": 0}


def test_only_the_platform_admin_sees_errors(client, company_headers):
    assert client.get(URL, headers=company_headers).status_code == 403
    assert client.get(f"{URL}/summary").status_code == 401


def test_live_channel_failures_are_recorded_and_its_client_errors_only_logged(
    client, admin_headers, company_headers, monkeypatch, caplog
):
    token = company_headers["Authorization"].removeprefix("Bearer ")
    _break(monkeypatch, realtime, "_validate", OperationalError, "SELECT", {}, Exception("bd caída"))
    with (
        caplog.at_level(logging.INFO, logger=realtime.logger.name),
        client.websocket_connect("/api/ws/validation") as ws,
    ):
        ws.send_text(json.dumps({"type": "auth", "token": token}))
        ws.receive_json()
        ws.send_text("{no-es-json")
        assert ws.receive_json()["code"] == "BAD_MESSAGE"  # error del cliente: solo al log
        ws.send_text(json.dumps({"type": "validate", "field": "employee_number", "value": "EMP-1"}))
        assert ws.receive_json()["code"] == "DATABASE_UNAVAILABLE"  # falla del servidor: a la bandeja
    assert "Canal de validación: 400 BAD_MESSAGE" in caplog.text
    reports = [r for r in _reports(client, admin_headers) if r["source"] == "WEBSOCKET"]
    assert [(r["code"], r["http_status"], r["severity"]) for r in reports] == [("DATABASE_UNAVAILABLE", 503, "ERROR")]


def test_a_full_queue_counts_what_was_lost():
    reporter = ErrorReporter(capacity=100)
    for n in range(105):
        reporter.report(ErrorEvent(source="LOG", severity="ERROR", code=f"X{n % 2}", message="m"))
    assert reporter.dropped == 5 and reporter.flush() == 101  # 100 + el aviso de lo perdido
    with SessionLocal() as db:
        lost = db.query(ErrorReport).filter_by(code="ERROR_REPORTS_DROPPED").one()
        assert "5" in lost.message
        assert {r.code: r.occurrences for r in db.query(ErrorReport)}["X0"] == 50


def test_saving_errors_never_raises_when_the_database_fails(monkeypatch):
    from app.services import error_reporter as module

    def down():
        raise RuntimeError("BD caída")

    reporter = ErrorReporter(capacity=10)
    reporter.report(ErrorEvent(source="LOG", severity="ERROR", code="X", message="m"))
    monkeypatch.setattr(module, "SessionLocal", down)
    assert reporter.flush() == 0  # se registra en el log del proceso y sigue


def test_old_occurrences_are_purged_but_the_report_stays(client, company_headers, monkeypatch):
    _database_down(monkeypatch, DepartmentService, "list_departments")
    client.get("/api/departments", headers=company_headers)
    error_reporter.flush()
    with SessionLocal() as db:
        removed = maintenance_service.purge_expired(db, now=datetime.now(UTC) + timedelta(days=31))
        assert removed["ocurrencias de errores"] >= 1
        assert db.query(ErrorOccurrence).count() == 0 and db.query(ErrorReport).count() >= 1


def test_old_resolved_errors_are_purged_but_open_ones_stay(client, admin_headers, company_headers, monkeypatch):
    _database_down(monkeypatch, DepartmentService, "list_departments")
    _database_down(monkeypatch, DepartmentService, "get")
    client.get("/api/departments", headers=company_headers)
    client.get("/api/departments/9", headers=company_headers)
    listed, detail = (
        next(r for r in _reports(client, admin_headers) if r["location"] == path)
        for path in ("/api/departments", "/api/departments/{department_id}")
    )
    client.patch(f"{URL}/{listed['id']}/status", json={"status": "RESOLVED"}, headers=admin_headers)
    with SessionLocal() as db:
        removed = maintenance_service.purge_expired(db, now=datetime.now(UTC) + timedelta(days=181))
        assert removed["errores solucionados"] == 1
        assert {r.id for r in db.query(ErrorReport)} >= {detail["id"]}
        assert db.get(ErrorReport, listed["id"]) is None


def _occurrence(client, admin_headers, *, location: str, code: str | None = None) -> dict:
    report = next(
        r for r in _reports(client, admin_headers) if r["location"] == location and (code is None or r["code"] == code)
    )
    return client.get(f"{URL}/{report['id']}/occurrences", headers=admin_headers).json()["data"]["items"][0]


def test_every_failure_keeps_its_literal_context_without_secrets(client, admin_headers, company_headers, monkeypatch):
    """A quién le pasó, de qué empresa, qué pidió (método, URL, encabezados, cuerpo) y qué se le
    respondió, tal cual. Solo los secretos (contraseñas, tokens) se guardan como «[oculto]»."""
    _break(monkeypatch, AuthService, "authenticate", RuntimeError, "el verificador de contraseñas se cayó")
    client.post("/api/auth/login", json={"email": "nadie@empresa.com", "password": "MiClave123"})
    login_error = _occurrence(client, admin_headers, location="/api/auth/login")
    request = login_error["context"]["request"]
    assert request["method"] == "POST" and request["body"] == {"email": "nadie@empresa.com", "password": "[oculto]"}
    assert login_error["context"]["response"]["status"] == 500 and login_error["context"]["user"] is None
    assert "authorization" not in {k.lower() for k in request["headers"]}

    _break(monkeypatch, EmployeeService, "create", RuntimeError, "se cayó el alta")
    failed = create_employee(client, company_headers)
    assert failed.status_code == 500
    error = _occurrence(client, admin_headers, location="/api/employees", code="INTERNAL_ERROR")
    context = error["context"]
    assert error["user_label"] == "admin@empresa.com (Empresa)" and error["company_name"]
    assert context["user"] == {"id": context["user"]["id"], "email": "admin@empresa.com", "role": "COMPANY"}
    assert (
        context["request"]["body"]["email"] == "juan@empresa.com"
        and context["request"]["body"]["password"] == "[oculto]"
    )
    assert context["response"]["body"]["code"] == "INTERNAL_ERROR"
    assert context["company_id"] and context["duration_ms"] >= 0

    _database_down(monkeypatch, EmployeeService, "list_employees")
    client.get("/api/employees", params={"page": 1, "access_token": "secreto"}, headers=company_headers)
    paged = _occurrence(client, admin_headers, location="/api/employees", code="DATABASE_UNAVAILABLE")
    assert paged["context"]["request"]["query"] == {"page": "1", "access_token": "[oculto]"}
    assert paged["context"]["response"]["body"]["code"] == "DATABASE_UNAVAILABLE"


def test_uploaded_photos_are_summarized_never_stored(client, admin_headers, company_headers):
    """El motor facial no está disponible (503): de las fotos enviadas solo queda nombre, tipo y
    tamaño; los campos de texto del formulario sí se guardan."""

    def engine_down():
        raise ServiceUnavailableError("Motor facial no disponible", code="FACE_SERVICE_UNAVAILABLE")

    app.dependency_overrides[get_pipeline] = engine_down
    files = [("images", (f"f{i}.jpg", b"noface", "image/jpeg")) for i in range(3)]
    failed = client.post("/api/face/check", data={"allow_headwear": "true"}, files=files, headers=company_headers)
    assert (failed.status_code, failed.json()["code"]) == (503, "FACE_SERVICE_UNAVAILABLE")
    occurrence = _occurrence(client, admin_headers, location="/api/face/check")
    parts = occurrence["context"]["request"]["body"]
    photos = [p for p in parts if p["name"] == "images"]
    assert [p["size"] for p in photos] == [6, 6, 6] and photos[0] == {
        "name": "images",
        "filename": "f0.jpg",
        "content_type": "image/jpeg",
        "size": 6,
    }
    assert {"name": "allow_headwear", "value": "true"} in parts
    assert occurrence["user_label"] == "admin@empresa.com (Empresa)"


def test_messages_and_details_are_kept_literal(client, admin_headers):
    literal = "juan.perez@correo.com, CURP PEPJ900101HSRRRN09, tel +52 662 123 4567"
    reporter = ErrorReporter(capacity=10)
    reporter.report(
        ErrorEvent(
            source="LOG",
            severity="ERROR",
            code="LITERAL",
            message=f"Falló con {literal}\x00",
            method="PATCH" * 5,
            detail=literal,
        )
    )
    assert reporter.flush() == 1
    with SessionLocal() as db:
        saved = db.query(ErrorReport).filter_by(code="LITERAL").one()
        assert saved.message == f"Falló con {literal}" and saved.detail == literal  # sin el carácter nulo
        assert len(saved.method or "") <= 10
        assert db.query(ErrorOccurrence).filter_by(report_id=saved.id).one().context is None  # sin petición


def test_occurrences_of_deleted_accounts_still_say_who():
    from app.services.error_report_service import ErrorReportService

    assert ErrorReportService._account_label(999, {}) == "Cuenta #999 (ya no existe)"
    assert ErrorReportService._account_label(None, {}) is None


def test_one_unsavable_error_does_not_lose_the_rest(monkeypatch):
    """Cada error se guarda en su transacción: uno inválido se omite, un interbloqueo se reintenta y,
    si la BD sigue fallando, el guardado se detiene sin lanzar."""
    from sqlalchemy.exc import SQLAlchemyError

    from app.repositories.error_report_repository import ErrorReportRepository

    original = ErrorReportRepository.record
    deadlocks = {"BLOQUEO": 1, "CAIDA": 99}

    def record(self, fingerprint, events, keep):
        code = events[0].code
        if code == "MALO":
            raise SQLAlchemyError("valor inválido")
        if deadlocks.get(code, 0) > 0:
            deadlocks[code] -= 1
            raise OperationalError("UPDATE", {}, Exception("deadlock detected"))
        return original(self, fingerprint, events, keep)

    monkeypatch.setattr(ErrorReportRepository, "record", record)
    reporter = ErrorReporter(capacity=10)
    for code in ("MALO", "BUENO", "BLOQUEO"):
        reporter.report(ErrorEvent(source="LOG", severity="ERROR", code=code, message="m"))
    assert reporter.flush() == 2
    with SessionLocal() as db:
        assert {r.code for r in db.query(ErrorReport)} == {"BUENO", "BLOQUEO"}

    reporter.report(ErrorEvent(source="LOG", severity="ERROR", code="CAIDA", message="m"))
    assert reporter.flush() == 0 and deadlocks["CAIDA"] == 97  # un reintento y se detiene
