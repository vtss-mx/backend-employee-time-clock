"""Llaves de acceso (WebAuthn / passkeys; antifraude fase 3): registrar, listar, renombrar y revocar las propias, y
entrar con una con las mismas reglas que la contraseña. El autenticador de pruebas (`tests/passkey_support.py`) arma
las credenciales byte por byte, como las devuelve el navegador."""

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from webauthn.helpers import bytes_to_base64url

from app.core.config import settings
from app.core.database import SessionLocal
from app.models import Company, ErrorReport, Passkey, PasskeyChallenge, User
from app.repositories.passkey_repository import PasskeyRepository
from app.services import maintenance_service, passkey_service
from app.services.error_reporter import error_reporter
from tests.conftest import COMPANY_EMAIL, create_employee, login
from tests.passkey_support import FakeAuthenticator, login_with, register

URL = "/api/auth/passkeys"


def _employee(client, company_headers) -> dict[str, str]:
    create_employee(client, company_headers, number="EMP-001", email="ana@empresa.com")
    return login(client, "ana@empresa.com", "Empleado123")


# ---------------------------------------------------------------- registrar


def test_registration_options_carry_a_sealed_challenge_and_the_webauthn_options(client, company_headers):
    response = client.post(f"{URL}/options", headers=company_headers)
    assert response.status_code == 200 and response.json()["code"] == "PASSKEY_OPTIONS"
    data = response.json()["data"]
    options = data["options"]
    assert options["rp"] == {"id": settings.WEBAUTHN_RP_ID, "name": "Employee Time Clock"}
    assert options["user"]["name"] == COMPANY_EMAIL and options["attestation"] == "none"
    selection = options["authenticatorSelection"]
    assert selection["residentKey"] == "required" and selection["userVerification"] == "required"
    assert options["timeout"] == settings.PASSKEY_CHALLENGE_TTL_SECONDS * 1000
    assert len(data["token"]) > 20 and options["excludeCredentials"] == []
    # El reto viaja sellado: nadie lo lee ni lo altera, y no queda nada en memoria ni en la base.
    with SessionLocal() as db:
        assert PasskeyRepository(db).challenges_count() == 0


def test_a_passkey_is_registered_listed_and_excluded_from_the_next_registration(client, company_headers):
    device = FakeAuthenticator()
    created = register(client, company_headers, device, name="  Mi   teléfono ")
    assert created.status_code == 201, created.text
    passkey = created.json()["data"]
    assert passkey["name"] == "Mi teléfono" and passkey["backed_up"] is True and passkey["transports"] == ["internal"]
    assert passkey["last_used_at"] is None and set(passkey) == {
        "id",
        "name",
        "created_at",
        "last_used_at",
        "transports",
        "backed_up",
    }
    listed = client.get(URL, headers=company_headers).json()
    assert listed["code"] == "PASSKEYS_LISTED" and listed["message"] == "1 llave de acceso"
    assert [item["id"] for item in listed["data"]["items"]] == [passkey["id"]]
    options = client.post(f"{URL}/options", headers=company_headers).json()["data"]["options"]
    assert [item["id"] for item in options["excludeCredentials"]] == [device.credential_id_b64]
    # El reto usado quedó anotado (una sola vez) y nunca la llave privada ni el id en claro de la persona.
    with SessionLocal() as db:
        stored = db.scalar(select(Passkey))
        assert stored.credential_id == device.credential_id_b64 and stored.public_key and stored.aaguid
        assert stored.sign_count == 0 and stored.backup_eligible and stored.transports == "internal"
        assert PasskeyRepository(db).challenges_count() == 1


