"""Prueba de vida completa del registro y lentes permitidos (decisiones del dueño del producto, 2026-10-07).

- El reto del registro (`POST /face/challenge?purpose=ENROLLMENT`) pide SIEMPRE los cuatro movimientos de la cabeza
  (derecha, izquierda, arriba, abajo) en orden al azar, sin «acercarse», sin importar `liveness_steps` ni el refuerzo
  por ataques; la verificación sigue con los de la política.
- El registro (propio y en persona) rechaza un reto de verificación (`LIVENESS_REQUIRED`) y acepta el suyo; las
  métricas del intento guardan los cuatro pasos; un validador nunca recibe el reto del registro.
- Los lentes están apagados por omisión (la migración `0082` deja `block_glasses` en `false`): un rostro con lentes se
  registra y se verifica, y la validación previa los informa (`accessories`) para la insignia de la app; con la regla
  encendida por el ADMIN, el servidor vuelve a rechazarlos como cualquier otro accesorio.
"""

from app.facial_recognition import LivenessAction
from app.services.liveness_service import (
    ENROLLMENT_ACTIONS,
    MAX_CHALLENGE_STEPS,
    Challenge,
    challenge_store,
    enrollment_actions,
    is_enrollment_challenge,
)
from tests.conftest import (
    SessionLocal,
    approved_employee,
    complete_voice,
    create_employee,
    enrollment_challenge,
    initial_photo,
    login,
    submit_enrollment,
    turn_files,
)
from tests.test_policy import set_policy
from tests.test_validators import approved, identify_face, validator_headers

FOUR = {a.value for a in ENROLLMENT_ACTIONS}


def _employee(client, company_headers, email: str = "juan@empresa.com", number: str = "EMP-001") -> dict[str, str]:
    assert create_employee(client, company_headers, email=email, number=number).status_code == 201
    return login(client, email, "Empleado123")


def _enroll_with(client, headers, challenge: dict, person: str = "juan"):
    assert initial_photo(client, headers, f"face:{person}".encode()).status_code == 201
    files = [("images", (f"f{i}.jpg", f"face:{person}".encode(), "image/jpeg")) for i in range(3)]
    files += turn_files(challenge, person)
    return client.post(
        "/api/enrollment/face", data={"challenge_id": challenge["challenge_id"]}, files=files, headers=headers
    )


# ---------------------------------------------------------------- reglas puras


def test_the_enrollment_actions_are_the_four_head_movements_in_a_random_order():
    assert {"TURN_RIGHT", "TURN_LEFT", "LOOK_UP", "LOOK_DOWN"} == FOUR and MAX_CHALLENGE_STEPS == 4
    orders = {enrollment_actions() for _ in range(40)}
    assert all(set(order) == set(ENROLLMENT_ACTIONS) and len(order) == 4 for order in orders)
    assert len(orders) > 1  # el orden cambia (24 posibles): un video grabado no conoce la secuencia


def test_only_a_challenge_with_the_four_movements_is_an_enrollment_challenge():
    def challenge(*actions: LivenessAction) -> Challenge:
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        return Challenge(id="c", user_id=1, actions=actions, issued_at=now, expires_at=now)

    assert is_enrollment_challenge(challenge(*ENROLLMENT_ACTIONS))
    assert is_enrollment_challenge(challenge(*reversed(ENROLLMENT_ACTIONS)))
    assert not is_enrollment_challenge(challenge(LivenessAction.TURN_LEFT, LivenessAction.TURN_RIGHT))
    assert not is_enrollment_challenge(challenge(*ENROLLMENT_ACTIONS[:3], LivenessAction.MOVE_CLOSER))
    assert not is_enrollment_challenge(challenge(*ENROLLMENT_ACTIONS[:3], LivenessAction.TURN_RIGHT))


# ---------------------------------------------------------------- el reto por la API


def test_the_enrollment_challenge_always_brings_the_four_movements(client, company_headers):
    headers = _employee(client, company_headers)
    set_policy(client, company_headers, liveness_steps=1)
    verification = client.post("/api/face/challenge", headers=headers).json()["data"]
    assert len(verification["actions"]) == 1  # la verificación sigue con la política
    seen = set()
    for _ in range(8):
        challenge = enrollment_challenge(client, headers)
        assert set(challenge["actions"]) == FOUR and len(challenge["actions"]) == 4
        assert challenge["action"] == challenge["actions"][0] and len(challenge["instructions"]) == 4
        assert challenge["instruction"] == challenge["instructions"][0]
        seen.add(tuple(challenge["actions"]))
    assert len(seen) > 1
    # El reto se guarda y se consume con sus cuatro movimientos.
    with SessionLocal() as db:
        issued = challenge_store.issue(db, 1, steps=2, lifetime_seconds=60, actions=enrollment_actions())
        consumed = challenge_store.consume(db, issued.id, 1)
    assert consumed is not None and consumed.actions == issued.actions and len(consumed.actions) == 4


def test_an_enrollment_needs_its_own_challenge(client, company_headers):
    headers = _employee(client, company_headers)
    # Un reto de verificación (dos movimientos) no sirve para registrarse: falta la prueba de vida completa.
    verification = client.post("/api/face/challenge", headers=headers).json()["data"]
    refused = _enroll_with(client, headers, verification)
    assert refused.status_code == 422 and refused.json()["code"] == "LIVENESS_REQUIRED"
    assert client.get("/api/users/me", headers=headers).json()["data"]["employee"]["face_status"] == "NOT_ENROLLED"
    # Con el suyo (los cuatro movimientos, una captura por cada uno) se registra.
    accepted = _enroll_with(client, headers, enrollment_challenge(client, headers))
    assert accepted.status_code == 201, accepted.text
    complete_voice(client, headers, accepted.json()["data"])
    assert client.get("/api/users/me", headers=headers).json()["data"]["employee"]["face_status"] == "PENDING_REVIEW"


