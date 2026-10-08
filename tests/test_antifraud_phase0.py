"""Fase 0 del antifraude (arreglos sin rechazos de más): "acercarse" nunca solo, asistencia con QR solo apagada en
las empresas nuevas (D4), sospecha de duplicado al registrarse (más sensible, solo marca) y el cociente rostro/fondo
del destello en la autocalibración."""

from datetime import UTC, datetime

from sqlalchemy import select, update

from app.core.config import settings
from app.core.database import SessionLocal
from app.facial_recognition import LivenessAction
from app.models import Company, FaceAttemptMetric
from app.services import face_security
from app.services.liveness_service import challenge_actions
from tests.conftest import approved_employee, create_employee, login, qr_content, submit_enrollment
from tests.test_attendance import clock, worker  # noqa: F401 (fixtures)
from tests.test_policy import set_policy
from tests.test_validators import approved, validator_headers


def test_moving_closer_is_never_the_only_step():
    every = tuple(LivenessAction)
    for _ in range(200):
        assert challenge_actions(every, 1) != (LivenessAction.MOVE_CLOSER,)
    # Con dos o más siempre hay otro movimiento (nunca se repite el mismo seguido).
    assert any(a != LivenessAction.MOVE_CLOSER for a in challenge_actions(every, 2))
    # Si el catálogo solo deja "acercarse" (configuración rota), se pide eso antes que nada.
    assert challenge_actions((LivenessAction.MOVE_CLOSER,), 1) == (LivenessAction.MOVE_CLOSER,)


def test_a_one_step_challenge_from_the_api_is_never_only_closer(client, company_headers):
    # Fase 0: con un solo movimiento, "acercarse" nunca es el único (una foto plana crece igual que un rostro). El
    # repertorio de la verificación sale de la política (los giros por omisión, decisión del dueño 2026-10-08) más
    # "acercarse" del catálogo, así que un reto de un paso siempre es un giro de cabeza.
    headers = approved_employee(client, company_headers)
    set_policy(client, company_headers, liveness_steps=1)
    for _ in range(10):
        actions = client.post("/api/face/challenge", headers=headers).json()["data"]["actions"]
        assert actions in (["TURN_RIGHT"], ["TURN_LEFT"])  # un giro, nunca solo "acercarse"


def test_a_new_company_does_not_record_attendance_with_the_qr_alone(client, company_headers, worker, clock):  # noqa: F811
    validator = validator_headers(client, company_headers, mode="QR")
    clock(worker["day"], "07:55")
    data = client.post(
        "/api/checkpoint/identify/qr", json={"qr_content": qr_content(worker["id"])}, headers=validator
    ).json()["data"]
    assert data["verified"] is True and data["attendance"] == {
        "action": None,
        "message": "Identificado con QR. Para registrar la asistencia, identifícate con tu rostro.",
    }
    # Una empresa que ya la usaba la conserva (la migración la deja encendida); el ADMIN la controla.
    set_policy(client, company_headers, qr_only_attendance=True)
    recorded = client.post(
        "/api/checkpoint/identify/qr", json={"qr_content": qr_content(worker["id"])}, headers=validator
    ).json()["data"]
    assert recorded["attendance"]["action"] == "CHECK_IN"


def test_a_similar_face_is_flagged_for_review_but_never_blocked(client, company_headers):
    approved(client, company_headers, "juan", number="EMP-001")
    # Ana se parece a Juan menos de lo que exige la aceptación (no es un duplicado), pero más que la sospecha.
    create_employee(client, company_headers, number="EMP-002", email="ana@empresa.com")
    ana = login(client, "ana@empresa.com", "Empleado123")
    frontal = (b"face:juan~0.55~ana",) * 3
    submitted = submit_enrollment(client, ana, frontal=frontal, turn_person="juan~0.55~ana")
    assert submitted.status_code == 201, submitted.text
    detail = client.get(f"/api/enrollments/{submitted.json()['data']['enrollment_id']}", headers=company_headers)
    data = detail.json()["data"]
    assert data["flagged_accessories"] == ["POSSIBLE_DUPLICATE"]
    assert len(data["similar"]) == 1 and data["similar"][0]["full_name"] and data["similar"][0]["similarity"] > 0.5
    # Con la sospecha al nivel de la aceptación (o apagada) no se marca.
    set_policy(client, company_headers, duplicate_confidence=0.99999)
    create_employee(client, company_headers, number="EMP-003", email="luis@empresa.com")
    luis = login(client, "luis@empresa.com", "Empleado123")
    again = submit_enrollment(client, luis, frontal=(b"face:juan~0.55~luis",) * 3, turn_person="juan~0.55~luis")
    flags = client.get(f"/api/enrollments/{again.json()['data']['enrollment_id']}", headers=company_headers)
    assert flags.json()["data"]["flagged_accessories"] == [] and flags.json()["data"]["similar"] == []


def test_a_flagged_employee_who_left_is_not_listed(client, company_headers):
    juan = approved(client, company_headers, "juan", number="EMP-001")
    create_employee(client, company_headers, number="EMP-002", email="ana@empresa.com")
    ana = login(client, "ana@empresa.com", "Empleado123")
    submitted = submit_enrollment(client, ana, frontal=(b"face:juan~0.55~ana",) * 3, turn_person="juan~0.55~ana")
    assert client.delete(f"/api/employees/{juan['id']}", headers=company_headers).status_code == 200
    detail = client.get(f"/api/enrollments/{submitted.json()['data']['enrollment_id']}", headers=company_headers)
    assert detail.json()["data"]["similar"] == []


def test_the_flash_ratio_is_calibrated_and_only_tightens():
    with SessionLocal() as db:
        company_id = db.scalar(select(Company.id))
        db.add_all(
            FaceAttemptMetric(
                company_id=company_id, success=True, flash_score=0.9, flash_magnitude=0.02, flash_ratio=2.0
            )
            for _ in range(settings.FACE_AUTOCALIBRATION_MIN_SAMPLES)
        )
        db.commit()
    with SessionLocal() as db:
        face_security.recalibrate(db, datetime.now(UTC))
        limits = face_security.thresholds(db)
    # 1 + (2.0 − 1) × 0.85 = 1.85 (sobre el piso de la configuración, bajo su tope).
    assert limits.flash_min_ratio == 1.85
    # Lo que la revisión confirmó como fraude no cuenta como persona real.
    with SessionLocal() as db:
        db.execute(update(FaceAttemptMetric).values(fraud_label="FRAUD"))
        db.commit()
        face_security.recalibrate(db, datetime.now(UTC))
        assert face_security.thresholds(db).flash_min_ratio == settings.FACE_FLASH_MIN_FACE_RATIO
        overview = face_security.overview(db, datetime.now(UTC))
    assert {t.key for t in overview.thresholds} >= {"FLASH_RATIO"} and overview.flash.ratio_median is None
