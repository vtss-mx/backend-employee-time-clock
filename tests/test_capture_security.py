"""Protección contra engaños en la identificación facial: cámaras virtuales, imágenes que no son de
la cámara, fotos fijas, reenvíos, tomas armadas, pantallas en el giro, respuestas automáticas al
reto, bloqueo por intentos y el mismo rostro en dos empleados."""

import io
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from PIL import Image
from sqlalchemy import update

from app.core.config import settings
from app.core.database import SessionLocal
from app.facial_recognition import FacePolicy, FaceValidationError
from app.facial_recognition.image_utils import decode_image
from app.facial_recognition.pipeline import capture_traits
from app.models import CaptureFingerprint, VerificationLog
from app.services.capture_guard import (
    ensure_continuity,
    ensure_not_static,
    ensure_same_take,
    is_virtual_camera,
    spoofed,
)
from app.services.face_service import SuspiciousCapture
from tests.conftest import _analysis, approved_employee, create_employee, login, submit_enrollment, turn_files
from tests.test_in_person_face import in_person
from tests.test_policy import set_policy
from tests.test_validators import approved, validator_headers

VERIFY = "/api/verification/face"
CHECKPOINT = "/api/checkpoint/identify/face"


def attempt(client, headers, url=VERIFY, *, frontal=("face:juan", "face:juan"), turn="turn:juan", camera=None):
    """Un intento completo: reto, capturas frontales, giro (`turn` → turn-left/turn-right) y cámara."""
    challenge = client.post("/api/face/challenge", headers=headers).json()["data"]
    files = [("images", (f"f{i}.jpg", f.encode(), "image/jpeg")) for i, f in enumerate(frontal)]
    kind, _, person = turn.partition(":")
    files += turn_files(challenge, person, image=f"{kind}:{{person}}")
    data = {"challenge_id": challenge["challenge_id"], **({"camera_label": camera} if camera else {})}
    return client.post(url, data=data, files=files, headers=headers)


def employee_id(client, headers) -> int:
    return client.get("/api/users/me", headers=headers).json()["data"]["employee"]["id"]


def history(client, company_headers, employee: int) -> list[tuple[bool, str | None]]:
    items = client.get(f"/api/employees/{employee}/verifications", headers=company_headers).json()["data"]["items"]
    return [(item["success"], item["reason"]) for item in items]


# ---------------- Reglas puras ----------------


def test_virtual_cameras_are_recognized_by_whole_words():
    for name in ("OBS Virtual Camera", "ManyCam Virtual Webcam", "Camo", "DroidCam Source 3", "Snap Camera"):
        assert is_virtual_camera(name), name
    for name in ("FaceTime HD Camera", "OBSBOT Tiny 2", "Back Dual Wide Camera", "Camouflage Cam", "", None):
        assert not is_virtual_camera(name), name


def test_images_with_camera_or_editor_metadata_are_not_live_captures():
    def jpeg(**tags: str) -> bytes:
        image, buffer = Image.new("RGB", (240, 240), (120, 90, 60)), io.BytesIO()
        exif = image.getexif()
        for tag, value in tags.items():
            exif[int(tag.removeprefix("t"))] = value
        image.save(buffer, "JPEG", exif=exif)
        return buffer.getvalue()

    options = {"min_dimension": 160, "max_dimension": 4096}
    for foreign in (jpeg(t271="Canon"), jpeg(t305="Adobe Photoshop")):  # Make / Software
        with pytest.raises(FaceValidationError) as error:
            decode_image(foreign, **options, reject_foreign=True)
        assert error.value.code == "IMAGE_NOT_FROM_CAMERA"
        assert decode_image(foreign, **options).shape == (240, 240, 3)  # sin la regla se acepta
    assert decode_image(jpeg(), **options, reject_foreign=True).shape == (240, 240, 3)  # captura de la app


def test_capture_traits_identify_each_frame():
    image = np.random.default_rng(1).integers(0, 255, (120, 160, 3), dtype=np.uint8)
    traits = capture_traits(image, image[:112, :112])
    assert traits.size == (160, 120) and traits.thumb.shape == (32, 32) and len(traits.digest) == 64
    assert capture_traits(image.copy(), image[:112, :112]).digest == traits.digest