def test_the_in_person_enrollment_also_needs_the_four_movements(client, company_headers):
    employee = create_employee(client, company_headers).json()["data"]
    verification = client.post("/api/face/challenge", headers=company_headers).json()["data"]
    files = [("images", (f"f{i}.jpg", b"face:juan", "image/jpeg")) for i in range(3)] + turn_files(verification)
    refused = client.post(
        f"/api/employees/{employee['id']}/face/enroll",
        data={"challenge_id": verification["challenge_id"]},
        files=files,
        headers=company_headers,
    )
    assert refused.status_code == 422 and refused.json()["code"] == "LIVENESS_REQUIRED"
    challenge = enrollment_challenge(client, company_headers)
    files = [("images", (f"f{i}.jpg", b"face:juan", "image/jpeg")) for i in range(3)] + turn_files(challenge)
    enrolled = client.post(
        f"/api/employees/{employee['id']}/face/enroll",
        data={"challenge_id": challenge["challenge_id"]},
        files=files,
        headers=company_headers,
    )
    assert enrolled.status_code == 201 and enrolled.json()["data"]["face_status"] == "APPROVED"


def test_a_validator_never_gets_the_enrollment_challenge_and_an_unknown_purpose_is_rejected(client, company_headers):
    approved(client, company_headers, "juan", number="EMP-001")
    validator = validator_headers(client, company_headers, mode="FACE")
    challenge = client.post("/api/face/challenge", params={"purpose": "ENROLLMENT"}, headers=validator)
    assert challenge.status_code == 200 and len(challenge.json()["data"]["actions"]) <= 3
    unknown = client.post("/api/face/challenge", params={"purpose": "SOMETHING"}, headers=validator)
    assert unknown.status_code == 422 and unknown.json()["success"] is False
    # Sin prueba de vida en la política, tampoco el registro pide movimientos.
    headers = _employee(client, company_headers, email="ana@empresa.com", number="EMP-002")
    set_policy(client, company_headers, liveness_challenge=False)
    assert enrollment_challenge(client, headers)["liveness_required"] is False
    assert initial_photo(client, headers, b"face:ana").status_code == 201
    files = [("images", (f"f{i}.jpg", b"face:ana", "image/jpeg")) for i in range(3)]
    assert client.post("/api/enrollment/face", files=files, headers=headers).status_code == 201


# ---------------------------------------------------------------- lentes: apagados por omisión, insignia siempre


def check(client, headers, image: bytes):
    return client.post("/api/face/check", files={"image": ("c.jpg", image, "image/jpeg")}, headers=headers)


def test_glasses_are_allowed_by_default_and_the_precheck_reports_every_accessory(client, company_headers):
    headers = _employee(client, company_headers)
    policy = client.get("/api/settings/verification", headers=headers).json()["data"]
    assert policy["block_glasses"] is False and policy["block_mask"] is True
    # Apagado: la foto pasa y la validación previa INFORMA los lentes (la insignia de la app), sin bloquear.
    with_glasses = check(client, headers, b"glasses:juan")
    assert with_glasses.status_code == 200 and with_glasses.json()["data"]["accessories"] == ["GLASSES"]
    assert check(client, headers, b"face:juan").json()["data"]["accessories"] == []
    # Lo bloqueado sigue rechazando con sus códigos en `details` (la insignia también sale de ahí).
    masked = check(client, headers, b"mask:juan")
    assert masked.status_code == 422 and masked.json()["errors"][0]["details"]["accessories"] == ["MASK"]
    enrolled = submit_enrollment(client, headers, frontal=(b"glasses:juan",) * 3)
    assert enrolled.status_code == 201, enrolled.text
    enrollment_id = enrolled.json()["data"]["enrollment_id"]
    detail = client.get(f"/api/enrollments/{enrollment_id}", headers=company_headers)
    assert detail.json()["data"]["flagged_accessories"] == []  # sin marca para el revisor: no se bloquea
    approve = client.post(f"/api/enrollments/{enrollment_id}/approve", headers=company_headers)
    assert approve.status_code == 200, approve.text
    verified = identify_face(client, validator_headers(client, company_headers, mode="FACE"), "juan", kind="glasses")
    assert verified.json()["data"]["verified"] is True


def test_the_admin_can_still_block_glasses_and_the_server_respects_it(client, company_headers):
    headers = _employee(client, company_headers)
    assert set_policy(client, company_headers, block_glasses=True)["block_glasses"] is True
    blocked = check(client, headers, b"glasses:juan")
    assert blocked.status_code == 422 and blocked.json()["code"] == "ACCESSORIES_DETECTED"
    assert blocked.json()["errors"][0]["details"]["accessories"] == ["GLASSES"]
    refused = submit_enrollment(client, headers, frontal=(b"glasses:juan",) * 3)
    assert refused.status_code == 422 and refused.json()["code"] == "ACCESSORIES_DETECTED"
    assert submit_enrollment(client, headers).status_code == 201  # sin lentes, se registra
    assert approved_employee(client, company_headers, email="ana@empresa.com", number="EMP-002")  # la regla no estorba
