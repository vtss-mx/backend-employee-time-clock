"""API pública de verificación facial (SDK móviles, migración 0084; `docs/sdk/contrato-verificacion.md`).

La aplicación de una empresa verifica (1:1) o identifica (1:N) a SUS empleados con la llave de la API (permiso
`VERIFICATION`), con las mismas cerraduras de la aplicación web y la prueba OBLIGATORIA de la llave de su dispositivo.
Cada intento es un 200 con su decisión; lo que no llegó a ser un intento es su 4xx.
"""

import base64
import hashlib
import json

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from pydantic import ValidationError
from sqlalchemy import select

from app.core.config import settings
from app.core.database import SessionLocal
from app.models import Employee, FaceChallenge, FraudCase, VerificationLog
from app.schemas.capture import CaptureTelemetry
from app.schemas.verification import VerificationResult
from app.services.api_verification_service import _outcome
from app.services.client_evidence import parse_telemetry
from app.services.device_service import NonceState, api_nonce_state, issue_api_nonce, issue_nonce
from app.services.fraud_cases import _subject
from app.services.liveness_service import ApiDevice, challenge_store, device_of, user_of
from tests.conftest import approved_employee, turn_files
from tests.test_api_keys import new_key, other_company
from tests.test_policy import set_policy

API = "/api/integrations/v1/verification"
DEVICE = ec.generate_private_key(ec.SECP256R1())
OTHER_DEVICE = ec.generate_private_key(ec.SECP256R1())
#: La telemetría de un SDK nativo (versión 1 con `native`, contrato §7).
NATIVE = {
    "v": 1,
    "webdriver": False,
    "automation": 0,
    "virtual_camera": False,
    "track": {"width": 1280, "height": 720, "frame_rate": 30, "device_id": True},
    "frames": {"count": 120, "mean_ms": 33.4, "cv": 0.06, "clock": "presentation"},
    "screen": {"width": 412, "height": 915, "pixel_ratio": 2.625, "touch_points": 5},
    "native": {
        "client": "flutter-sdk",
        "platform": "android",
        "sdk_version": "1.0.0",
        "os_version": "14",
        "device_model": "Pixel 8",
        "front_camera_physical": True,
        "emulator": False,
    },
}


def spki(key: ec.EllipticCurvePrivateKey = DEVICE) -> str:
    der = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.b64encode(der).decode()


def device_hash(key: ec.EllipticCurvePrivateKey = DEVICE) -> str:
    return hashlib.sha256(base64.b64decode(spki(key))).hexdigest()


def sign(key: ec.EllipticCurvePrivateKey, nonce: str, action: str, first: bytes) -> str:
    """La firma r||s (base64) de `"{reto}.{acción}.{SHA-256 de la primera frontal}"`, como los SDK."""
    message = f"{nonce}.{action}.{hashlib.sha256(first).hexdigest()}".encode()
    r, s = decode_dss_signature(key.sign(message, ec.ECDSA(hashes.SHA256())))
    return base64.b64encode(r.to_bytes(32, "big") + s.to_bytes(32, "big")).decode()


@pytest.fixture
def secret(client, company_headers) -> str:
    """La llave de la aplicación móvil de la empresa: SOLO con el permiso de verificación."""
    return new_key(client, company_headers, scopes=["VERIFICATION"], name="App móvil")["secret"]


@pytest.fixture
def juan(client, company_headers) -> None:
    approved_employee(client, company_headers)


def challenge(client, secret: str, key: ec.EllipticCurvePrivateKey = DEVICE):
    return client.post(f"{API}/challenge", json={"device_key": spki(key)}, headers={"X-API-Key": secret})


