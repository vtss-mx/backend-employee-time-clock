"""Movimientos de la prueba de vida configurables y lentes permitidos (decisiones del dueño del producto, 2026-10-08).

- El reto del registro (`POST /face/challenge?purpose=ENROLLMENT`) pide los movimientos de cabeza que la empresa
  habilitó (por omisión solo los giros; decisión 2026-10-08, reemplaza «siempre los cuatro»), en orden al azar, sin
  «acercarse», sin importar `liveness_steps` ni el refuerzo; la verificación elige los suyos de ese mismo repertorio.
- El registro (propio y en persona) rechaza un reto de verificación incompleto (`LIVENESS_REQUIRED`) y acepta el suyo;
  un validador nunca recibe el reto del registro.
- Los lentes están apagados por omisión (la migración `0082` deja `block_glasses` en `false`): un rostro con lentes se
  registra y se verifica, y la validación previa los informa (`accessories`) para la insignia de la app; con la regla
  encendida por el ADMIN, el servidor vuelve a rechazarlos como cualquier otro accesorio.
"""

from app.facial_recognition import LivenessAction
from app.services.liveness_service import (
    HEAD_ACTIONS,
    MAX_CHALLENGE_STEPS,
    Challenge,
    challenge_store,
    enrollment_actions,
    is_enrollment_challenge,
)
from app.services.policy_service import PolicySnapshot
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

#: Todos los movimientos de cabeza posibles, y los dos de un registro por omisión (solo giros, decisión 2026-10-08).
FOUR = {action.value for action, _ in HEAD_ACTIONS}
TWO = {"TURN_RIGHT", "TURN_LEFT"}
#: Una empresa por omisión (solo giros) y una que habilitó los cuatro movimientos.
DEFAULT_POLICY = PolicySnapshot()
ALL_MOVES_POLICY = PolicySnapshot(enable_look_up=True, enable_look_down=True)


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


def test_the_enrollment_actions_are_the_enabled_head_movements_in_a_random_order():
    assert {"TURN_RIGHT", "TURN_LEFT", "LOOK_UP", "LOOK_DOWN"} == FOUR and MAX_CHALLENGE_STEPS == 4
    # Por omisión solo los giros (decisión del dueño, 2026-10-08).
    giros = {LivenessAction.TURN_RIGHT, LivenessAction.TURN_LEFT}
    assert all(set(enrollment_actions(DEFAULT_POLICY)) == giros for _ in range(20))
    # Una empresa que los habilitó todos los recibe todos, en orden al azar.
    four = {enrollment_actions(ALL_MOVES_POLICY) for _ in range(40)}
    assert all(set(order) == FOUR and len(order) == 4 for order in four) and len(four) > 1


def test_only_a_challenge_with_the_enabled_movements_is_an_enrollment_challenge():
    def challenge(*actions: LivenessAction) -> Challenge:
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        return Challenge(id="c", user_id=1, actions=actions, issued_at=now, expires_at=now)

    # Por omisión, el registro pide exactamente los dos giros (en cualquier orden).
    assert is_enrollment_challenge(challenge(LivenessAction.TURN_RIGHT, LivenessAction.TURN_LEFT), DEFAULT_POLICY)
    assert is_enrollment_challenge(challenge(LivenessAction.TURN_LEFT, LivenessAction.TURN_RIGHT), DEFAULT_POLICY)
    # Un reto con menos, con «acercarse» o con un movimiento que no se habilitó no es el del registro.
    assert not is_enrollment_challenge(challenge(LivenessAction.TURN_RIGHT), DEFAULT_POLICY)
    assert not is_enrollment_challenge(challenge(LivenessAction.TURN_RIGHT, LivenessAction.MOVE_CLOSER), DEFAULT_POLICY)
    assert not is_enrollment_challenge(challenge(LivenessAction.TURN_RIGHT, LivenessAction.LOOK_UP), DEFAULT_POLICY)
    # Una empresa que habilitó los cuatro exige los cuatro (los dos giros ya no bastan).
    four = (LivenessAction.TURN_RIGHT, LivenessAction.TURN_LEFT, LivenessAction.LOOK_UP, LivenessAction.LOOK_DOWN)
    assert is_enrollment_challenge(challenge(*four), ALL_MOVES_POLICY)
    assert not is_enrollment_challenge(challenge(LivenessAction.TURN_RIGHT, LivenessAction.TURN_LEFT), ALL_MOVES_POLICY)


# ---------------------------------------------------------------- el reto por la API


def test_the_enrollment_challenge_brings_the_enabled_movements(client, company_headers):
    headers = _employee(client, company_headers)
    set_policy(client, company_headers, liveness_steps=1)
    verification = client.post("/api/face/challenge", headers=headers).json()["data"]
    assert len(verification["actions"]) == 1  # la verificación sigue con la política
    seen = set()
    for _ in range(8):
        challenge = enrollment_challenge(client, headers)
        assert set(challenge["actions"]) == TWO and len(challenge["actions"]) == 2  # por omisión, solo los giros
        assert challenge["action"] == challenge["actions"][0] and len(challenge["instructions"]) == 2
        assert challenge["instruction"] == challenge["instructions"][0]
        seen.add(tuple(challenge["actions"]))
    assert len(seen) > 1  # el orden cambia
    # Con los cuatro habilitados, el registro los pide todos.
    set_policy(client, company_headers, enable_look_up=True, enable_look_down=True)
    four = enrollment_challenge(client, headers)
    assert set(four["actions"]) == FOUR and len(four["actions"]) == 4
    # El reto se guarda y se consume con sus movimientos.
    with SessionLocal() as db:
        issued = challenge_store.issue(db, 1, steps=2, lifetime_seconds=60, actions=enrollment_actions(DEFAULT_POLICY))
        consumed = challenge_store.consume(db, issued.id, 1)
    assert consumed is not None and consumed.actions == issued.actions and len(consumed.actions) == 2


def test_an_enrollment_needs_its_own_challenge(client, company_headers):
    headers = _employee(client, company_headers)
    # Un reto de verificación (un solo movimiento) no sirve para registrarse: faltan movimientos de la prueba de vida.
    set_policy(client, company_headers, liveness_steps=1)
    verification = client.post("/api/face/challenge", headers=headers).json()["data"]
    refused = _enroll_with(client, headers, verification)
    assert refused.status_code == 422 and refused.json()["code"] == "LIVENESS_REQUIRED"
    assert client.get("/api/users/me", headers=headers).json()["data"]["employee"]["face_status"] == "NOT_ENROLLED"
    # Con el suyo (los cuatro movimientos, una captura por cada uno) se registra.
    accepted = _enroll_with(client, headers, enrollment_challenge(client, headers))
    assert accepted.status_code == 201, accepted.text
    complete_voice(client, headers, accepted.json()["data"])
    assert client.get("/api/users/me", headers=headers).json()["data"]["employee"]["face_status"] == "PENDING_REVIEW"


def test_the_in_person_enrollment_also_needs_its_own_challenge(client, company_headers):
    employee = create_employee(client, company_headers).json()["data"]
    set_policy(client, company_headers, liveness_steps=1)  # la verificación trae un solo movimiento
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