def test_registration_rejects_a_bad_token_a_foreign_token_a_reused_one_and_a_duplicate(
    client, company_headers, monkeypatch
):
    device = FakeAuthenticator()
    options = client.post(f"{URL}/options", headers=company_headers).json()["data"]
    credential = device.register(options["options"])
    # Un token alterado o inventado.
    broken = client.post(
        URL, json={"token": "x" * 40, "name": "Llave", "credential": credential}, headers=company_headers
    )
    assert broken.status_code == 422 and broken.json()["code"] == "PASSKEY_CHALLENGE_INVALID"
    assert broken.json()["errors"][0]["field"] == "token"
    # El reto de otra persona.
    employee = _employee(client, company_headers)
    foreign = client.post(
        URL, json={"token": options["token"], "name": "Llave", "credential": credential}, headers=employee
    )
    assert foreign.status_code == 422 and foreign.json()["code"] == "PASSKEY_CHALLENGE_INVALID"
    # Un reto vencido (uno emitido con una vigencia ya pasada).
    monkeypatch.setattr(settings, "PASSKEY_CHALLENGE_TTL_SECONDS", -1)
    stale = client.post(f"{URL}/options", headers=company_headers).json()["data"]
    monkeypatch.undo()
    expired = client.post(
        URL,
        json={"token": stale["token"], "name": "Llave", "credential": device.register(stale["options"])},
        headers=company_headers,
    )
    assert expired.status_code == 422 and expired.json()["code"] == "PASSKEY_CHALLENGE_INVALID"
    # El registro bueno; repetir el reto con OTRA credencial es un reto usado y la MISMA credencial, una repetida.
    first = client.post(
        URL, json={"token": options["token"], "name": "Llave", "credential": credential}, headers=company_headers
    )
    assert first.status_code == 201, first.text
    other = FakeAuthenticator().register(options["options"])
    reused = client.post(
        URL, json={"token": options["token"], "name": "Otra", "credential": other}, headers=company_headers
    )
    assert reused.status_code == 409 and reused.json()["code"] == "PASSKEY_CHALLENGE_USED"
    again = client.post(f"{URL}/options", headers=company_headers).json()["data"]
    duplicate = client.post(
        URL,
        json={"token": again["token"], "name": "Otra", "credential": device.register(again["options"])},
        headers=company_headers,
    )
    assert duplicate.status_code == 409 and duplicate.json()["code"] == "PASSKEY_ALREADY_REGISTERED"


def test_registration_rejects_a_credential_that_does_not_verify(client, company_headers):
    options = client.post(f"{URL}/options", headers=company_headers).json()["data"]
    # Desde un origen que no es de la plataforma.
    foreign_origin = FakeAuthenticator().register(options["options"], origin="https://atacante.example")
    bad = client.post(
        URL, json={"token": options["token"], "name": "Llave", "credential": foreign_origin}, headers=company_headers
    )
    assert bad.status_code == 422 and bad.json()["code"] == "PASSKEY_INVALID"
    assert bad.json()["errors"][0]["field"] == "credential"
    # Sin verificar a la persona (sin rostro, huella ni PIN): el servidor la exige.
    unverified = FakeAuthenticator(user_verified=False).register(options["options"])
    assert (
        client.post(
            URL, json={"token": options["token"], "name": "Llave", "credential": unverified}, headers=company_headers
        ).json()["code"]
        == "PASSKEY_INVALID"
    )
    # Una credencial mal formada o demasiado grande.
    assert (
        client.post(
            URL, json={"token": options["token"], "name": "Llave", "credential": {"id": "x"}}, headers=company_headers
        ).json()["code"]
        == "PASSKEY_INVALID"
    )
    huge = {"id": "x", "response": {"attestationObject": "A" * 40_000}}
    assert (
        client.post(
            URL, json={"token": options["token"], "name": "Llave", "credential": huge}, headers=company_headers
        ).json()["code"]
        == "PASSKEY_INVALID"
    )
    # Nada quedó registrado y el reto sigue sin usarse (una falla no lo gasta).
    with SessionLocal() as db:
        assert db.scalar(select(Passkey)) is None and PasskeyRepository(db).challenges_count() == 0


