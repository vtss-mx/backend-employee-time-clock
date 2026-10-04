"""QR dinámicos: viven unos segundos, sirven UNA sola vez y todo el sistema lo respeta."""

from datetime import UTC, datetime, timedelta

from app.core.config import settings
from app.core.database import SessionLocal
from app.models import Employee, EmployeeQr, VerificationLog
from app.services.maintenance_service import purge_expired
from app.services.qr_service import LEGACY_PREFIX, QrService
from tests.conftest import approved_employee, qr_content
from tests.test_policy import URL as POLICY
from tests.test_policy import set_policy
from tests.test_validators import approved, identify_face, validator_headers

MY_QR = "/api/users/me/qr"
IDENTIFY = "/api/checkpoint/identify/qr"
INSPECT = "/api/checkpoint/qr/inspect"


def _employee_id(client, headers) -> int:
    return client.get("/api/users/me", headers=headers).json()["data"]["employee"]["id"]


def _identify(client, headers, content: str) -> dict:
    return client.post(IDENTIFY, json={"qr_content": content}, headers=headers).json()["data"]


def _latest(employee_id: int, **changes) -> int:
    """Cambia el último QR del empleado (p. ej. para simular el paso del tiempo); devuelve su id."""
    with SessionLocal() as db:
        qr = db.query(EmployeeQr).filter_by(employee_id=employee_id).order_by(EmployeeQr.id.desc()).first()
        assert qr is not None
        _change(qr.id, **changes)
        return qr.id


def _change(qr_id: int, **changes) -> None:
    with SessionLocal() as db:
        qr = db.get(EmployeeQr, qr_id)
        assert qr is not None
        for name, value in changes.items():
            setattr(qr, name, value)
        db.commit()


def test_employee_qr_renews_on_demand_and_reports_its_state(client, company_headers):
    headers = approved_employee(client, company_headers)
    set_policy(client, company_headers, qr_lifetime_seconds=45)

    first = client.post(MY_QR, headers=headers).json()["data"]
    assert first["lifetime_seconds"] == 45
    remaining = (datetime.fromisoformat(first["expires_at"]) - datetime.now(UTC)).total_seconds()
    assert 40 <= remaining <= 46
    assert client.get(f"{MY_QR}/{first['id']}", headers=headers).json()["data"]["status"] == "ACTIVE"

    second = client.post(MY_QR, headers=headers).json()["data"]  # bajo demanda: reemplaza al anterior
    assert client.get(f"{MY_QR}/{first['id']}", headers=headers).json()["data"]["status"] == "REVOKED"
    assert client.get(f"{MY_QR}/{second['id']}", headers=headers).json()["data"]["status"] == "ACTIVE"
    _latest(_employee_id(client, headers), expires_at=datetime.now(UTC) - timedelta(seconds=1))
    assert client.get(f"{MY_QR}/{second['id']}", headers=headers).json()["data"]["status"] == "EXPIRED"
    missing = client.get(f"{MY_QR}/999999", headers=headers)
    assert missing.status_code == 404 and missing.json()["code"] == "QR_NOT_FOUND"


def test_company_decides_how_long_each_qr_lives(client, company_headers):
    assert client.get(POLICY, headers=company_headers).json()["data"]["qr_lifetime_seconds"] == 30
    for invalid in (10, 301):
        assert client.put(POLICY, json={"qr_lifetime_seconds": invalid}, headers=company_headers).status_code == 422
    assert set_policy(client, company_headers, qr_lifetime_seconds=120)["qr_lifetime_seconds"] == 120


def test_a_qr_serves_once_and_never_again(client, company_headers):
    headers = approved_employee(client, company_headers)
    employee_id = _employee_id(client, headers)
    checkpoint = validator_headers(client, company_headers, mode="QR")

    content = qr_content(employee_id)
    assert _identify(client, checkpoint, content)["verified"] is True
    qr_id = _latest(employee_id)
    assert client.get(f"{MY_QR}/{qr_id}", headers=headers).json()["data"]["status"] == "USED"
    for _ in range(2):
        again = _identify(client, checkpoint, content)
        assert again["verified"] is False and "ya se usó" in again["message"]
    with SessionLocal() as db:
        reasons = [log.reason for log in db.query(VerificationLog).order_by(VerificationLog.id)]
    assert reasons == [None, "ALREADY_USED", "ALREADY_USED"]  # cada intento queda en la bitácora