def attempt(
    client,
    secret: str,
    *,
    action: str = "verify",
    person: str = "juan",
    frontal: bytes = b"face:juan",
    reference: dict | None = None,
    key: ec.EllipticCurvePrivateKey = DEVICE,
    issued: dict | None = None,
    proof: dict | None = None,
    camera: str | None = "Front Camera",
    wrong_turn: bool = False,
    telemetry: dict | None = None,
    location: dict | None = None,
):
    """Un intento completo: su reto (o el que se pasa), las frontales, los movimientos y la prueba del dispositivo."""
    issued = issued or challenge(client, secret, key).json()["data"]
    files = [("images", (f"f{i}.jpg", frontal, "image/jpeg")) for i in range(2)]
    files += turn_files(issued, person, wrong=wrong_turn)
    data: dict = {"challenge_id": issued["challenge_id"], "telemetry": json.dumps(telemetry or NATIVE)}
    if camera:
        data["camera_label"] = camera
    if action == "verify":
        data.update({"employee_number": "EMP-001"} if reference is None else reference)
    signature = sign(key, issued["device_nonce"], action, frontal)
    default = {"device_key": spki(key), "device_nonce": issued["device_nonce"], "device_signature": signature}
    data.update(default if proof is None else proof)
    if location:
        data.update(location)
    return client.post(f"{API}/{action}", data=data, files=files, headers={"X-API-Key": secret})


def logs() -> list[VerificationLog]:
    with SessionLocal() as db:
        return list(db.scalars(select(VerificationLog).order_by(VerificationLog.id)))


# ---------------------------------------------------------------- llave y permiso


def test_only_a_key_with_the_verification_scope_opens_these_routes(client, company_headers, secret):
    reader = new_key(client, company_headers, scopes=["EMPLOYEES_READ", "ATTENDANCE_READ"])["secret"]
    denied = challenge(client, reader)
    assert denied.status_code == 403 and denied.json()["code"] == "API_SCOPE_REQUIRED"
    assert denied.json()["errors"][0]["details"] == {"scope": "VERIFICATION"}
    # Y el permiso de verificación no lee nada más.
    employees = client.get("/api/integrations/v1/employees", headers={"X-API-Key": secret})
    assert employees.status_code == 403 and employees.json()["code"] == "API_SCOPE_REQUIRED"
    assert challenge(client, "tck_inventada").json()["code"] == "API_KEY_INVALID"
    anonymous = client.post(f"{API}/verify")
    assert anonymous.status_code == 401 and anonymous.json()["code"] == "API_KEY_REQUIRED"


def test_a_revoked_key_stops_at_once(client, company_headers):
    created = new_key(client, company_headers, scopes=["VERIFICATION"])
    assert client.delete(f"/api/api-keys/{created['id']}", headers=company_headers).status_code == 200
    assert challenge(client, created["secret"]).json()["code"] == "API_KEY_REVOKED"


def test_the_key_has_its_own_limit_on_these_routes(client, secret, monkeypatch):
    monkeypatch.setattr(settings, "RATE_LIMIT_API_VERIFICATION_PER_MINUTE", 1)
    assert challenge(client, secret).status_code == 200
    limited = challenge(client, secret, OTHER_DEVICE)
    assert limited.status_code == 429 and limited.json()["code"] == "RATE_LIMITED"
    assert limited.headers["Retry-After"]


# ---------------------------------------------------------------- reto


def test_the_challenge_brings_the_device_nonce_and_how_to_capture(client, secret):
    response = challenge(client, secret)
    assert response.status_code == 200 and response.json()["code"] == "CHALLENGE_ISSUED"
    data = response.json()["data"]
    assert data["liveness_required"] is True and 1 <= len(data["actions"]) <= 3
    assert data["flash"] == [] and data["flash_required"] is False and data["flash_pace"] is None
    assert data["capture"] == {
        "frontal_min": 1,
        "frontal_max": 3,
        "frontal_recommended": 2,
        "min_side_px": settings.MIN_IMAGE_DIMENSION,
        "max_side_px": settings.MAX_IMAGE_DIMENSION,
        "recommended_long_side_px": 1280,
        "max_image_bytes": settings.max_image_bytes,
        "formats": ["image/jpeg"],
        "min_response_seconds": 0.0,  # las pruebas no esperan el tiempo humano (FACE_CHALLENGE_MIN_SECONDS = 0)
    }
    assert data["burst"] is not None and response.json()["i18n"] is not None  # una escritura lleva cada idioma
    key_id = new_key_id(secret)
    assert api_nonce_state(key_id, device_hash(), data["device_nonce"]) == NonceState.VALID
    assert api_nonce_state(key_id, device_hash(OTHER_DEVICE), data["device_nonce"]) == NonceState.INVALID
    with SessionLocal() as db:
        stored = db.get(FaceChallenge, data["challenge_id"])
        assert stored is not None and stored.user_id is None and stored.api_key_id == key_id
        assert stored.device_hash == device_hash()