def test_the_passkeys_per_account_are_limited(client, company_headers, monkeypatch):
    monkeypatch.setattr(settings, "PASSKEYS_MAX_PER_USER", 1)
    options = client.post(f"{URL}/options", headers=company_headers).json()["data"]
    assert register(client, company_headers, FakeAuthenticator()).status_code == 201
    # Con el tope alcanzado ya no se dan retos nuevos (409) ni se acepta uno pedido antes del tope.
    limit = client.post(f"{URL}/options", headers=company_headers)
    assert limit.status_code == 409 and limit.json()["code"] == "PASSKEY_LIMIT_REACHED"
    assert limit.json()["message"] == "Ya tienes 1 llave de acceso. Revoca una para registrar otra."
    late = client.post(
        URL,
        json={
            "token": options["token"],
            "name": "Otra",
            "credential": FakeAuthenticator().register(options["options"]),
        },
        headers=company_headers,
    )
    assert late.status_code == 409 and late.json()["code"] == "PASSKEY_LIMIT_REACHED"


# ---------------------------------------------------------------- renombrar y revocar


def test_rename_and_revoke_only_the_own_passkeys(client, company_headers):
    employee = _employee(client, company_headers)
    passkey = register(client, company_headers, FakeAuthenticator()).json()["data"]
    renamed = client.patch(f"{URL}/{passkey['id']}", json={"name": " Laptop  del trabajo "}, headers=company_headers)
    assert renamed.status_code == 200 and renamed.json()["code"] == "PASSKEY_RENAMED"
    assert renamed.json()["data"]["name"] == "Laptop del trabajo"
    # La llave de otra persona no existe para quien no es su dueño (ni para renombrar ni para revocar).
    assert client.patch(f"{URL}/{passkey['id']}", json={"name": "Robada"}, headers=employee).status_code == 404
    assert client.delete(f"{URL}/{passkey['id']}", headers=employee).json()["code"] == "PASSKEY_NOT_FOUND"
    assert client.delete(f"{URL}/999999", headers=company_headers).status_code == 404
    revoked = client.delete(f"{URL}/{passkey['id']}", headers=company_headers)
    assert revoked.status_code == 200 and revoked.json()["code"] == "PASSKEY_REVOKED"
    assert client.get(URL, headers=company_headers).json()["data"]["total"] == 0
    with SessionLocal() as db:  # borrado real: revocar no es eliminar
        assert db.scalar(select(Passkey)) is None


# ---------------------------------------------------------------- entrar


def test_signing_in_with_a_passkey_issues_the_same_session_as_the_password(client, company_headers):
    device = FakeAuthenticator(synced=False)
    passkey_id = register(client, company_headers, device).json()["data"]["id"]
    options = client.post("/api/auth/login/passkey/options")
    assert options.status_code == 200 and options.json()["code"] == "PASSKEY_LOGIN_OPTIONS"
    request = options.json()["data"]["options"]
    assert request["rpId"] == settings.WEBAUTHN_RP_ID and request["userVerification"] == "required"
    assert request.get("allowCredentials", []) == []  # la persona elige la llave en su dispositivo
    signed = login_with(client, device, remember=True)
    assert signed.status_code == 200, signed.text
    body = signed.json()
    assert body["code"] == "LOGIN_SUCCESS" and body["data"]["user"]["email"] == COMPANY_EMAIL
    assert body["data"]["access_token"] and body["data"]["session_id"]
    assert settings.REFRESH_COOKIE_NAME in signed.cookies and settings.REMEMBER_COOKIE_NAME in signed.cookies
    headers = {"Authorization": f"Bearer {body['data']['access_token']}"}
    me = client.get("/api/users/me", headers=headers).json()["data"]
    assert me["email"] == COMPANY_EMAIL and me["screens"]
    # La sesión es una de las de la cuenta (se lista y se revoca como cualquiera) y la llave registra su uso.
    sessions = client.get("/api/auth/sessions", headers=headers).json()["data"]["items"]
    assert any(session["id"] == body["data"]["session_id"] for session in sessions)
    with SessionLocal() as db:  # el autenticador firmó dos veces: al registrarse y al entrar
        stored = db.get(Passkey, passkey_id)
        assert stored.sign_count == 2 and stored.last_used_at is not None


