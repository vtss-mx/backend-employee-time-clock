"""Catálogos en la base de datos (esquema catalog): la API los sirve y la lógica los obedece."""

import re
from pathlib import Path

from sqlalchemy import delete, update

from app.core.database import SessionLocal
from app.facial_recognition import TurnDirection
from app.facial_recognition.pipeline import Accessory
from app.models import (
    CatalogCountry,
    CatalogFaceError,
    CatalogSessionRevocationReason,
    CatalogVerificationReason,
    EnrollmentStatus,
    FaceStatus,
    SessionRevocationReason,
    UserRole,
    ValidatorMode,
    ValidatorModeMethod,
    VerificationMethod,
)
from app.services.catalog_service import clear_catalog_cache, get_catalogs
from app.services.face_service import SPOOF_FLAG
from tests.conftest import COMPANY_EMAIL, COMPANY_PASSWORD, create_employee, login, submit_enrollment
from tests.test_validators import validator_headers

APP_DIR = Path(__file__).resolve().parents[1] / "app"

#: Todos los motivos que la lógica registra en la bitácora (QR y rostro).
REASONS = {
    "INVALID_FORMAT", "NOT_FOUND", "OTHER_COMPANY", "REVOKED", "EXPIRED", "OTHER_EMPLOYEE", "EMPLOYEE_INACTIVE",
    "NO_MATCH", "LIVENESS_FAILED", "LIVENESS_MISMATCH", "FACE_NOT_REGISTERED", "EMPTY_GALLERY", "AMBIGUOUS_MATCH",
    "INCONSISTENT_MATCH",
}  # fmt: skip


def _codes(catalog: str) -> set[str]:
    return {row["code"] for row in get_catalogs().entries[catalog]}


def test_catalogs_require_a_session(client, company_headers):
    assert client.get("/api/catalogs").status_code == 401
    response = client.get("/api/catalogs", headers=company_headers)
    assert response.status_code == 200 and response.json()["code"] == "CATALOGS"
    data = response.json()["data"]
    assert [m["code"] for m in data["validator_modes"]] == ["QR_OR_FACE", "QR", "FACE", "QR_AND_FACE"]
    assert {m["code"]: m["methods"] for m in data["validator_modes"]}["QR_OR_FACE"] == ["FACE", "QR"]
    assert data["face_statuses"][0] == {
        **data["face_statuses"][0],
        "code": "NOT_ENROLLED",
        "name": "Sin registrar",
        "tone": "muted",
    }
    countries = data["countries"]
    assert len(countries) == 245
    assert countries[0] == {**countries[0], "code": "MX", "dial_code": "+52", "featured": True}
    levels = data["confidence_levels"]
    assert len(levels) == 21 and levels[-1] == {**levels[-1], "code": "100", "name": "Máximo", "value": 0.99999}
    assert len(data["enrollment_rejection_reasons"]) == 4 and len(data["reverification_reasons"]) == 4
    assert "liveness_actions" not in data  # interno: la instrucción viaja con cada reto
    assert data["accessories"][0] == {**data["accessories"][0], "code": "GLASSES", "phrase": "los lentes"}
    errors = {e["code"]: e for e in data["face_errors"]}
    assert errors["NO_FACE"]["retryable"] is True and errors["FACE_NOT_REGISTERED"]["retryable"] is False
    assert [f["code"] for f in data["enrollment_flags"]] == ["GLASSES", "HEADWEAR", "MASK", "SPOOF"]


def test_code_and_catalogs_name_the_same_values():
    """Los Enum del código y las tablas del catálogo deben tener exactamente los mismos códigos."""
    assert _codes("roles") == {r.value for r in UserRole}
    assert _codes("verification_methods") == {m.value for m in VerificationMethod}
    assert _codes("validator_modes") == {m.value for m in ValidatorMode}
    assert _codes("face_statuses") == {s.value for s in FaceStatus}
    assert _codes("enrollment_statuses") == {s.value for s in EnrollmentStatus}
    assert _codes("accessories") == {a.value for a in Accessory}
    assert _codes("liveness_actions") == {d.value for d in TurnDirection}
    assert _codes("verification_reasons") == REASONS
    assert _codes("session_revocation_reasons") == {r.value for r in SessionRevocationReason}
    assert _codes("enrollment_flags") == {a.value for a in Accessory} | {SPOOF_FLAG}


