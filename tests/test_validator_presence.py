"""Antifraude 2b: la firma por petición del dispositivo del validador y su ubicación en cada identificación.

Por omisión todo se mide («Solo medir»: señales del motor de riesgo, nunca un rechazo); obligatorio, lo que no cumple se
rechaza con su código ANTES de consumir el reto o el QR (se puede reintentar con lo mismo); apagado, no se revisa."""

import hashlib
import json
import logging
import time

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select, update

from app.core.database import SessionLocal
from app.models import AuthSession, DeviceStatus, Validator, ValidatorDevice
from app.services import device_service
from app.services.policy_service import PolicyService
from app.services.request_signing import RequestProof, RequestSigning, SignatureVerdict, digest_of, signed_message
from tests.conftest import TEST_DEVICE_KEY, device_proof, qr_content, turn_files
from tests.test_policy import set_policy
from tests.test_risk_engine import assessments, last_reasons
from tests.test_validators import PASSWORD, approved, create_validator, validator_headers

FACE = b"face:juan"
OTHER_KEY = ec.generate_private_key(ec.SECP256R1())
#: El punto del domicilio de los validadores de prueba (`tests.test_validators.ADDRESS`), ~55 m y ~1.1 km al norte.
NEAR = {"latitude": 29.0734, "longitude": -110.9559, "accuracy": 10}
FAR = {"latitude": 29.0829, "longitude": -110.9559, "accuracy": 10}


def signature(nonce: str, action: str, content: bytes, key: ec.EllipticCurvePrivateKey = TEST_DEVICE_KEY) -> dict:
    """La firma por petición de una identificación, como la manda la app."""
    proof = device_proof(signed_message(nonce, action, digest_of(content)), key)
    return {"signature_key": proof["public_key"], "signature_nonce": nonce, "signature": proof["signature"]}


