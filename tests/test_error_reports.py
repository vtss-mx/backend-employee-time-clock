"""Errores del sistema: CUALQUIER error del backend queda en la base de datos y el ADMIN le da
seguimiento (pendiente, en proceso, en revisión, solucionado)."""

import json
import logging
from datetime import UTC, datetime, timedelta

from app.core.database import SessionLocal
from app.core.error_events import ErrorEvent
from app.models import ErrorOccurrence, ErrorReport
from app.services import maintenance_service
from app.services.error_reporter import ErrorLogHandler, ErrorReporter, error_reporter
from tests.conftest import create_employee, login, submit_enrollment

URL = "/api/admin/errors"


def _reports(client, admin_headers, **params) -> list[dict]:
    error_reporter.flush()
    return client.get(URL, params=params, headers=admin_headers).json()["data"]["items"]


def test_every_http_error_is_recorded_and_grouped(client, admin_headers, company_headers):
    first = client.get("/api/admin/companies", headers=company_headers)  # 403: no es su API
    client.get("/api/admin/companies", headers=company_headers)
    assert first.status_code == 403
    found = [r for r in _reports(client, admin_headers) if r["http_status"] == 403]
    assert len(found) == 1  # dos veces el mismo error = una fila
    report = found[0]
    assert (report["severity"], report["status"], report["source"], report["method"]) == (
        "WARNING",
        "PENDING",
        "HTTP",
        "GET",
    )
    assert report["location"] == "/api/admin/companies" and report["occurrences"] == 2
    assert report["last_trace_id"] in {first.headers["X-Request-ID"], report["last_trace_id"]}

    occurrences = client.get(f"{URL}/{report['id']}/occurrences", headers=admin_headers).json()["data"]
    assert occurrences["total"] == 2
    assert occurrences["items"][0]["user_label"] == "admin@empresa.com (Company)"  # quién lo provocó
    assert occurrences["items"][-1]["trace_id"] == first.headers["X-Request-ID"]


def test_the_location_is_the_route_template_and_unknown_urls_share_one_report(
    client, admin_headers, company_headers, monkeypatch
):
    client.get("/api/employees/999999", headers=company_headers)  # 404 de negocio en una ruta real
    for url in ("/api/no-existe/1", "/api/wp-admin/setup.php", "/.env"):  # un escáner probando URLs
        client.get(url, headers=company_headers)
    reports = _reports(client, admin_headers)
    assert any(r["location"] == "/api/employees/{employee_id}" for r in reports)
    unknown = [r for r in reports if r["location"] == "(ruta inexistente)"]
    assert len(unknown) == 1 and unknown[0]["occurrences"] == 3

    # Si registrar el error falla, la respuesta sale igual (y la falla queda en el log del proceso).
    from app.middleware import request_id

    def broken(*_args):
        raise RuntimeError("registro roto")

    monkeypatch.setattr(request_id, "location_of", broken)
    response = client.get("/api/no-existe/2", headers=company_headers)
    assert response.status_code == 404 and response.json()["code"] == "NOT_FOUND"


def test_unexpected_errors_keep_their_stack_trace(client, admin_headers, company_headers, monkeypatch):
    from app.routers import catalogs

    def boom(*_args, **_kwargs):
        raise RuntimeError("se rompió algo")

    monkeypatch.setattr(catalogs, "get_catalogs", boom)
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


def test_admin_tracks_errors_and_a_resolved_one_reopens(client, admin_headers, company_headers):
    client.get("/api/admin/stats", headers=company_headers)
    report = next(r for r in _reports(client, admin_headers) if r["location"] == "/api/admin/stats")
    url = f"{URL}/{report['id']}/status"
    for status in ("IN_PROGRESS", "IN_REVIEW", "RESOLVED"):
        changed = client.patch(url, json={"status": status}, headers=admin_headers)
        assert changed.status_code == 200 and changed.json()["data"]["status"] == status
    assert changed.json()["data"]["status_changed_by"] == "superadmin@plataforma.com"
    assert client.patch(url, json={"status": "OTRO"}, headers=admin_headers).status_code == 422

    summary = client.get(f"{URL}/summary", headers=admin_headers).json()["data"]
    assert summary["by_status"].get("RESOLVED") == 1
    client.get("/api/admin/stats", headers=company_headers)  # vuelve a pasar: la corrección no bastó
    again = next(r for r in _reports(client, admin_headers) if r["id"] == report["id"])
    assert (again["status"], again["reopened"], again["occurrences"]) == ("PENDING", 1, 2)
    summary = client.get(f"{URL}/summary", headers=admin_headers).json()["data"]
    assert summary["pending"] >= 1 and summary["open_by_severity"]["WARNING"] >= 1

    filtered = client.get(URL, params={"status": "RESOLVED"}, headers=admin_headers).json()["data"]
    assert filtered["total"] == 0
    assert client.get(f"{URL}/999999", headers=admin_headers).json()["code"] == "ERROR_REPORT_NOT_FOUND"


def test_only_the_platform_admin_sees_errors(client, company_headers):
    assert client.get(URL, headers=company_headers).status_code == 403
    assert client.get(f"{URL}/summary").status_code == 401