def test_take_rules_static_frames_sizes_and_continuity():
    frame = replace(_analysis("juan"), face_thumb=np.zeros((32, 32), np.float32), image_size=(640, 480))
    other = replace(frame, capture_digest="b", face_thumb=np.full((32, 32), 4.0, np.float32))
    ensure_not_static([replace(frame, capture_digest="a"), other], [])
    with pytest.raises(SuspiciousCapture) as error:  # rostro idéntico píxel a píxel
        ensure_not_static([replace(frame, capture_digest="a"), replace(frame, capture_digest="c")], [])
    assert error.value.code == "STATIC_CAPTURE"
    with pytest.raises(SuspiciousCapture) as error:  # el giro es la misma imagen que una frontal
        ensure_not_static([replace(frame, capture_digest="a")], [replace(frame, capture_digest="a")])
    assert error.value.code == "STATIC_CAPTURE"
    with pytest.raises(SuspiciousCapture) as error:
        ensure_same_take([frame, replace(other, image_size=(1280, 720))], [])
    assert error.value.code == "CAPTURE_INCONSISTENT"

    ensure_continuity(frame, replace(frame, face_box=(30, 0, 80, 100)))  # al girar el rostro se angosta
    for jump in ({"face_box": (0, 0, 300, 300)}, {"face_box": (500, 0, 100, 100)}, {"brightness": 220.0}):
        with pytest.raises(SuspiciousCapture):  # tamaño, lugar o luz imposibles en la misma toma
            ensure_continuity(frame, replace(frame, **jump))


def test_spoof_decision_follows_the_company_level():
    real, screen = replace(_analysis("juan"), real_probability=0.9), replace(_analysis("juan"), real_probability=0.01)
    standard, maximum = FacePolicy(), FacePolicy(spoof_threshold=0.5, spoof_any_frame=True)
    assert not spoofed([real, real], [screen], standard)  # un solo indicio no basta (falso positivo aislado)
    assert spoofed([real, screen], [screen], standard)  # el giro y una frontal: dos indicios
    assert spoofed([screen, screen, real], [], standard)
    assert not spoofed([real, screen, real], [], standard)
    assert spoofed([real, screen, real], [], maximum)  # en el nivel máximo basta una captura
    assert spoofed([real], [screen], maximum)
    mid = replace(real, real_probability=0.4)
    assert not spoofed([mid, mid], [], standard) and spoofed([mid, mid], [], maximum)


# ---------------- Verificación del empleado ----------------


def test_virtual_camera_is_rejected_and_logged(client, company_headers):
    headers = approved_employee(client, company_headers)
    blocked = attempt(client, headers, camera="OBS Virtual Camera")
    assert blocked.status_code == 422 and blocked.json()["code"] == "VIRTUAL_CAMERA"
    assert attempt(client, headers, camera="FaceTime HD Camera").json()["data"]["verified"] is True
    assert history(client, company_headers, employee_id(client, headers))[:2] == [
        (True, None),
        (False, "VIRTUAL_CAMERA"),
    ]


@pytest.mark.parametrize(
    ("frontal", "turn", "code"),
    [
        (("exif:juan", "face:juan"), "turn:juan", "IMAGE_NOT_FROM_CAMERA"),
        (("face:juan", "face:juan"), "exif:juan", "IMAGE_NOT_FROM_CAMERA"),
        (("face:juan#1", "face:juan#1"), "turn:juan", "STATIC_CAPTURE"),
        (("face:juan", "wide:juan"), "turn:juan", "CAPTURE_INCONSISTENT"),
        (("face:juan", "face:juan"), "turn-moved:juan", "CAPTURE_INCONSISTENT"),
        (("face:juan", "spoof:juan", "face:juan"), "spoof-turn:juan", "SPOOF_DETECTED"),
        (("spoof:juan", "spoof:juan"), "turn:juan", "SPOOF_DETECTED"),
    ],
)
def test_suspicious_takes_are_rejected_and_logged(client, company_headers, frontal, turn, code):
    headers = approved_employee(client, company_headers)
    response = attempt(client, headers, frontal=frontal, turn=turn)
    assert response.status_code == 422 and response.json()["code"] == code, response.text
    assert history(client, company_headers, employee_id(client, headers))[0] == (False, code)


def test_a_capture_cannot_be_sent_twice(client, company_headers):
    headers = approved_employee(client, company_headers)
    frames = ("face:juan#a", "face:juan#b")
    assert attempt(client, headers, frontal=frames).json()["data"]["verified"] is True
    replayed = attempt(client, headers, frontal=frames)
    assert replayed.status_code == 422 and replayed.json()["code"] == "REPLAY_DETECTED"

    # Las huellas se recuerdan un tiempo limitado (FACE_REPLAY_RETENTION_DAYS).
    with SessionLocal() as db:
        db.execute(update(CaptureFingerprint).values(created_at=datetime.now(UTC) - timedelta(days=60)))
        db.commit()
    assert attempt(client, headers, frontal=frames).json()["data"]["verified"] is True