def test_expired_replaced_and_printed_qrs_are_rejected(client, company_headers):
    headers = approved_employee(client, company_headers)
    employee_id = _employee_id(client, headers)
    checkpoint = validator_headers(client, company_headers, mode="QR")

    expired = qr_content(employee_id)
    _latest(employee_id, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    assert "venció" in _identify(client, checkpoint, expired)["message"]

    replaced = qr_content(employee_id)
    qr_content(employee_id)  # el empleado pidió otro
    assert "reemplazado" in _identify(client, checkpoint, replaced)["message"]

    printed = _identify(client, checkpoint, LEGACY_PREFIX + "x" * 43)  # credencial impresa anterior
    assert printed["verified"] is False and "impresos ya no son válidos" in printed["message"]
    assert _identify(client, checkpoint, "no-es-un-qr")["message"] == "QR inválido"


def test_company_sees_the_activity_and_can_invalidate_the_live_qr(client, company_headers):
    headers = approved_employee(client, company_headers)
    employee_id = _employee_id(client, headers)
    url = f"/api/employees/{employee_id}/qr"
    assert client.get(url, headers=company_headers).json()["data"] == {
        "live": False,
        "live_until": None,
        "last_issued_at": None,
        "last_used_at": None,
    }
    content = qr_content(employee_id)
    live = client.get(url, headers=company_headers).json()["data"]
    assert live["live"] is True and live["live_until"] and live["last_issued_at"] and live["last_used_at"] is None

    revoked = client.delete(url, headers=company_headers)
    assert revoked.json()["code"] == "QR_REVOKED" and revoked.json()["data"]["live"] is False
    checkpoint = validator_headers(client, company_headers, mode="QR")
    assert "reemplazado" in _identify(client, checkpoint, content)["message"]

    _identify(client, checkpoint, qr_content(employee_id))
    assert client.get(url, headers=company_headers).json()["data"]["last_used_at"] is not None
    assert client.get("/api/employees/999999/qr", headers=company_headers).status_code == 404


def test_two_simultaneous_reads_only_one_wins(client, company_headers):
    headers = approved_employee(client, company_headers)
    employee_id = _employee_id(client, headers)
    content = qr_content(employee_id)
    with SessionLocal() as db:
        company_id = db.get(Employee, employee_id).company_id
    with SessionLocal() as first, SessionLocal() as second:
        reader_a, reader_b = QrService(first), QrService(second)
        qr_a, _ = reader_a._find(content, company_id=company_id)
        qr_b, _ = reader_b._find(content, company_id=company_id)
        assert qr_a is not None and qr_b is not None
        assert reader_a.repo.mark_used(qr_a.id, actor_id=1, complete=True)
        first.commit()
        assert not reader_b.repo.mark_used(qr_b.id, actor_id=1, complete=True)  # llegó tarde


def test_qr_and_face_hold_is_for_one_validator_and_expires(client, company_headers):
    ana = approved(client, company_headers, "ana", number="EMP-002")
    first = validator_headers(client, company_headers, mode="QR_AND_FACE")
    second = validator_headers(client, company_headers, mode="QR_AND_FACE", email="comedor@empresa.com")

    held = qr_content(ana["id"])
    assert client.post(INSPECT, json={"qr_content": held}, headers=first).status_code == 200
    stolen = identify_face(client, second, "ana", qr=held).json()["data"]  # otro validador no lo completa
    assert stolen["verified"] is False and "ya se usó" in stolen["message"]

    late = qr_content(ana["id"])
    client.post(INSPECT, json={"qr_content": late}, headers=first)
    window = timedelta(seconds=settings.QR_FACE_WINDOW_SECONDS + 1)
    _latest(ana["id"], used_at=datetime.now(UTC) - window)
    expired = identify_face(client, first, "ana", qr=late).json()["data"]
    assert expired["verified"] is False and "venció" in expired["message"]

    # Sin el paso del escaneo, el rostro usa y cierra el QR en el mismo intento.
    direct = qr_content(ana["id"])
    assert identify_face(client, first, "ana", qr=direct).json()["data"]["verified"] is True
    assert "ya se usó" in identify_face(client, first, "ana", qr=direct).json()["data"]["message"]


def test_issuing_codes_is_rate_limited_and_old_ones_are_purged(client, company_headers, monkeypatch):
    headers = approved_employee(client, company_headers)
    employee_id = _employee_id(client, headers)
    qr_content(employee_id)
    old = _latest(employee_id)
    qr_content(employee_id)  # reemplaza al anterior
    _change(old, expires_at=datetime.now(UTC) - timedelta(days=settings.QR_TOKEN_RETENTION_DAYS + 1))
    with SessionLocal() as db:  # el mantenimiento (fuera de las peticiones) depura los vencidos hace tiempo
        assert purge_expired(db)["códigos QR"] == 1
        assert db.get(EmployeeQr, old) is None

    monkeypatch.setattr(settings, "RATE_LIMIT_QR_PER_MINUTE", 2)
    statuses = [client.post(MY_QR, headers=headers).status_code for _ in range(3)]
    assert statuses == [201, 201, 429]