def test_every_face_error_the_api_raises_has_its_message_in_the_catalog():
    """El motor facial y los servicios solo usan códigos: su mensaje debe existir en face_errors."""
    used = re.compile(r'(?:FaceValidationError|face_rejection|face_error_message)\(\s*"([A-Z_]+)"')
    raised = {code for path in APP_DIR.rglob("*.py") for code in used.findall(path.read_text(encoding="utf-8"))}
    # El giro no detectado es interno: la API lo informa como LIVENESS_FAILED.
    assert raised - {"LIVENESS_TURN_NOT_DETECTED"} <= _codes("face_errors")
    assert {"NO_FACE", "TOO_DARK", "IMAGE_TOO_LARGE", "SPOOF_DETECTED", "CHALLENGE_INVALID"} <= raised


def test_messages_and_rules_come_from_the_database(client, company_headers):
    headers = validator_headers(client, company_headers, mode="QR_OR_FACE")
    garbage = {"qr_content": "no-es-un-qr"}
    assert client.post("/api/checkpoint/identify/qr", json=garbage, headers=headers).json()["message"] == "QR inválido"

    # Cambiar el texto en la base cambia lo que ve la persona (sin tocar el código).
    with SessionLocal() as db:
        db.execute(
            update(CatalogVerificationReason)
            .where(CatalogVerificationReason.code == "INVALID_FORMAT")
            .values(message="Código no válido para esta empresa")
        )
        # Y las reglas: el modo "QR o rostro" ya no permite QR.
        db.execute(
            delete(ValidatorModeMethod).where(
                ValidatorModeMethod.mode_code == "QR_OR_FACE", ValidatorModeMethod.method_code == "QR"
            )
        )
        db.commit()
    clear_catalog_cache()
    denied = client.post("/api/checkpoint/identify/qr", json=garbage, headers=headers)
    assert denied.status_code == 409 and denied.json()["code"] == "VALIDATOR_METHOD_NOT_ALLOWED"
    assert "«QR o rostro»" in denied.json()["message"]
    assert get_catalogs().reason_message("INVALID_FORMAT") == "Código no válido para esta empresa"


def _set(model, code: str, **values) -> None:
    with SessionLocal() as db:
        db.execute(update(model).where(model.code == code).values(**values))
        db.commit()
    clear_catalog_cache()


def test_face_and_session_messages_come_from_the_database(client, company_headers):
    create_employee(client, company_headers)
    headers = login(client, "juan@empresa.com", "Empleado123")
    _set(CatalogFaceError, "NO_FACE", message="No te vemos: acércate a la cámara")
    _set(CatalogFaceError, "IMAGE_TOO_SMALL", message="Mínimo {min_dimension}px")
    response = submit_enrollment(client, headers, frontal=(b"face:juan", b"noface"))
    assert response.json() == {
        **response.json(),
        "code": "NO_FACE",
        "message": "Foto 2: No te vemos: acércate a la cámara",
    }
    # Los datos del error llenan los marcadores del mensaje.
    assert get_catalogs().face_error_message("IMAGE_TOO_SMALL", {"min_dimension": 200}) == "Mínimo 200px"

    _set(CatalogSessionRevocationReason, "SIGNED_IN_ELSEWHERE", message="Entraste desde otro equipo")
    first = login(client, COMPANY_EMAIL, COMPANY_PASSWORD)
    login(client, COMPANY_EMAIL, COMPANY_PASSWORD)
    kicked = client.get("/api/users/me", headers=first).json()
    assert kicked == {**kicked, "code": "SESSION_REPLACED", "message": "Entraste desde otro equipo"}


def test_confidence_and_phone_country_must_be_active_in_the_catalog(client, company_headers):
    url = "/api/settings/verification"
    accepted = client.put(url, json={"min_confidence": 0.95}, headers=company_headers)
    assert accepted.status_code == 200 and accepted.json()["data"]["min_confidence"] == 0.95
    not_a_level = client.put(url, json={"min_confidence": 0.951}, headers=company_headers)
    assert not_a_level.status_code == 422 and not_a_level.json()["code"] == "INVALID_CONFIDENCE_LEVEL"

    _set(CatalogCountry, "US", active=False)
    us_phone = create_employee(client, company_headers, phone="+12025550123")
    assert us_phone.status_code == 422 and us_phone.json()["errors"][0]["field"] == "phone"
    _set(CatalogCountry, "US", active=True)
    assert create_employee(client, company_headers, phone="+12025550123").status_code == 201