def test_the_turn_cannot_arrive_faster_than_a_person(client, company_headers, monkeypatch):
    headers = approved_employee(client, company_headers)
    monkeypatch.setattr(settings, "FACE_CHALLENGE_MIN_SECONDS", 30.0)
    too_fast = attempt(client, headers)
    assert too_fast.status_code == 422 and too_fast.json()["code"] == "CHALLENGE_TOO_FAST"


def test_repeated_failures_lock_the_employee_for_a_while(client, company_headers):
    set_policy(client, company_headers, lockout_max_failures=3, lockout_minutes=15)
    headers = approved_employee(client, company_headers)
    employee = employee_id(client, headers)
    assert attempt(client, headers).json()["data"]["verified"] is True
    for _ in range(3):  # alguien con fotos de otra persona
        assert attempt(client, headers, frontal=("face:pedro", "face:pedro"), turn="turn:pedro").status_code == 200

    locked = attempt(client, headers)
    assert locked.status_code == 429 and locked.json()["code"] == "FACE_LOCKED"
    assert 0 < int(locked.headers["Retry-After"]) <= 15 * 60
    assert "espera 15 min" in locked.json()["message"]
    # Tampoco en persona: el bloqueo protege la identidad del empleado, no la cuenta.
    assert in_person(client, company_headers, employee, "verify").status_code == 429

    # Pasado el tiempo se libera.
    with SessionLocal() as db:
        old = datetime.now(UTC) - timedelta(minutes=16)
        db.execute(update(VerificationLog).where(VerificationLog.success.is_(False)).values(created_at=old))
        db.commit()
    assert attempt(client, headers).json()["data"]["verified"] is True


# ---------------- Punto de control y registro ----------------


def test_a_validator_is_locked_only_by_suspicious_attempts(client, company_headers):
    set_policy(client, company_headers, lockout_max_failures=3)
    approved(client, company_headers, "juan", number="EMP-001")
    headers = validator_headers(client, company_headers)
    for _ in range(4):  # en un acceso es normal que alguien no registrado no coincida
        assert attempt(client, headers, CHECKPOINT, frontal=("face:pedro",) * 2, turn="turn:pedro").status_code == 200
    for _ in range(3):
        assert attempt(client, headers, CHECKPOINT, camera="ManyCam Virtual Webcam").status_code == 422
    locked = attempt(client, headers, CHECKPOINT)
    assert locked.status_code == 429 and locked.json()["code"] == "FACE_LOCKED"


def test_the_same_face_cannot_be_two_employees(client, company_headers):
    approved_employee(client, company_headers)  # Juan, con su rostro aprobado
    juan = client.get("/api/employees", headers=company_headers).json()["data"]["items"][0]
    ana = create_employee(client, company_headers, number="EMP-002", email="ana@empresa.com").json()["data"]

    # Autoregistro con el rostro de Juan: se envía, pero marcado para quien revisa.
    assert submit_enrollment(client, login(client, "ana@empresa.com", "Empleado123")).status_code == 201
    pending = client.get("/api/enrollments", headers=company_headers).json()["data"]["items"][0]
    assert "DUPLICATE_FACE" in pending["flagged_accessories"]

    # En persona (se aprueba al momento) se bloquea; con su propio rostro sí se registra.
    duplicate = in_person(client, company_headers, ana["id"], "enroll", frames=3)
    assert duplicate.status_code == 409 and duplicate.json()["code"] == "FACE_ALREADY_REGISTERED"
    assert juan["employee_number"] in duplicate.json()["message"]
    assert in_person(client, company_headers, ana["id"], "enroll", person="ana").status_code == 201


def test_suspicious_enrollment_is_logged_for_the_employee(client, company_headers):
    created = create_employee(client, company_headers).json()["data"]
    headers = login(client, "juan@empresa.com", "Empleado123")
    rejected = attempt(client, headers, "/api/enrollment/face", camera="OBS Virtual Camera")
    assert rejected.status_code == 422 and rejected.json()["code"] == "VIRTUAL_CAMERA"
    assert history(client, company_headers, created["id"]) == [(False, "VIRTUAL_CAMERA")]


def test_the_policy_tells_the_app_which_cameras_are_blocked(client, company_headers):
    policy = client.get("/api/settings/verification", headers=company_headers).json()["data"]
    assert "virtual" in policy["blocked_cameras"] and "manycam" in policy["blocked_cameras"]


# ---------------- Cada candado lo decide la empresa ----------------