def profile(client, headers) -> dict:
    response = client.get("/api/checkpoint/me", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def identify(client, headers, *, signed: dict | None = None, location: dict | None = None, samples=None, **extra):
    challenge = client.post("/api/face/challenge", headers=headers).json()["data"]
    return send(client, headers, challenge, signed=signed, location=location, samples=samples, **extra)


def send(client, headers, challenge, *, signed=None, location=None, samples=None, qr: str | None = None):
    files = [("images", (f"c{i}.jpg", FACE, "image/jpeg")) for i in range(3)]
    files += turn_files(challenge, "juan")
    data = {"challenge_id": challenge["challenge_id"], **(signed or {}), **(location or {})}
    if samples is not None:
        data["location_samples"] = samples if isinstance(samples, str) else json.dumps(samples)
    if qr:
        data["qr_content"] = qr
    return client.post("/api/checkpoint/identify/face", data=data, files=files, headers=headers)


def identified(response) -> dict:
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["verified"] is True, data
    return data


def error(response, status: int, code: str) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == code, body
    return body["errors"][0]


@pytest.fixture
def setup(client, company_headers):
    approved(client, company_headers, "juan", number="EMP-001")
    return validator_headers(client, company_headers, mode="FACE")


def bound_key(user_email: str = "recepcion@empresa.com") -> str | None:
    with SessionLocal() as db:
        rows = db.scalars(select(AuthSession).order_by(AuthSession.created_at.desc())).all()
        return next(row.device_key_hash for row in rows if row.user.email == user_email)


# ---------------------------------------------------------------- firma por petición (Solo medir)


def test_the_session_is_bound_at_login_and_a_signed_identification_measures_nothing(client, setup):
    assert bound_key() == device_service.key_hash(device_proof("x")["public_key"])
    nonce = profile(client, setup)["device_nonce"]
    assert nonce and int(nonce.split(".")[0]) > time.time()  # el cliente sabe cuándo vence
    data = identified(identify(client, setup, signed=signature(nonce, "face", FACE)))
    assert data["device_nonce"] and data["device_nonce"] != nonce  # el reto de la siguiente, sin otra petición
    assert not {code for code in last_reasons() if code.startswith("VALIDATOR_")}
    # El mismo reto sirve para otra identificación mientras no vence (la firma la ata a su contenido).
    identified(identify(client, setup, signed=signature(nonce, "face", FACE)))


@pytest.mark.parametrize(
    ("make", "code", "value"),
    [
        (lambda nonce: {}, "VALIDATOR_UNSIGNED", 0.0),
        (lambda nonce: {"signature_nonce": nonce}, "VALIDATOR_SIGNATURE_INVALID", None),
        (lambda nonce: signature(nonce, "qr", FACE), "VALIDATOR_SIGNATURE_INVALID", None),
        (lambda nonce: signature(nonce + "x", "face", FACE), "VALIDATOR_SIGNATURE_INVALID", None),
        (lambda nonce: signature(nonce, "face", b"face:otra"), "VALIDATOR_SIGNATURE_INVALID", None),
        (lambda nonce: {**signature(nonce, "face", FACE), "signature": "A" * 300}, "VALIDATOR_SIGNATURE_INVALID", None),
        (lambda nonce: signature(nonce, "face", FACE, OTHER_KEY), "VALIDATOR_KEY_MISMATCH", None),
    ],
)
def test_what_is_not_signed_by_the_session_device_is_only_a_signal(client, setup, make, code, value):
    nonce = profile(client, setup)["device_nonce"]
    data = identified(identify(client, setup, signed=make(nonce)))  # nunca un rechazo en «Solo medir»
    reason = last_reasons()[code]
    assert (reason["mode"], reason["value"]) == ("OBSERVE", value)
    assert reason["kind"] == {"VALIDATOR_KEY_MISMATCH": "INTERNAL"}.get(code, "INJECTION")
    assert data["review"] is False


def expired_nonce(client, headers, monkeypatch) -> str:
    """Un reto auténtico que ya venció (el servidor lo emitió con vigencia negativa)."""
    with monkeypatch.context() as patch:
        patch.setattr(device_service, "DEVICE_NONCE_SECONDS", -10)
        return profile(client, headers)["device_nonce"]


def test_an_expired_challenge_is_stale_not_forged(client, setup, monkeypatch):
    nonce = expired_nonce(client, setup, monkeypatch)
    identified(identify(client, setup, signed=signature(nonce, "face", FACE)))
    assert last_reasons()["VALIDATOR_UNSIGNED"]["value"] == 1.0


# ---------------------------------------------------------------- firma obligatoria


@pytest.mark.parametrize(
    ("make", "code"),
    [
        (lambda nonce: {}, "SIGNATURE_REQUIRED"),
        (lambda nonce: {"signature_key": "x"}, "SIGNATURE_INVALID"),
        (lambda nonce: signature(nonce, "face", FACE, OTHER_KEY), "SIGNATURE_KEY_MISMATCH"),
    ],
)
def test_with_required_signing_the_challenge_survives_a_rejection(client, company_headers, setup, make, code):
    set_policy(client, company_headers, validator_signing="ENFORCE")
    nonce = profile(client, setup)["device_nonce"]
    challenge = client.post("/api/face/challenge", headers=setup).json()["data"]
    rejected = error(send(client, setup, challenge, signed=make(nonce)), 403, code)
    fresh = rejected["details"]["device_nonce"]
    assert fresh and fresh != nonce
    assert assessments() == []  # rechazado antes del intento: nada se midió ni se registró
    # El mismo reto sigue sin usarse: la app reintenta con la firma correcta.
    identified(send(client, setup, challenge, signed=signature(fresh, "face", FACE)))


def test_with_required_signing_a_stale_challenge_is_retried_with_the_new_one(
    client, company_headers, setup, monkeypatch
):
    set_policy(client, company_headers, validator_signing="ENFORCE")
    nonce = expired_nonce(client, setup, monkeypatch)
    stale = error(identify(client, setup, signed=signature(nonce, "face", FACE)), 403, "SIGNATURE_STALE")
    identified(identify(client, setup, signed=signature(stale["details"]["device_nonce"], "face", FACE)))


def test_without_signing_nothing_is_asked_or_measured(client, company_headers, setup):
    set_policy(client, company_headers, validator_signing="OFF")
    assert profile(client, setup)["device_nonce"] is None
    data = identified(identify(client, setup))
    assert data["device_nonce"] is None
    assert not {code for code in last_reasons() if code.startswith("VALIDATOR_")}


# ---------------------------------------------------------------- QR y QR + rostro


def test_qr_identifications_are_signed_too(client, company_headers, caplog):
    employee = approved(client, company_headers, "juan", number="EMP-001")
    headers = validator_headers(client, company_headers, mode="QR")
    nonce = profile(client, headers)["device_nonce"]
    qr = qr_content(employee["id"])
    signed = signature(nonce, "qr", qr.encode())
    response = client.post("/api/checkpoint/identify/qr", json={"qr_content": qr, **signed}, headers=headers)
    assert response.status_code == 200 and response.json()["data"]["verified"] is True
    assert response.json()["data"]["device_nonce"]
    # Sin firma: sin rostro no hay motor de riesgo; con «Solo medir» queda en el log del proceso.
    with caplog.at_level(logging.INFO, logger="app.services.checkpoint_service"):
        unsigned = client.post(
            "/api/checkpoint/identify/qr", json={"qr_content": qr_content(employee["id"])}, headers=headers
        )
    assert unsigned.json()["data"]["verified"] is True
    assert "VALIDATOR_UNSIGNED" in caplog.text
    # Un QR que no sirve también trae el reto de la siguiente firma.
    failed = client.post("/api/checkpoint/identify/qr", json={"qr_content": "x" * 20, **signed}, headers=headers)
    assert failed.json()["data"]["verified"] is False and failed.json()["data"]["device_nonce"]
    set_policy(client, company_headers, validator_signing="ENFORCE")
    error(
        client.post("/api/checkpoint/identify/qr", json={"qr_content": "x" * 20}, headers=headers),
        403,
        "SIGNATURE_REQUIRED",
    )


def test_qr_and_face_signs_both_steps(client, company_headers):
    employee = approved(client, company_headers, "juan", number="EMP-001")
    headers = validator_headers(client, company_headers, mode="QR_AND_FACE")
    set_policy(client, company_headers, validator_signing="ENFORCE")
    nonce = profile(client, headers)["device_nonce"]
    qr = qr_content(employee["id"])
    unsigned = client.post("/api/checkpoint/qr/inspect", json={"qr_content": qr}, headers=headers)
    error(unsigned, 403, "SIGNATURE_REQUIRED")
    inspected = client.post(
        "/api/checkpoint/qr/inspect",
        json={"qr_content": qr, **signature(nonce, "inspect", qr.encode())},
        headers=headers,
    )
    assert inspected.status_code == 200, inspected.text
    nonce = inspected.json()["data"]["device_nonce"]
    challenge = client.post("/api/face/challenge", headers=headers).json()["data"]
    identified(send(client, headers, challenge, qr=qr, signed=signature(nonce, "face", FACE)))


# ---------------------------------------------------------------- la sesión y su llave


def test_a_session_without_a_key_is_bound_to_the_first_approved_device_that_signs(client, setup):
    with SessionLocal() as db:  # una sesión abierta antes de la migración 0070
        db.execute(update(AuthSession).values(device_key_hash=None))
        db.commit()
    nonce = profile(client, setup)["device_nonce"]
    identified(
        identify(client, setup, signed=signature(nonce, "face", FACE, OTHER_KEY))
    )  # no es un dispositivo aprobado
    assert last_reasons()["VALIDATOR_KEY_MISMATCH"] and bound_key() is None
    identified(identify(client, setup, signed=signature(nonce, "face", FACE)))  # el aprobado la liga
    assert "VALIDATOR_KEY_MISMATCH" not in last_reasons()
    assert bound_key() == device_service.key_hash(device_proof("x")["public_key"])
    with SessionLocal() as db:  # un dispositivo aprobado deja de serlo: ya no liga una sesión nueva sin llave
        db.execute(update(AuthSession).values(device_key_hash=None))
        db.execute(update(ValidatorDevice).values(status=DeviceStatus.REVOKED))
        db.commit()
    identified(identify(client, setup, signed=signature(nonce, "face", FACE)))
    assert last_reasons()["VALIDATOR_KEY_MISMATCH"]


def test_without_device_approval_the_first_key_that_signs_binds_the_session(client, company_headers):
    set_policy(client, company_headers, validator_device_approval=False)
    approved(client, company_headers, "juan", number="EMP-001")
    headers = validator_headers(client, company_headers, mode="FACE")  # sin prueba al iniciar sesión
    assert bound_key() is None
    nonce = profile(client, headers)["device_nonce"]
    identified(identify(client, headers, signed=signature(nonce, "face", FACE, OTHER_KEY)))
    assert bound_key() == device_service.key_hash(device_proof("x", OTHER_KEY)["public_key"])
    identified(identify(client, headers, signed=signature(nonce, "face", FACE)))  # otra llave: ya no es la de la sesión
    assert last_reasons()["VALIDATOR_KEY_MISMATCH"]


def test_without_device_approval_required_signing_asks_for_the_key_at_login(client, company_headers):
    set_policy(client, company_headers, validator_device_approval=False, validator_signing="ENFORCE")
    assert create_validator(client, company_headers, mode="FACE").status_code == 201
    body = {"email": "recepcion@empresa.com", "password": PASSWORD}
    asked = client.post("/api/auth/login", json=body)
    nonce = error(asked, 403, "DEVICE_PROOF_REQUIRED")["details"]["nonce"]
    response = client.post("/api/auth/login", json={**body, "device": device_proof(nonce)})
    assert response.status_code == 200, response.text
    assert bound_key() == device_service.key_hash(device_proof("x")["public_key"])
    with SessionLocal() as db:  # posesión de la llave, sin registrar el dispositivo (la empresa no los aprueba)
        assert db.scalars(select(ValidatorDevice)).all() == []


def test_a_request_without_its_session_is_never_signed(client, setup):
    with SessionLocal() as db:
        checkpoint = db.scalars(select(Validator)).one()
        policy = PolicyService(db, checkpoint.company_id).current()
        proof = signature(device_service.issue_nonce(checkpoint.user_id), "face", FACE)
        request = RequestProof(proof["signature_key"], proof["signature_nonce"], proof["signature"])
        for session_id in (None, "no-existe"):
            signing = RequestSigning(db, checkpoint, (session_id, None), policy)
            assert signing.verdict(request, "face", digest_of(FACE)) == SignatureVerdict.MISMATCH


# ---------------------------------------------------------------- ubicación en cada identificación


@pytest.fixture
def located(client, company_headers):
    """Un validador con "requiere ubicación" (radio 100 m), con su sesión iniciada en su lugar."""
    approved(client, company_headers, "juan", number="EMP-001")
    created = create_validator(client, company_headers, mode="FACE", location_required=True, location_radius_m=100)
    assert created.status_code == 201, created.text
    body = {"email": "recepcion@empresa.com", "password": PASSWORD, "location": NEAR}
    asked = client.post("/api/auth/login", json=body)
    nonce = error(asked, 403, "DEVICE_PROOF_REQUIRED")["details"]["nonce"]
    response = client.post("/api/auth/login", json={**body, "device": device_proof(nonce)})
    if response.status_code == 403:  # el dispositivo de pruebas se da por autorizado
        with SessionLocal() as db:
            db.execute(update(ValidatorDevice).values(status=DeviceStatus.APPROVED))
            db.commit()
        response = client.post("/api/auth/login", json={**body, "device": device_proof(nonce)})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


def signed_for(client, headers) -> dict:
    return signature(profile(client, headers)["device_nonce"], "face", FACE)


@pytest.mark.parametrize(
    ("location", "code", "value", "threshold"),
    [
        (None, "VALIDATOR_LOCATION_MISSING", None, None),
        ({**NEAR, "accuracy": 900}, "VALIDATOR_LOCATION_INACCURATE", 900.0, None),
        ({"latitude": NEAR["latitude"], "longitude": NEAR["longitude"]}, "VALIDATOR_LOCATION_INACCURATE", None, None),
        (FAR, "VALIDATOR_OUT_OF_ZONE", pytest.approx(1112, abs=5), 100.0),
    ],
)
def test_a_location_problem_is_only_a_signal_by_default(client, located, location, code, value, threshold):
    assert profile(client, located)["location_required"] is True
    identified(identify(client, located, signed=signed_for(client, located), location=location))
    reason = last_reasons()[code]
    assert (reason["mode"], reason["value"], reason["threshold"]) == ("OBSERVE", value, threshold)
    assert reason["kind"] == "LOCATION"


def test_a_fresh_location_inside_feeds_the_place_signals(client, located):
    frozen = [{"latitude": 29.0734, "longitude": -110.9559, "accuracy": 10}] * 3
    identified(identify(client, located, signed=signed_for(client, located), location=NEAR, samples=frozen))
    reasons = last_reasons()
    assert not {code for code in reasons if code.startswith("VALIDATOR_")}
    assert {"LOCATION_STATIC", "LOCATION_ROUND_ACCURACY"} <= set(reasons)  # las lecturas de la fase 1b
    malformed = identify(client, located, signed=signed_for(client, located), location=NEAR, samples="[{]")
    error(malformed, 422, "LOCATION_SAMPLES_INVALID")


@pytest.mark.parametrize(
    ("location", "code", "fragment"),
    [
        (None, "LOCATION_REQUIRED", "solo identifica en su lugar"),
        ({**NEAR, "accuracy": 900}, "LOCATION_INACCURATE", "no es precisa"),
        (FAR, "LOCATION_OUT_OF_RANGE", "para identificar"),
    ],
)
def test_with_required_location_each_identification_must_be_in_place(
    client, company_headers, located, location, code, fragment
):
    set_policy(client, company_headers, validator_location="ENFORCE")
    challenge = client.post("/api/face/challenge", headers=located).json()["data"]
    rejected = send(client, located, challenge, signed=signed_for(client, located), location=location)
    assert fragment in error(rejected, 403, code)["message"]
    identified(send(client, located, challenge, signed=signed_for(client, located), location=NEAR))  # mismo reto


def test_qr_carries_its_location_in_the_json_body(client, company_headers):
    employee = approved(client, company_headers, "juan", number="EMP-001")
    set_policy(client, company_headers, validator_device_approval=False, validator_location="ENFORCE")
    created = create_validator(client, company_headers, mode="QR", location_required=True, location_radius_m=100)
    assert created.status_code == 201
    body = {"email": "recepcion@empresa.com", "password": PASSWORD, "location": NEAR}
    headers = {"Authorization": f"Bearer {client.post('/api/auth/login', json=body).json()['data']['access_token']}"}
    qr = qr_content(employee["id"])
    far = client.post("/api/checkpoint/identify/qr", json={"qr_content": qr, "location": FAR}, headers=headers)
    error(far, 403, "LOCATION_OUT_OF_RANGE")
    samples = [NEAR, {**NEAR, "accuracy": 12}]
    near = client.post(
        "/api/checkpoint/identify/qr",
        json={"qr_content": qr, "location": NEAR, "location_samples": samples},
        headers=headers,
    )
    assert near.status_code == 200 and near.json()["data"]["verified"] is True


def test_without_location_checks_nothing_is_asked(client, company_headers, located):
    set_policy(client, company_headers, validator_location="OFF")
    assert profile(client, located)["location_required"] is False
    identified(identify(client, located, signed=signed_for(client, located)))
    assert not {code for code in last_reasons() if code.startswith("VALIDATOR_LOCATION")}


def test_the_signed_digest_is_the_sha256_of_the_first_capture():
    assert digest_of(FACE) == hashlib.sha256(FACE).hexdigest()
    assert signed_message("n", "qr", "d") == "n.qr.d"


# ---------------------------------------------------------------- en el motor de riesgo


def test_a_forged_signature_is_a_hard_rule_once_required(client, company_headers, setup):
    """La firma alterada es regla dura: exigida (por el ADMIN, tras calibrar), niega sola sin importar el puntaje."""
    set_policy(client, company_headers, risk_signals={"VALIDATOR_SIGNATURE_INVALID": {"mode": "ENFORCE"}})
    nonce = profile(client, setup)["device_nonce"]
    denied = identify(client, setup, signed=signature(nonce, "face", b"face:otra"))
    assert denied.status_code == 422 and denied.json()["code"] == "RISK_DENIED"
    assert assessments()[-1].action == "DENY"


def test_the_presence_signals_only_ask_for_more_even_when_required(client, company_headers, setup):
    """Las demás señales de la fase 2b piden más como mucho: aunque sumen lo de un intento crítico, queda «en
    revisión» (nunca se niega por ellas hasta calibrarlas)."""
    set_policy(client, company_headers, risk_signals={"VALIDATOR_KEY_MISMATCH": {"mode": "ENFORCE", "points": 100}})
    nonce = profile(client, setup)["device_nonce"]
    data = identified(identify(client, setup, signed=signature(nonce, "face", FACE, OTHER_KEY)))
    assert data["review"] is True and assessments()[-1].action == "REVIEW"
