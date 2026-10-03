"""Dispositivos de los validadores: cada uno se enrola con su llave y la empresa lo autoriza."""

import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.core.config import settings
from app.services.device_service import issue_nonce, nonce_is_valid, signature_is_valid
from tests.conftest import create_company, device_proof, login
from tests.test_policy import set_policy
from tests.test_validators import PASSWORD, URL, create_validator

EMAIL = "recepcion@empresa.com"


def _login(client, device=None, **extra):
    return client.post(
        "/api/auth/login",
        json={"email": EMAIL, "password": PASSWORD, **({"device": device} if device else {}), **extra},
    )


def _nonce(client) -> str:
    response = _login(client)
    assert response.status_code == 403 and response.json()["code"] == "DEVICE_PROOF_REQUIRED", response.text
    return response.json()["errors"][0]["details"]["nonce"]


def test_nonce_and_signature_rules():
    nonce = issue_nonce(7)
    assert nonce_is_valid(7, nonce) and not nonce_is_valid(8, nonce)  # ligado a la cuenta
    assert not nonce_is_valid(7, "basura") and not nonce_is_valid(7, nonce.replace(".", "x", 1))
    proof = device_proof(nonce)
    assert signature_is_valid(proof["public_key"], nonce, proof["signature"])
    assert not signature_is_valid(proof["public_key"], issue_nonce(7), proof["signature"])  # otro reto
    other = device_proof(nonce, ec.generate_private_key(ec.SECP256R1()))
    assert not signature_is_valid(proof["public_key"], nonce, other["signature"])  # otra llave
    p384 = ec.generate_private_key(ec.SECP384R1()).public_key()
    p384_der = p384.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    assert not signature_is_valid(base64.b64encode(p384_der).decode(), nonce, proof["signature"])  # solo P-256
    assert not signature_is_valid("no-es-base64!", nonce, proof["signature"])
    assert not signature_is_valid(proof["public_key"], nonce, base64.b64encode(b"x" * 10).decode())


def test_a_new_device_waits_for_the_company_and_then_signs_every_login(client, company_headers, monkeypatch):
    monkeypatch.setattr(settings, "RATE_LIMIT_LOGIN_PER_MINUTE", 100)  # muchos inicios de sesión seguidos
    validator = create_validator(client, company_headers).json()["data"]
    nonce = _nonce(client)

    invalid = _login(client, {**device_proof(nonce), "signature": device_proof(issue_nonce(1))["signature"]})
    assert invalid.status_code == 403 and invalid.json()["code"] == "DEVICE_PROOF_INVALID"

    pending = _login(client, device_proof(nonce))
    assert pending.status_code == 403 and pending.json()["code"] == "DEVICE_PENDING_APPROVAL"
    assert "Safari · iPadOS" in pending.json()["message"]
    listed = client.get(URL, headers=company_headers).json()["data"]["items"][0]
    assert (listed["devices_pending"], listed["devices_approved"]) == (1, 0)

    devices_url = f"{URL}/{validator['id']}/devices"
    device = client.get(devices_url, headers=company_headers).json()["data"]["items"][0]
    assert device["status"] == "PENDING" and device["name"] == "Safari · iPadOS"
    status_url = f"{devices_url}/{device['id']}/status"
    assert client.patch(status_url, json={"status": "REVOKED"}, headers=company_headers).status_code == 409
    approved = client.patch(status_url, json={"status": "APPROVED"}, headers=company_headers).json()["data"]
    assert approved["status"] == "APPROVED" and approved["reviewed_by"] == "admin@empresa.com"

    # Con el dispositivo autorizado entra; la sesión de otro dispositivo (otra llave) no.
    session = _login(client, device_proof(_nonce(client)))
    assert session.status_code == 200, session.text
    headers = {"Authorization": f"Bearer {session.json()['data']['access_token']}"}
    assert client.get("/api/checkpoint/me", headers=headers).status_code == 200
    stranger = _login(
        client, device_proof(_nonce(client), ec.generate_private_key(ec.SECP256R1()), name="Chrome · Android")
    )
    assert stranger.json()["code"] == "DEVICE_PENDING_APPROVAL"
    assert client.get(devices_url, headers=company_headers).json()["data"]["total"] == 2

    # Revocar cierra su sesión y ya no entra; autorizarlo de nuevo lo devuelve.
    assert client.patch(status_url, json={"status": "REVOKED"}, headers=company_headers).status_code == 200
    assert client.get("/api/checkpoint/me", headers=headers).status_code == 401
    assert _login(client, device_proof(_nonce(client))).json()["code"] == "DEVICE_REVOKED"
    assert client.patch(status_url, json={"status": "APPROVED"}, headers=company_headers).status_code == 200
    assert _login(client, device_proof(_nonce(client))).status_code == 200


def test_a_rejected_device_cannot_sign_in(client, company_headers):
    validator = create_validator(client, company_headers).json()["data"]
    assert _login(client, device_proof(_nonce(client))).json()["code"] == "DEVICE_PENDING_APPROVAL"
    devices_url = f"{URL}/{validator['id']}/devices"
    device = client.get(devices_url, headers=company_headers).json()["data"]["items"][0]
    rejected = client.patch(
        f"{devices_url}/{device['id']}/status", json={"status": "REJECTED"}, headers=company_headers
    )
    assert rejected.json()["data"]["status"] == "REJECTED"
    assert _login(client, device_proof(_nonce(client))).json()["code"] == "DEVICE_REJECTED"


def test_the_company_can_turn_device_approval_off(client, company_headers):
    create_validator(client, company_headers)
    assert _login(client).status_code == 403
    set_policy(client, company_headers, validator_device_approval=False)
    assert _login(client).status_code == 200


@pytest.mark.parametrize("role", ["company", "employee"])
def test_only_validators_enroll_devices(client, company_headers, role):
    if role == "company":
        response = client.post("/api/auth/login", json={"email": "admin@empresa.com", "password": "Admin1234"})
    else:
        from tests.conftest import create_employee

        create_employee(client, company_headers)
        response = client.post("/api/auth/login", json={"email": "juan@empresa.com", "password": "Empleado123"})
    assert response.status_code == 200


def test_devices_belong_to_their_company(client, company_headers, admin_headers):
    validator = create_validator(client, company_headers).json()["data"]
    _login(client, device_proof(_nonce(client)))
    devices_url = f"{URL}/{validator['id']}/devices"
    device = client.get(devices_url, headers=company_headers).json()["data"]["items"][0]
    create_company(client, admin_headers)
    other = login(client, "admin@panificadora.com", "Empresa1234")
    assert client.get(devices_url, headers=other).status_code == 404
    assert (
        client.patch(f"{devices_url}/{device['id']}/status", json={"status": "APPROVED"}, headers=other).status_code
        == 404
    )
    assert (
        client.patch(f"{devices_url}/999/status", json={"status": "APPROVED"}, headers=company_headers).status_code
        == 404
    )