def test_live_channel_errors_are_recorded(client, admin_headers, company_headers):
    token = company_headers["Authorization"].removeprefix("Bearer ")
    with client.websocket_connect("/api/ws/validation") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": token}))
        ws.receive_json()
        ws.send_text("{no-es-json")
        assert ws.receive_json()["code"] == "BAD_MESSAGE"
    report = next(r for r in _reports(client, admin_headers) if r["source"] == "WEBSOCKET")
    assert (report["code"], report["http_status"]) == ("BAD_MESSAGE", 400)


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


def test_old_occurrences_are_purged_but_the_report_stays(client, company_headers):
    client.get("/api/admin/stats", headers=company_headers)
    error_reporter.flush()
    with SessionLocal() as db:
        removed = maintenance_service.purge_expired(db, now=datetime.now(UTC) + timedelta(days=31))
        assert removed["ocurrencias de errores"] >= 1
        assert db.query(ErrorOccurrence).count() == 0 and db.query(ErrorReport).count() >= 1


def test_old_resolved_errors_are_purged_but_open_ones_stay(client, admin_headers, company_headers):
    client.get("/api/admin/stats", headers=company_headers)
    client.get("/api/admin/companies", headers=company_headers)
    stats, companies = (
        next(r for r in _reports(client, admin_headers) if r["location"] == path)
        for path in ("/api/admin/stats", "/api/admin/companies")
    )
    client.patch(f"{URL}/{stats['id']}/status", json={"status": "RESOLVED"}, headers=admin_headers)
    with SessionLocal() as db:
        removed = maintenance_service.purge_expired(db, now=datetime.now(UTC) + timedelta(days=181))
        assert removed["errores solucionados"] == 1
        assert {r.id for r in db.query(ErrorReport)} >= {companies["id"]}
        assert db.get(ErrorReport, stats["id"]) is None


def _occurrence(client, admin_headers, *, location: str, code: str | None = None) -> dict:
    report = next(
        r for r in _reports(client, admin_headers) if r["location"] == location and (code is None or r["code"] == code)
    )
    return client.get(f"{URL}/{report['id']}/occurrences", headers=admin_headers).json()["data"]["items"][0]


def test_every_error_keeps_its_literal_context_without_secrets(client, admin_headers, company_headers):
    """A quién le pasó, de qué empresa, qué pidió (método, URL, encabezados, cuerpo) y qué se le
    respondió, tal cual. Solo los secretos (contraseñas, tokens) se guardan como «[oculto]»."""
    client.post("/api/auth/login", json={"email": "nadie@empresa.com", "password": "MiClave123"})
    login_error = _occurrence(client, admin_headers, location="/api/auth/login")
    request = login_error["context"]["request"]
    assert request["method"] == "POST" and request["body"] == {"email": "nadie@empresa.com", "password": "[oculto]"}
    assert login_error["context"]["response"]["status"] == 401 and login_error["context"]["user"] is None
    assert "authorization" not in {k.lower() for k in request["headers"]}

    create_employee(client, company_headers)
    duplicated = create_employee(client, company_headers, number="EMP-002")  # mismo correo
    assert duplicated.status_code >= 400
    error = _occurrence(client, admin_headers, location="/api/employees", code=duplicated.json()["code"])
    context = error["context"]
    assert error["user_label"] == "admin@empresa.com (Company)" and error["company_name"]
    assert context["user"] == {"id": context["user"]["id"], "email": "admin@empresa.com", "role": "COMPANY"}
    assert (
        context["request"]["body"]["email"] == "juan@empresa.com"
        and context["request"]["body"]["password"] == "[oculto]"
    )
    assert context["response"]["body"]["code"] == duplicated.json()["code"]
    assert context["company_id"] and context["duration_ms"] >= 0

    client.get("/api/employees", params={"page": 0, "access_token": "secreto"}, headers=company_headers)
    paged = _occurrence(client, admin_headers, location="/api/employees", code="VALIDATION_ERROR")
    assert paged["context"]["request"]["query"] == {"page": "0", "access_token": "[oculto]"}


def test_uploaded_photos_are_summarized_never_stored(client, admin_headers, company_headers):
    create_employee(client, company_headers)
    employee = login(client, "juan@empresa.com", "Empleado123")
    rejected = submit_enrollment(client, employee, frontal=(b"noface", b"noface", b"noface"))
    assert rejected.status_code == 422
    occurrence = _occurrence(client, admin_headers, location="/api/enrollment/face")
    parts = occurrence["context"]["request"]["body"]
    photos = [p for p in parts if p["name"] == "images"]
    assert [p["size"] for p in photos] == [6, 6, 6] and photos[0] == {
        "name": "images",
        "filename": "f0.jpg",
        "content_type": "image/jpeg",
        "size": 6,
    }
    assert any(p["name"] == "challenge_id" and p["value"] for p in parts)
    assert occurrence["user_label"] == "juan@empresa.com (Employee)"


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
    from sqlalchemy.exc import OperationalError, SQLAlchemyError

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