def new_key_id(secret: str) -> int:
    from app.core.crypto import hash_token
    from app.models import CompanyApiKey

    with SessionLocal() as db:
        return db.scalars(select(CompanyApiKey.id).where(CompanyApiKey.key_hash == hash_token(secret))).one()


def test_a_bad_device_key_or_an_extra_field_is_a_422(client, secret):
    bad = client.post(f"{API}/challenge", json={"device_key": "bm8tZXMtdW5hLWxsYXZl"}, headers={"X-API-Key": secret})
    assert bad.status_code == 422 and bad.json()["code"] == "DEVICE_KEY_INVALID"
    assert bad.json()["errors"][0]["field"] == "device_key"
    rsa_like = client.post(f"{API}/challenge", json={"device_key": "x" * 301}, headers={"X-API-Key": secret})
    assert rsa_like.status_code == 422
    extra = client.post(f"{API}/challenge", json={"device_key": spki(), "company_id": 2}, headers={"X-API-Key": secret})
    assert extra.status_code == 422


def test_without_liveness_the_challenge_still_brings_the_device_nonce(client, company_headers, secret, juan):
    set_policy(client, company_headers, liveness_challenge=False)
    response = challenge(client, secret)
    assert response.json()["code"] == "LIVENESS_NOT_REQUIRED"
    data = response.json()["data"]
    assert data["challenge_id"] is None and data["device_nonce"] and data["capture"]["min_response_seconds"] == 0
    files = [("images", ("f.jpg", b"face:juan", "image/jpeg"))]
    form = {
        "employee_number": "EMP-001",
        "device_key": spki(),
        "device_nonce": data["device_nonce"],
        "device_signature": sign(DEVICE, data["device_nonce"], "verify", b"face:juan"),
    }
    verified = client.post(f"{API}/verify", data=form, files=files, headers={"X-API-Key": secret})
    assert verified.json()["data"]["decision"] == "ALLOW"


def test_each_device_has_its_own_challenge(client, secret, juan):
    first = challenge(client, secret).json()["data"]
    other = challenge(client, secret, OTHER_DEVICE).json()["data"]
    # El reto de otro teléfono de la misma llave sigue vigente; el nuevo del mismo teléfono reemplaza al anterior.
    assert attempt(client, secret, key=OTHER_DEVICE, issued=other).json()["data"]["decision"] == "ALLOW"
    challenge(client, secret)
    replaced = attempt(client, secret, issued=first)
    assert replaced.status_code == 422 and replaced.json()["code"] == "CHALLENGE_INVALID"


def test_a_challenge_serves_one_attempt_of_its_own_device(client, secret, juan):
    issued = challenge(client, secret).json()["data"]
    assert attempt(client, secret, issued=issued).json()["data"]["matched"] is True
    reused = attempt(client, secret, issued=issued, frontal=b"face:juan")
    assert reused.status_code == 422 and reused.json()["code"] == "CHALLENGE_INVALID"
    # El reto de un teléfono no sirve en otro aunque firme bien con su propia llave (otro dueño).
    stolen = challenge(client, secret).json()["data"]
    other_nonce = challenge(client, secret, OTHER_DEVICE).json()["data"]["device_nonce"]
    proof = {
        "device_key": spki(OTHER_DEVICE),
        "device_nonce": other_nonce,
        "device_signature": sign(OTHER_DEVICE, other_nonce, "verify", b"face:juan"),
    }
    crossed = attempt(client, secret, issued=stolen, key=OTHER_DEVICE, proof=proof)
    assert crossed.status_code == 422 and crossed.json()["code"] == "CHALLENGE_INVALID"