def test_a_login_challenge_is_single_use_and_a_bad_signature_never_says_why(client, company_headers, monkeypatch):
    device = FakeAuthenticator()
    register(client, company_headers, device)
    options = client.post("/api/auth/login/passkey/options").json()["data"]
    credential = device.sign(options["options"])
    first = client.post("/api/auth/login/passkey", json={"token": options["token"], "credential": credential})
    assert first.status_code == 200
    # El mismo reto firmado otra vez (reenvío): ya se usó.
    replay = client.post("/api/auth/login/passkey", json={"token": options["token"], "credential": credential})
    assert replay.status_code == 401 and replay.json()["code"] == "PASSKEY_LOGIN_FAILED"
    assert replay.json()["message"] == "Ese reto ya se usó. Intenta de nuevo."
    # Un token inventado, una llave desconocida, un origen ajeno, una firma de otra llave: siempre el mismo 401.
    fresh = client.post("/api/auth/login/passkey/options").json()["data"]
    for body in (
        {"token": "x" * 40, "credential": device.sign(fresh["options"])},
        {"token": fresh["token"], "credential": FakeAuthenticator().sign(fresh["options"])},
        {"token": fresh["token"], "credential": device.sign(fresh["options"], origin="https://atacante.example")},
        {
            "token": fresh["token"],
            "credential": {**device.sign(fresh["options"]), "response": {"attestationObject": "A" * 40_000}},
        },
        {"token": fresh["token"], "credential": {"id": device.credential_id_b64, "response": {}}},
    ):
        failed = client.post("/api/auth/login/passkey", json=body)
        assert failed.status_code == 401 and failed.json()["code"] == "PASSKEY_LOGIN_FAILED", (body, failed.text)
    # Un reto de registro no sirve para entrar (otro uso: otra llave de sellado). La sesión nueva es la que vale: con
    # una sola sesión por cuenta, entrar con la llave cerró la anterior (`company_headers`).
    session = {"Authorization": f"Bearer {first.json()['data']['access_token']}"}
    registration = client.post(f"{URL}/options", headers=session).json()["data"]
    crossed = client.post(
        "/api/auth/login/passkey",
        json={"token": registration["token"], "credential": device.sign(registration["options"])},
    )
    assert crossed.status_code == 401
    # Vencido.
    monkeypatch.setattr(settings, "PASSKEY_CHALLENGE_TTL_SECONDS", -1)
    stale = client.post("/api/auth/login/passkey/options").json()["data"]
    monkeypatch.undo()
    expired = client.post(
        "/api/auth/login/passkey", json={"token": stale["token"], "credential": device.sign(stale["options"])}
    )
    assert expired.status_code == 401 and expired.json()["code"] == "PASSKEY_LOGIN_FAILED"
    # Las fallas no gastan retos ni dejan filas de más.
    with SessionLocal() as db:
        assert PasskeyRepository(db).challenges_count() == 2  # el registro y el único inicio de sesión que pasó


def test_a_cloned_passkey_is_revoked_and_the_admin_is_told(client, company_headers, admin_headers):
    device = FakeAuthenticator(synced=False)
    passkey_id = register(client, company_headers, device).json()["data"]["id"]
    assert login_with(client, device).status_code == 200  # contador 2 (registrarse fue la firma 1)
    # Una copia de la llave privada firma con un contador que no avanzó.
    options = client.post("/api/auth/login/passkey/options").json()["data"]
    cloned = client.post(
        "/api/auth/login/passkey",
        json={"token": options["token"], "credential": device.sign(options["options"], count=2)},
    )
    assert cloned.status_code == 401 and cloned.json()["code"] == "PASSKEY_CLONED"
    with SessionLocal() as db:
        assert db.get(Passkey, passkey_id) is None
    # La llave legítima ya tampoco entra (se revocó) y el ADMIN tiene su error del sistema.
    assert login_with(client, device).json()["code"] == "PASSKEY_LOGIN_FAILED"
    error_reporter.flush()
    with SessionLocal() as db:
        report = db.scalar(select(ErrorReport).where(ErrorReport.code == "app.services.passkey_service"))
        assert report is not None and "contador de firmas no avanzó" in report.message