def test_policy_exposes_every_lock_enabled_by_default(client, company_headers):
    policy = client.get("/api/settings/verification", headers=company_headers).json()["data"]
    locks = (
        "block_virtual_cameras",
        "reject_foreign_images",
        "detect_static_captures",
        "detect_replays",
        "check_capture_continuity",
        "enforce_human_timing",
        "detect_duplicate_faces",
        "lockout_enabled",
    )
    assert all(policy[lock] is True for lock in locks)
    assert (policy["anti_spoofing_level"], policy["liveness_steps"]) == ("STANDARD", 2)
    assert (policy["lockout_max_failures"], policy["lockout_minutes"]) == (5, 15)
    for invalid in ({"liveness_steps": 3}, {"lockout_max_failures": 1}, {"lockout_minutes": 0}):
        assert client.put("/api/settings/verification", json=invalid, headers=company_headers).status_code == 422
    level = client.put("/api/settings/verification", json={"anti_spoofing_level": "PARANOID"}, headers=company_headers)
    assert level.status_code == 422 and level.json()["code"] == "INVALID_ANTISPOOF_LEVEL"


@pytest.mark.parametrize(
    ("switch", "kwargs"),
    [
        ("block_virtual_cameras", {"camera": "OBS Virtual Camera"}),
        ("reject_foreign_images", {"frontal": ("exif:juan", "face:juan")}),
        ("detect_static_captures", {"frontal": ("face:juan#1", "face:juan#1")}),
        ("check_capture_continuity", {"frontal": ("face:juan", "wide:juan")}),
        ("check_capture_continuity", {"turn": "turn-moved:juan"}),
    ],
)
def test_each_lock_can_be_turned_off_by_the_company(client, company_headers, switch, kwargs):
    headers = approved_employee(client, company_headers)
    assert attempt(client, headers, **kwargs).status_code == 422
    set_policy(client, company_headers, **{switch: False})
    assert attempt(client, headers, **kwargs).json()["data"]["verified"] is True


def test_replay_timing_and_lockout_can_be_turned_off(client, company_headers, monkeypatch):
    headers = approved_employee(client, company_headers)
    set_policy(client, company_headers, detect_replays=False, enforce_human_timing=False, lockout_enabled=False)
    frames = ("face:juan#a", "face:juan#b")
    assert attempt(client, headers, frontal=frames).json()["data"]["verified"] is True
    assert attempt(client, headers, frontal=frames).json()["data"]["verified"] is True  # mismo envío
    monkeypatch.setattr(settings, "FACE_CHALLENGE_MIN_SECONDS", 30.0)
    assert attempt(client, headers).json()["data"]["verified"] is True
    for _ in range(6):  # sin bloqueo
        assert attempt(client, headers, frontal=("face:pedro",) * 2, turn="turn:pedro").status_code == 200
    assert attempt(client, headers).json()["data"]["verified"] is True


def test_duplicate_detection_can_be_turned_off(client, company_headers):
    approved_employee(client, company_headers)
    ana = create_employee(client, company_headers, number="EMP-002", email="ana@empresa.com").json()["data"]
    set_policy(client, company_headers, detect_duplicate_faces=False)
    assert in_person(client, company_headers, ana["id"], "enroll").status_code == 201


def test_one_or_two_random_turns(client, company_headers):
    headers = approved_employee(client, company_headers)
    two = client.post("/api/face/challenge", headers=headers).json()["data"]
    assert len(two["actions"]) == 2 and len(two["instructions"]) == 2 and two["action"] == two["actions"][0]
    # Con dos giros, una sola captura no basta.
    files = [("images", ("f.jpg", b"face:juan", "image/jpeg")), *turn_files(two)[:1]]
    one_missing = client.post(VERIFY, data={"challenge_id": two["challenge_id"]}, files=files, headers=headers)
    assert one_missing.status_code == 422 and one_missing.json()["code"] == "LIVENESS_REQUIRED"

    set_policy(client, company_headers, liveness_steps=1)
    one = client.post("/api/face/challenge", headers=headers).json()["data"]
    assert len(one["actions"]) == 1
    assert attempt(client, headers).json()["data"]["verified"] is True


def test_maximum_anti_spoofing_rejects_a_single_suspicious_capture(client, company_headers):
    headers = approved_employee(client, company_headers)
    frames = ("face:juan", "spoof:juan", "face:juan")
    assert attempt(client, headers, frontal=frames).json()["data"]["verified"] is True  # estándar: decide la mayoría
    set_policy(client, company_headers, anti_spoofing_level="MAXIMUM")
    strict = attempt(client, headers, frontal=frames)
    assert strict.status_code == 422 and strict.json()["code"] == "SPOOF_DETECTED"