# ---------------------------------------------------------------- verificar 1:1


def test_the_employee_is_verified_and_the_log_says_it_came_from_the_api(client, secret, juan):
    response = attempt(client, secret)
    assert response.status_code == 200 and response.json()["code"] == "IDENTITY_VERIFIED"
    data = response.json()["data"]
    assert data["matched"] is True and data["decision"] == "ALLOW" and data["reason"] is None
    assert data["employee"]["employee_number"] == "EMP-001" and data["employee"]["name"] == "Juan Pérez"
    assert "avatar" not in data["employee"] and data["method"] == "API_FACE" and data["confidence"] > 0
    assert data["challenge"] is None and data["verified_at"] and response.json()["i18n"] is not None
    last = logs()[-1]
    assert (last.id, last.method, last.success) == (data["attempt_id"], "API_FACE", True)
    assert last.user_id is None and last.device_hash == device_hash()
    by_id = attempt(client, secret, reference={"employee_id": str(data["employee"]["id"])}, frontal=b"face:juan")
    assert by_id.json()["data"]["decision"] == "ALLOW"


def test_the_verification_can_carry_its_location(client, secret, juan):
    """La ubicación viaja con el intento (la empresa decide si la exige con `verification_location`): con latitud y
    longitud presentes se arma y el intento sigue su curso."""
    located = attempt(client, secret, location={"latitude": "19.4326", "longitude": "-99.1332", "accuracy": "15"})
    assert located.status_code == 200 and located.json()["data"]["decision"] == "ALLOW"


def test_the_company_server_confirms_the_attempt_with_its_own_key(client, company_headers, secret, juan):
    attempt_id = attempt(client, secret).json()["data"]["attempt_id"]
    reader = new_key(client, company_headers, scopes=["ATTENDANCE_READ"])["secret"]
    # (El tramo de sincronización `/attendance/feed` entrega lo de hace más de 60 s; el listado, al instante.)
    listed = client.get("/api/integrations/v1/attendance", headers={"X-API-Key": reader}).json()["data"]
    found = [item for item in listed["items"] if item["id"] == attempt_id]
    assert found and found[0]["method"] == "API_FACE" and found[0]["success"] is True
    assert found[0]["validator_id"] is None


def test_the_employee_reference_is_exactly_one(client, secret, juan):
    for reference in ({}, {"employee_id": "1", "employee_number": "EMP-001"}):
        response = attempt(client, secret, reference=reference)
        assert response.status_code == 422 and response.json()["code"] == "EMPLOYEE_REFERENCE_INVALID"


def test_an_employee_of_another_company_does_not_exist_here(client, company_headers, admin_headers, secret, juan):
    unknown = attempt(client, secret, reference={"employee_number": "EMP-999"})
    assert unknown.status_code == 404 and unknown.json()["code"] == "EMPLOYEE_NOT_FOUND"
    other_headers = other_company(client, admin_headers)
    approved_employee(client, other_headers, number="PAN-001", email="pedro@panificadora.com")
    with SessionLocal() as db:
        pedro = db.scalars(select(Employee.id).where(Employee.employee_number == "PAN-001")).one()
    foreign = attempt(client, secret, reference={"employee_id": str(pedro)})
    assert foreign.status_code == 404 and foreign.json()["code"] == "EMPLOYEE_NOT_FOUND"
    # Y la llave de la otra empresa no ve a Juan.
    theirs = new_key(client, other_headers, scopes=["VERIFICATION"])["secret"]
    crossed = attempt(client, theirs, reference={"employee_number": "EMP-001"})
    assert crossed.status_code == 404