def test_passkey_login_respects_the_account_rules(client, company_headers, admin_headers, monkeypatch):
    device = FakeAuthenticator()
    register(client, company_headers, device)
    # Cuenta desactivada: 401 como con la contraseña.
    with SessionLocal() as db:
        db.execute(update(User).where(User.email == COMPANY_EMAIL).values(active=False))
        db.commit()
    inactive = login_with(client, device)
    assert inactive.status_code == 401 and inactive.json()["code"] == "USER_INACTIVE"
    with SessionLocal() as db:
        db.execute(update(User).where(User.email == COMPANY_EMAIL).values(active=True))
        company_id = db.scalar(select(User.company_id).where(User.email == COMPANY_EMAIL))
        db.execute(
            update(Company)
            .where(Company.id == company_id)
            .values(suspended_at=datetime.now(UTC), suspension_reason="MANUAL")
        )
        db.commit()
    suspended = login_with(client, device)
    assert suspended.status_code == 403 and suspended.json()["code"] == "COMPANY_SUSPENDED"
    with SessionLocal() as db:
        db.execute(update(Company).where(Company.id == company_id).values(suspended_at=None, suspension_reason=None))
        db.commit()
    # El límite de intentos por llave (como el de la contraseña por correo): van dos intentos con esta llave.
    monkeypatch.setattr(settings, "RATE_LIMIT_LOGIN_PER_MINUTE", 3)
    assert login_with(client, device).status_code == 200
    assert login_with(client, device).status_code == 429


def test_a_validator_signing_in_with_a_passkey_still_proves_its_device(client, company_headers):
    from tests.conftest import device_proof
    from tests.test_validators import validator_headers

    headers = validator_headers(client, company_headers, mode="QR_OR_FACE")
    device = FakeAuthenticator()
    assert register(client, headers, device).status_code == 201
    # La empresa autoriza dispositivos (por omisión): la llave de acceso entra solo con la prueba del dispositivo
    # (403 primero, con el reto; el dispositivo de pruebas ya quedó autorizado al crear el validador).
    denied = login_with(client, device)
    assert denied.status_code == 403 and denied.json()["code"] == "DEVICE_PROOF_REQUIRED"
    nonce = denied.json()["errors"][0]["details"]["nonce"]
    allowed = login_with(client, device, device=device_proof(nonce))
    assert allowed.status_code == 200, allowed.text


# ---------------------------------------------------------------- lo técnico


def test_used_challenges_are_purged_when_they_expire():
    with SessionLocal() as db:
        repo = PasskeyRepository(db)
        past = datetime.now(UTC) - timedelta(minutes=10)
        assert repo.claim_challenge("a" * 64, past) and not repo.claim_challenge("a" * 64, past)
        assert repo.claim_challenge("b" * 64, datetime.now(UTC) + timedelta(minutes=10))
        db.commit()
    with SessionLocal() as db:
        removed = maintenance_service.purge_expired(db)
    assert removed["retos de llaves de acceso usados"] == 1
    with SessionLocal() as db:
        assert db.scalar(select(PasskeyChallenge.digest)) == "b" * 64


def test_webauthn_origins_default_to_cors_plus_the_site(monkeypatch):
    monkeypatch.setattr(settings, "WEBAUTHN_ORIGINS", [])
    assert settings.webauthn_origins == [*settings.CORS_ORIGINS, f"https://{settings.WEBAUTHN_RP_ID}"]
    monkeypatch.setattr(settings, "WEBAUTHN_ORIGINS", ["https://reloj.empresa.com"])
    assert settings.webauthn_origins == ["https://reloj.empresa.com"]
    assert settings._split_origins("https://a.com, https://b.com,") == ["https://a.com", "https://b.com"]


def test_sign_count_parsing_tolerates_garbage():
    assert passkey_service._sign_count_of({"response": {"authenticatorData": "no-es-base64url!!"}}) is None
    assert passkey_service._sign_count_of({}) is None
    valid = bytes_to_base64url(b"\x00" * 32 + b"\x05" + (7).to_bytes(4, "big"))
    assert passkey_service._sign_count_of({"response": {"authenticatorData": valid}}) == 7
    assert passkey_service._credential_too_big({"a": "b" * 40_000}) and not passkey_service._credential_too_big({})
    assert json.loads(json.dumps({"id": 1}))["id"] == 1