def test_an_inactive_or_unapproved_employee_is_a_409(client, company_headers, secret, juan):
    from tests.conftest import create_employee

    assert create_employee(client, company_headers, number="EMP-002", email="ana@empresa.com").status_code == 201
    pending = attempt(client, secret, reference={"employee_number": "EMP-002"})
    assert pending.status_code == 409 and pending.json()["code"] == "FACE_NOT_APPROVED"
    with SessionLocal() as db:
        employee = db.scalars(select(Employee).where(Employee.employee_number == "EMP-001")).one()
        employee.active = False
        db.commit()
    inactive = attempt(client, secret)
    assert inactive.status_code == 409 and inactive.json()["code"] == "EMPLOYEE_INACTIVE"


def test_another_face_is_denied_with_its_reason(client, secret, juan):
    response = attempt(client, secret, frontal=b"face:pedro", person="pedro")
    assert response.status_code == 200 and response.json()["code"] == "IDENTITY_NOT_VERIFIED"
    data = response.json()["data"]
    assert data["matched"] is False and data["decision"] == "DENY" and data["employee"] is None
    assert data["reason"] in ("NO_MATCH", "LIVENESS_MISMATCH") and data["attempt_id"] == logs()[-1].id


def test_a_missing_movement_is_a_liveness_failure(client, secret, juan):
    data = attempt(client, secret, wrong_turn=True).json()["data"]
    assert data["decision"] == "DENY" and data["reason"] == "LIVENESS_FAILED"


def test_a_virtual_camera_is_denied_and_recorded_as_suspicious(client, secret, juan):
    response = attempt(client, secret, camera="OBS Virtual Camera")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["decision"] == "DENY" and data["reason"] == "VIRTUAL_CAMERA" and data["matched"] is False
    assert logs()[-1].reason == "VIRTUAL_CAMERA" and logs()[-1].device_hash == device_hash()


def test_the_risk_engine_asks_for_one_more_step_inside_the_result(client, company_headers, secret, juan):
    set_policy(client, company_headers, risk_signals={"CAMERA_LABEL_MISSING": {"mode": "ENFORCE", "points": 30}})
    asked = attempt(client, secret, camera=None)
    assert asked.status_code == 200 and asked.json()["code"] == "VERIFICATION_STEP_UP"
    data = asked.json()["data"]
    assert data["decision"] == "STEP_UP" and data["reason"] == "STEP_UP_REQUIRED" and data["matched"] is False
    step_up = data["challenge"]
    assert step_up["step_up"] is True and len(step_up["actions"]) == 3 and step_up["device_nonce"]
    assert step_up["capture"]["frontal_recommended"] == 2
    done = attempt(client, secret, camera=None, issued=step_up, frontal=b"face:juan")
    assert done.json()["data"]["decision"] == "ALLOW"


def test_medium_risk_without_liveness_is_left_in_review(client, company_headers, secret, juan):
    set_policy(client, company_headers, liveness_challenge=False)
    set_policy(client, company_headers, risk_signals={"CAMERA_LABEL_MISSING": {"mode": "ENFORCE", "points": 30}})
    nonce = challenge(client, secret).json()["data"]["device_nonce"]
    form = {
        "employee_number": "EMP-001",
        "device_key": spki(),
        "device_nonce": nonce,
        "device_signature": sign(DEVICE, nonce, "verify", b"face:juan"),
    }
    files = [("images", ("f.jpg", b"face:juan", "image/jpeg"))]
    data = client.post(f"{API}/verify", data=form, files=files, headers={"X-API-Key": secret}).json()["data"]
    assert data["matched"] is True and data["decision"] == "REVIEW"


# ---------------------------------------------------------------- prueba del dispositivo


def test_the_device_proof_is_required_and_checked_before_the_challenge(client, secret, juan):
    issued = challenge(client, secret).json()["data"]
    empty = {"device_key": "", "device_nonce": "", "device_signature": ""}
    missing = attempt(client, secret, issued=issued, proof=empty)
    assert missing.status_code == 403 and missing.json()["code"] == "DEVICE_PROOF_REQUIRED"
    forged = {
        "device_key": spki(),
        "device_nonce": issued["device_nonce"],
        "device_signature": sign(DEVICE, issued["device_nonce"], "identify", b"face:juan"),  # otra acción
    }
    invalid = attempt(client, secret, issued=issued, proof=forged)
    assert invalid.status_code == 403 and invalid.json()["code"] == "DEVICE_PROOF_INVALID"
    other_key = {**forged, "device_key": spki(OTHER_DEVICE)}  # el reto se emitió para otra llave del dispositivo
    assert attempt(client, secret, issued=issued, proof=other_key).json()["code"] == "DEVICE_PROOF_INVALID"
    not_a_key = {**forged, "device_key": "bm8tZXMtdW5hLWxsYXZl"}
    assert attempt(client, secret, issued=issued, proof=not_a_key).json()["code"] == "DEVICE_PROOF_INVALID"
    huge = {**forged, "device_signature": "A" * 201}
    assert attempt(client, secret, issued=issued, proof=huge).json()["code"] == "DEVICE_PROOF_INVALID"
    # Nada de eso consumió el reto ni contó como intento.
    assert not [log for log in logs() if log.method == "API_FACE"]
    assert attempt(client, secret, issued=issued).json()["data"]["decision"] == "ALLOW"


def test_a_nonce_of_an_account_never_serves_a_device(client, secret, juan):
    key_id = new_key_id(secret)
    issued = challenge(client, secret).json()["data"]
    account_nonce = issue_nonce(key_id)  # el de la cuenta con el mismo número: otro espacio de nombres
    proof = {
        "device_key": spki(),
        "device_nonce": account_nonce,
        "device_signature": sign(DEVICE, account_nonce, "verify", b"face:juan"),
    }
    assert attempt(client, secret, issued=issued, proof=proof).json()["code"] == "DEVICE_PROOF_INVALID"
    assert issue_api_nonce(key_id, device_hash()).count(".") == 2


def test_each_device_has_its_own_limit(client, secret, juan, monkeypatch):
    monkeypatch.setattr(settings, "RATE_LIMIT_API_VERIFICATION_DEVICE_PER_MINUTE", 2)
    issued = challenge(client, secret).json()["data"]  # 1
    assert attempt(client, secret, issued=issued).json()["data"]["decision"] == "ALLOW"  # 2
    limited = challenge(client, secret)
    assert limited.status_code == 429 and limited.json()["code"] == "RATE_LIMITED"
    assert challenge(client, secret, OTHER_DEVICE).status_code == 200  # otro teléfono, su propio límite


# ---------------------------------------------------------------- identificar 1:N


def test_the_person_is_identified_among_the_employees(client, secret, juan):
    response = attempt(client, secret, action="identify")
    assert response.status_code == 200 and response.json()["code"] == "EMPLOYEE_IDENTIFIED"
    data = response.json()["data"]
    assert data["decision"] == "ALLOW" and data["employee"]["employee_number"] == "EMP-001"
    assert logs()[-1].method == "API_FACE" and logs()[-1].device_hash == device_hash()
    nobody = attempt(client, secret, action="identify", frontal=b"face:pedro", person="pedro")
    assert nobody.json()["code"] == "EMPLOYEE_NOT_IDENTIFIED"
    assert nobody.json()["data"]["employee"] is None and nobody.json()["data"]["reason"] == "NO_MATCH"


def test_an_identification_with_a_failed_movement_is_denied(client, secret, juan):
    data = attempt(client, secret, action="identify", wrong_turn=True).json()["data"]
    assert data["decision"] == "DENY" and data["reason"] == "LIVENESS_FAILED" and data["employee"] is None


def test_suspicious_identifications_lock_only_that_device(client, company_headers, secret, juan):
    set_policy(client, company_headers, lockout_max_failures=3)
    for _ in range(3):
        denied = attempt(client, secret, action="identify", camera="OBS Virtual Camera")
        assert denied.json()["data"]["reason"] == "VIRTUAL_CAMERA"
    locked = attempt(client, secret, action="identify")
    assert locked.status_code == 429 and locked.json()["code"] == "FACE_LOCKED"
    assert int(locked.headers["Retry-After"]) > 0
    assert attempt(client, secret, action="identify", key=OTHER_DEVICE).json()["data"]["decision"] == "ALLOW"
    with SessionLocal() as db:
        case = db.scalars(select(FraudCase)).first()
    assert case is None or case.subject.startswith(("device:", "employee:"))


def test_identify_in_one_more_step(client, company_headers, secret, juan):
    set_policy(client, company_headers, risk_signals={"CAMERA_LABEL_MISSING": {"mode": "ENFORCE", "points": 30}})
    asked = attempt(client, secret, action="identify", camera=None).json()
    assert asked["code"] == "VERIFICATION_STEP_UP" and asked["data"]["challenge"]["step_up"] is True


# ---------------------------------------------------------------- telemetría nativa y piezas


def test_the_native_telemetry_is_strict_and_the_browser_one_is_unchanged():
    assert CaptureTelemetry.model_validate(NATIVE).native is not None
    browser = {key: value for key, value in NATIVE.items() if key != "native"}
    assert CaptureTelemetry.model_validate(browser).native is None
    for wrong in (
        {"client": "web"},
        {"platform": "windows"},
        {"sdk_version": "uno"},
        {"device_model": "Teléfono de Juan"},
        {"extra": True},
    ):
        with pytest.raises(ValidationError):
            CaptureTelemetry.model_validate({**NATIVE, "native": {**NATIVE["native"], **wrong}})
    unmeasured = {**NATIVE["native"], "front_camera_physical": None, "emulator": None}
    assert CaptureTelemetry.model_validate({**NATIVE, "native": unmeasured}).native is not None
    assert parse_telemetry(json.dumps({**NATIVE, "native": {"client": "x"}})).invalid is True


def test_the_owner_helpers_and_the_case_subject():
    device = ApiDevice(7, "f" * 64)
    assert (user_of(device), device_of(device), user_of(5), device_of(5)) == (None, "f" * 64, 5, None)
    assert _subject(VerificationLog(employee_id=None, user_id=None, device_hash="a" * 64)) == "device:" + "a" * 32
    assert _subject(VerificationLog(employee_id=None, user_id=9, device_hash=None)) == "actor:9"
    assert _subject(VerificationLog(employee_id=3, user_id=9, device_hash=None)) == "employee:3"


def test_a_challenge_of_a_device_is_not_one_of_an_account(client, secret):
    key_id = new_key_id(secret)
    with SessionLocal() as db:
        issued = challenge_store.issue(db, ApiDevice(key_id, device_hash()), steps=1, lifetime_seconds=60)
        assert challenge_store.consume(db, issued.id, key_id) is None  # una cuenta con el mismo número
        again = challenge_store.issue(db, ApiDevice(key_id, device_hash()), steps=1, lifetime_seconds=60)
        taken = challenge_store.consume(db, again.id, ApiDevice(key_id, device_hash()))
        assert taken is not None and taken.api_device == ApiDevice(key_id, device_hash()) and taken.user_id is None


def test_an_outcome_without_its_log_still_answers():
    review = VerificationResult(verified=True, method="API_FACE", message="ok", employee_id=1, name="Ana", review=True)
    result = _outcome(review, None)
    assert result.decision == "REVIEW" and result.attempt_id is None and result.employee is not None
    denied = _outcome(VerificationResult(verified=False, method="API_FACE", message="no"), None)
    assert denied.decision == "DENY" and denied.reason is None
