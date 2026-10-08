"""Verificación por voz y video del registro facial (decisión del dueño del producto, 2026-10-06).

El orden no se altera: fotos válidas → preguntas en video → fin. Aquí: las preguntas solo sobre datos registrados, la
sesión sellada (vence, no se altera, es de su dueño), cada motivo de rechazo con su código y la repetición de la misma
pregunta, el tope de intentos en la base, el clip aceptado cifrado en el bucket, el registro que no llega a la empresa
hasta terminar, la revisión de la empresa (video por la API, nunca el ADMIN), el rechazo y el borrado real, la
depuración y la política que lo exige (apagarla relaja).
"""

import base64
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.core.database import platform_session
from app.models import (
    EnrollmentVoiceAnswer,
    FaceEnrollment,
    StorageDeletion,
    VoiceQuestion,
)
from app.services import maintenance_service, voice_questions
from app.services.voice_questions import ANY_OF, Expected, VoiceSession, seal, unseal
from tests.conftest import (
    answer_voice,
    complete_voice,
    create_employee,
    login,
    submit_enrollment,
)
from tests.speech_support import voice_clip
from tests.test_policy import admin_policy, set_policy
from tests.test_policy_governance import put


def _employee(client, company_headers, **kwargs):
    assert create_employee(client, company_headers, **kwargs).status_code == 201
    return login(client, kwargs.get("email", "juan@empresa.com"), "Empleado123")


def _submitted(client, company_headers, **kwargs) -> tuple[dict, dict]:
    headers = _employee(client, company_headers, **kwargs)
    response = submit_enrollment(client, headers, voice=False)
    assert response.status_code == 201, response.text
    return headers, response.json()


def _session(token: str) -> VoiceSession:
    session = voice_questions._open(token)
    assert session is not None
    return session


def _truth(token: str, position: int) -> str:
    return _session(token).questions[position].answer.split(ANY_OF)[0]


def _enrollment(enrollment_id: int) -> FaceEnrollment:
    with platform_session() as db:
        enrollment = db.get(FaceEnrollment, enrollment_id)
        assert enrollment is not None
        db.expunge(enrollment)
        return enrollment


def _answers(enrollment_id: int) -> list[EnrollmentVoiceAnswer]:
    with platform_session() as db:
        rows = list(
            db.scalars(select(EnrollmentVoiceAnswer).where(EnrollmentVoiceAnswer.enrollment_id == enrollment_id))
        )
        for row in rows:
            db.expunge(row)
        return rows


# ---------------------------------------------------------------- las fotos no terminan el registro


def test_the_photos_open_the_voice_stage_and_nothing_is_visible_until_it_passes(client, company_headers):
    headers, submitted = _submitted(client, company_headers)
    data = submitted["data"]
    assert submitted["code"] == "ENROLLMENT_PHOTOS_ACCEPTED" and data["face_status"] == "NOT_ENROLLED"
    voice = data["voice"]
    assert len(voice["questions"]) == settings.VOICE_QUESTIONS_PER_SESSION
    assert [q["position"] for q in voice["questions"]] == [0, 1, 2]
    assert voice["retries"] == settings.VOICE_MAX_RETRIES_PER_QUESTION and voice["expires_in"] == 900
    # Las respuestas esperadas NUNCA viajan al cliente: solo el código y el TEXTO ya renderizado de cada pregunta.
    assert all(set(q) == {"position", "question", "text"} for q in voice["questions"])
    assert all(q["text"] for q in voice["questions"])
    # La empresa no ve el registro (ni en la bandeja ni en el detalle) y el empleado sigue en la pantalla de registro.
    inbox = client.get("/api/enrollments", headers=company_headers).json()["data"]
    assert inbox["total"] == 0
    detail = client.get(f"/api/enrollments/{data['enrollment_id']}", headers=company_headers)
    assert detail.status_code == 404 and detail.json()["code"] == "ENROLLMENT_NOT_FOUND"
    me = client.get("/api/users/me", headers=headers).json()["data"]
    assert me["employee"]["face_status"] == "NOT_ENROLLED"
    assert _enrollment(data["enrollment_id"]).voice_pending is True


def test_the_three_answers_finish_the_enrollment_and_the_company_reviews_the_video(client, company_headers, bucket):
    headers, submitted = _submitted(client, company_headers)
    data = submitted["data"]
    last = complete_voice(client, headers, data)
    assert last is not None
    body = last.json()
    assert body["code"] == "ENROLLMENT_VOICE_DONE" and body["data"]["done"] is True
    assert body["data"]["next_position"] is None
    me = client.get("/api/users/me", headers=headers).json()["data"]
    assert me["employee"]["face_status"] == "PENDING_REVIEW"
    enrollment = _enrollment(data["enrollment_id"])
    assert enrollment.voice_passed_at is not None and enrollment.voice_attempts == 0
    # La empresa ya lo ve, con su verificación por voz: tres respuestas, cada una con su clip cifrado en el bucket.
    inbox = client.get("/api/enrollments", headers=company_headers).json()["data"]
    assert inbox["total"] == 1
    detail = client.get(f"/api/enrollments/{data['enrollment_id']}", headers=company_headers).json()["data"]
    voice = detail["voice"]
    assert voice["required"] is True and voice["passed_at"] and voice["failed_attempts"] == 0
    assert [a["position"] for a in voice["answers"]] == [0, 1, 2]
    assert all(a["attempts"] == 1 and a["has_clip"] and a["transcript"] for a in voice["answers"])
    assert detail["flagged_accessories"] == []
    clips = [name for name in bucket.objects if "/voice/" in name]
    assert len(clips) == 3 and all(name.endswith(".webm.enc") for name in clips)
    # El clip llega por la API, descifrado y en base64 (nunca una URL del bucket); el ADMIN no tiene esta ruta.
    answer = voice["answers"][0]
    clip = client.get(f"/api/enrollments/{data['enrollment_id']}/voice/{answer['id']}/clip", headers=company_headers)
    assert clip.status_code == 200, clip.text
    payload = clip.json()["data"]
    assert payload["content_type"] == "video/webm" and payload["duration_ms"] == 2000
    assert base64.b64decode(payload["data"]).startswith(b"clip:juan|")
    assert payload["byte_size"] == len(base64.b64decode(payload["data"]))


#: Preguntas fiables que SIEMPRE son elegibles (no dependen de datos opcionales ni de apellidos de dos palabras).
ALWAYS_ELIGIBLE = {
    VoiceQuestion.FIRST_NAME,
    VoiceQuestion.SURNAMES,
    VoiceQuestion.FULL_NAME,
    VoiceQuestion.BIRTH_DATE,
    VoiceQuestion.BIRTH_MONTH,
    VoiceQuestion.BIRTH_YEAR,
    VoiceQuestion.BIRTH_DAY,
    VoiceQuestion.COMPANY_NAME,
    VoiceQuestion.ARITHMETIC_SUM,
}


def test_each_question_is_about_data_the_employee_has(client, company_headers):
    """Sin número, departamento ni turno (y apellidos de una palabra): el repertorio son los datos fiables más la suma;
    la sesión elige tres DISTINTAS al azar de ahí, cada una con su dato (la fecha y sus partes en ISO; la suma, su
    total)."""
    from app.models import Employee

    headers = _employee(client, company_headers)
    with platform_session() as db:
        employee = db.scalar(select(Employee))
        assert employee is not None
        employee.employee_number = None  # el número es opcional (migración 0076): sin él no se pregunta
        first_name, last_name = employee.first_name, employee.last_name
        full_name, birth = employee.full_name, employee.birth_date
        options = {option.kind: option.answer for option in voice_questions.eligible(db, employee, "Panificadora")}
        db.rollback()
    # Solo los fiables + la suma (apellidos «Pérez» es una palabra: sin primer/segundo apellido).
    assert set(options) == ALWAYS_ELIGIBLE
    assert options[VoiceQuestion.FIRST_NAME] == first_name and options[VoiceQuestion.SURNAMES] == last_name
    assert options[VoiceQuestion.FULL_NAME] == full_name
    assert options[VoiceQuestion.BIRTH_DATE] == birth.isoformat()
    assert options[VoiceQuestion.BIRTH_MONTH] == options[VoiceQuestion.BIRTH_YEAR] == birth.isoformat()
    assert options[VoiceQuestion.BIRTH_DAY] == birth.isoformat()
    assert int(options[VoiceQuestion.ARITHMETIC_SUM]) >= 2 * settings.VOICE_ARITHMETIC_MIN
    submitted = submit_enrollment(client, headers, voice=False).json()["data"]
    session = _session(submitted["voice"]["token"])
    kinds = [q.kind for q in session.questions]
    assert len(kinds) == len(set(kinds)) == settings.VOICE_QUESTIONS_PER_SESSION  # tres DISTINTAS
    # El empleado real SÍ tiene número (solo se quitó en la sesión revertida de arriba): es dato que tiene.
    assert set(kinds) <= ALWAYS_ELIGIBLE | {VoiceQuestion.EMPLOYEE_NUMBER}


def test_the_optional_data_becomes_eligible_only_when_registered(client, company_headers):
    from app.core.clock import business_today
    from tests.test_shifts import assign, create_shift, create_site

    department = client.post("/api/departments", json={"name": "Producción"}, headers=company_headers).json()["data"]
    site = create_site(client, company_headers, name="Planta Hermosillo")
    shift = create_shift(client, company_headers, name="Matutino", sites=[site["id"]])
    headers = _employee(client, company_headers, number="EMP-7412")
    me = client.get("/api/users/me", headers=headers).json()["data"]["employee"]
    assigned = assign(client, company_headers, me["id"], shift["id"], business_today())
    assert assigned.status_code == 201, assigned.text
    with platform_session() as db:
        from app.models import Employee

        employee = db.get(Employee, me["id"])
        assert employee is not None
        employee.department_id = department["id"]
        db.flush()
        options = voice_questions.eligible(db, employee, "Panificadora")
        db.rollback()
    by_kind = {option.kind: option.answer for option in options}
    assert by_kind[VoiceQuestion.EMPLOYEE_NUMBER] == "EMP-7412"
    assert by_kind[VoiceQuestion.DEPARTMENT] == "Producción"
    assert by_kind[VoiceQuestion.WORK_SITE] == "Planta Hermosillo"
    assert by_kind[VoiceQuestion.COMPANY_NAME] == "Panificadora"
    # Nueve fiables (nombre, apellidos, nombre completo, fecha/mes/año/día, empresa, suma) + los tres condicionales
    # (número, departamento, sitio); «Pérez» es una palabra, así que no hay primer/segundo apellido.
    assert set(by_kind) == ALWAYS_ELIGIBLE | {
        VoiceQuestion.EMPLOYEE_NUMBER,
        VoiceQuestion.DEPARTMENT,
        VoiceQuestion.WORK_SITE,
    }
    assert len(options) == 12 and len(voice_questions.choose(options, 3)) == 3
    assert len(voice_questions.choose(options, 99)) == 12  # nunca más de las elegibles


# ---------------------------------------------------------------- cada motivo de rechazo repite la pregunta


@pytest.mark.parametrize(
    ("clip", "code"),
    [
        (b"not a video at all", "VIDEO_UNSUPPORTED_FORMAT"),
        (voice_clip("juan", "lo que sea", seconds="0.3"), "ANSWER_TOO_SHORT"),
        (voice_clip("juan", "lo que sea", seconds="15"), "ANSWER_TOO_LONG"),
        (voice_clip("juan", "lo que sea", truncated="1"), "ANSWER_TOO_LONG"),
        (voice_clip("juan", "lo que sea", rms="-60"), "ANSWER_INAUDIBLE"),
        (voice_clip("juan", "lo que sea", ratio="0.05"), "ANSWER_INAUDIBLE"),
        (
            voice_clip(
                "juan",
                "",
            ),
            "ANSWER_UNCLEAR",
        ),
        (voice_clip("juan", "algo", nospeech="0.9"), "ANSWER_UNCLEAR"),
        (voice_clip("juan", "algo", logprob="-2.5"), "ANSWER_UNCLEAR"),
        (voice_clip("juan", "Pedro Páramo Rulfo"), "ANSWER_MISMATCH"),
        (voice_clip("luis", "TRUTH"), "VIDEO_FACE_MISMATCH"),
        (voice_clip("juan", "TRUTH", frame="noface"), "VIDEO_FACE_MISMATCH"),
        (voice_clip("juan", "TRUTH", frames="0"), "VIDEO_FACE_MISMATCH"),
        (voice_clip("juan", "TRUTH", frame="broken"), "VIDEO_FACE_MISMATCH"),
    ],
)
def test_a_rejected_answer_keeps_the_same_question_with_its_reason(client, company_headers, clip, code):
    headers, submitted = _submitted(client, company_headers)
    token = submitted["data"]["voice"]["token"]
    # La primera pregunta de esta prueba es siempre la del nombre (la sesión se vuelve a sellar con ella al frente).
    session = _session(token)
    name = Expected(VoiceQuestion.FULL_NAME, "Juan Pérez")
    ordered = replace(session, questions=(name, *session.questions[1:]))
    token = seal(ordered)
    if b"TRUTH" in clip:
        clip = clip.replace(b"TRUTH", name.answer.encode())
    response = answer_voice(client, headers, token, 0, clip)
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == code
    details = body["errors"][0]["details"]
    assert details["attempts_left"] == settings.VOICE_MAX_RETRIES_PER_QUESTION * 3 - 1
    renewed = _session(details["token"])
    assert renewed.passed == (False, False, False) and renewed.tries == (1, 0, 0)
    assert _enrollment(submitted["data"]["enrollment_id"]).voice_attempts == 1
    # Nada quedó: ni fila ni objeto.
    assert _answers(submitted["data"]["enrollment_id"]) == []
    # La misma pregunta, respondida bien, pasa (con la cuenta de intentos para la empresa).
    accepted = answer_voice(client, headers, details["token"], 0, voice_clip("juan", name.answer))
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["data"]["next_position"] == 1
    assert _answers(submitted["data"]["enrollment_id"])[0].attempts == 2


def test_the_too_long_message_says_the_limit(client, company_headers):
    headers, submitted = _submitted(client, company_headers)
    token = submitted["data"]["voice"]["token"]
    response = answer_voice(client, headers, token, 0, voice_clip("juan", "x", seconds="15"))
    assert response.status_code == 422
    assert "12" in response.json()["message"]
    assert response.json()["i18n"]["en-US"]["message"] == "The answer is too long. Answer in under 12 seconds."


def test_a_face_mismatch_marks_the_enrollment_and_retries_mark_it_too(client, company_headers):
    headers, submitted = _submitted(client, company_headers)
    data = submitted["data"]
    token = data["voice"]["token"]
    first = _truth(token, 0)
    rejected = answer_voice(client, headers, token, 0, voice_clip("luis", first))
    assert rejected.json()["code"] == "VIDEO_FACE_MISMATCH"
    token = rejected.json()["errors"][0]["details"]["token"]
    for position in range(3):
        response = answer_voice(client, headers, token, position, voice_clip("juan", _truth(token, position)))
        assert response.status_code == 200, response.text
        token = response.json()["data"]["token"]
    detail = client.get(f"/api/enrollments/{data['enrollment_id']}", headers=company_headers).json()["data"]
    assert sorted(detail["flagged_accessories"]) == ["VIDEO_FACE_MISMATCH", "VOICE_RETRIES"]
    assert detail["voice"]["failed_attempts"] == 1 and detail["voice"]["answers"][0]["attempts"] == 2


def test_the_retry_cap_lives_in_the_database_not_in_the_token(client, company_headers, bucket):
    headers, submitted = _submitted(client, company_headers)
    data = submitted["data"]
    original = data["voice"]["token"]
    total = settings.VOICE_MAX_RETRIES_PER_QUESTION * 3
    for attempt in range(1, total):
        # Siempre con el token ORIGINAL: un cliente que lo reenvía no reinicia su cuenta.
        response = answer_voice(client, headers, original, 0, voice_clip("juan", "respuesta equivocada"))
        assert response.status_code == 422 and response.json()["code"] == "ANSWER_MISMATCH", response.text
        assert response.json()["errors"][0]["details"]["attempts_left"] == total - attempt
    exhausted = answer_voice(client, headers, original, 0, voice_clip("juan", "respuesta equivocada"))
    assert exhausted.status_code == 422 and exhausted.json()["code"] == "VOICE_RETRIES_EXHAUSTED"
    # Ya no se acepta nada en esa sesión, ni la verdad.
    again = answer_voice(client, headers, original, 0, voice_clip("juan", _truth(original, 0)))
    assert again.json()["code"] == "VOICE_RETRIES_EXHAUSTED"
    # Un envío nuevo de fotos reemplaza el registro sin terminar: su foto a la cola del bucket y sin filas.
    resubmitted = submit_enrollment(client, headers, voice=False)
    assert resubmitted.status_code == 201, resubmitted.text
    with platform_session() as db:
        assert db.scalar(select(func.count()).select_from(FaceEnrollment)) == 1
        assert db.scalar(select(func.count()).select_from(StorageDeletion)) == 1


def test_an_answer_already_accepted_is_not_taken_twice(client, company_headers):
    headers, submitted = _submitted(client, company_headers)
    token = submitted["data"]["voice"]["token"]
    first = answer_voice(client, headers, token, 0, voice_clip("juan", _truth(token, 0)))
    assert first.status_code == 200
    renewed = first.json()["data"]["token"]
    repeated = answer_voice(client, headers, renewed, 0, voice_clip("juan", _truth(renewed, 0)))
    assert repeated.status_code == 409 and repeated.json()["code"] == "ANSWER_ALREADY_ACCEPTED"
    # Con el token anterior (sin esa respuesta) tampoco: la fila única de la posición es la última barrera.
    replay = answer_voice(client, headers, token, 0, voice_clip("juan", _truth(token, 0)))
    assert replay.status_code == 409 and replay.json()["code"] == "CONCURRENT_UPDATE"


# ---------------------------------------------------------------- la sesión sellada


def test_the_session_token_is_sealed_owned_and_expires(client, company_headers, monkeypatch):
    headers, submitted = _submitted(client, company_headers)
    token = submitted["data"]["voice"]["token"]
    clip = voice_clip("juan", _truth(token, 0))
    for bad in ("not-a-token", token[:-5] + "AAAAA", seal(replace(_session(token), company_id=999))):
        response = answer_voice(client, headers, bad, 0, clip)
        assert response.status_code == 422 and response.json()["code"] == "VOICE_SESSION_INVALID", bad[:20]
    out_of_range = answer_voice(client, headers, token, 7, clip)
    assert out_of_range.json()["code"] == "VOICE_SESSION_INVALID"
    # De otro usuario: inválido (nunca «vencido»).
    other = _employee(client, company_headers, email="ana@empresa.com", number="A-1")
    assert answer_voice(client, other, token, 0, clip).json()["code"] == "VOICE_SESSION_INVALID"
    # Vencido: su propio código (la app empieza de nuevo).
    monkeypatch.setattr(time, "time", lambda: _session(token).expires + 1)
    expired = answer_voice(client, headers, token, 0, clip)
    assert expired.status_code == 422 and expired.json()["code"] == "VOICE_SESSION_EXPIRED"
    assert unseal(token, 1, int(time.time())) is None and voice_questions.expired("garbage", 1, 0) is False


def test_a_session_of_a_replaced_enrollment_is_not_found(client, company_headers):
    headers, submitted = _submitted(client, company_headers)
    stale = submitted["data"]["voice"]["token"]
    fresh = submit_enrollment(client, headers, voice=False).json()["data"]["voice"]["token"]
    response = answer_voice(client, headers, stale, 0, voice_clip("juan", _truth(stale, 0)))
    assert response.status_code == 404 and response.json()["code"] == "ENROLLMENT_NOT_FOUND"
    assert answer_voice(client, headers, fresh, 0, voice_clip("juan", _truth(fresh, 0))).status_code == 200


def test_the_clip_is_bounded_and_the_route_is_rate_limited(client, company_headers, monkeypatch):
    headers, submitted = _submitted(client, company_headers)
    token = submitted["data"]["voice"]["token"]
    monkeypatch.setattr(settings, "FACE_VIDEO_MAX_MB", 0.0001)
    too_large = answer_voice(client, headers, token, 0, b"clip:juan|x|" + b"0" * 200)
    assert too_large.status_code == 413 and too_large.json()["code"] == "PAYLOAD_TOO_LARGE"
    assert "< 0.01 MB" in too_large.json()["message"]
    monkeypatch.setattr(settings, "FACE_VIDEO_MAX_MB", 8.0)
    empty = answer_voice(client, headers, token, 0, b"")
    assert empty.status_code == 422 and empty.json()["code"] == "VIDEO_UNSUPPORTED_FORMAT"
    monkeypatch.setattr(settings, "RATE_LIMIT_VOICE_ANSWERS_PER_MINUTE", 1)
    answer_voice(client, headers, token, 0, voice_clip("juan", "x"))
    limited = answer_voice(client, headers, token, 0, voice_clip("juan", "x"))
    assert limited.status_code == 429


# ---------------------------------------------------------------- fallas de las dependencias


def test_a_speech_engine_outage_is_a_retryable_503_and_counts_no_attempt(client, company_headers, speech):
    headers, submitted = _submitted(client, company_headers)
    token = submitted["data"]["voice"]["token"]
    speech.down = True
    response = answer_voice(client, headers, token, 0, voice_clip("juan", _truth(token, 0)))
    assert response.status_code == 503 and response.json()["code"] == "SPEECH_SERVICE_UNAVAILABLE"
    assert response.headers["Retry-After"]
    speech.down, speech.crash = False, True
    crashed = answer_voice(client, headers, token, 0, voice_clip("juan", _truth(token, 0)))
    assert crashed.status_code == 503 and crashed.json()["code"] == "SPEECH_SERVICE_UNAVAILABLE"
    assert _enrollment(submitted["data"]["enrollment_id"]).voice_attempts == 0
    assert speech.languages == ["es", "es"]


def test_the_language_of_the_request_drives_the_transcription(client, company_headers, speech):
    headers, submitted = _submitted(client, company_headers)
    token = submitted["data"]["voice"]["token"]
    english = {**headers, "Accept-Language": "en-US"}
    response = answer_voice(client, english, token, 0, voice_clip("juan", _truth(token, 0)))
    assert response.status_code == 200 and response.json()["message"] == "Answer accepted"
    assert speech.languages == ["en"]


def test_a_face_engine_crash_on_a_frame_is_a_503(client, company_headers):
    headers, submitted = _submitted(client, company_headers)
    token = submitted["data"]["voice"]["token"]
    response = answer_voice(client, headers, token, 0, voice_clip("juan", _truth(token, 0), frame="crash"))
    assert response.status_code == 503 and response.json()["code"] == "FACE_PROCESSING_ERROR"


def test_with_the_bucket_down_an_accepted_answer_is_not_recorded(client, company_headers, bucket):
    headers, submitted = _submitted(client, company_headers)
    token = submitted["data"]["voice"]["token"]
    bucket.down.add("put")
    response = answer_voice(client, headers, token, 0, voice_clip("juan", _truth(token, 0)))
    assert response.status_code == 503 and response.json()["code"] == "STORAGE_UNAVAILABLE"
    assert _answers(submitted["data"]["enrollment_id"]) == []
    bucket.down.clear()
    assert answer_voice(client, headers, token, 0, voice_clip("juan", _truth(token, 0))).status_code == 200


def test_a_database_failure_after_the_upload_removes_the_clip(client, company_headers, bucket, monkeypatch):
    from app.repositories.enrollment_repository import FaceEnrollmentRepository

    headers, submitted = _submitted(client, company_headers)
    token = submitted["data"]["voice"]["token"]

    def boom(self, answer):
        raise RuntimeError("la base se cayó")

    monkeypatch.setattr(FaceEnrollmentRepository, "add_voice_answer", boom)
    response = answer_voice(client, headers, token, 0, voice_clip("juan", _truth(token, 0)))
    assert response.status_code == 500
    assert not [name for name in bucket.objects if "/voice/" in name]


def test_the_clip_read_degrades_with_its_own_codes(client, company_headers, bucket, monkeypatch):
    headers, submitted = _submitted(client, company_headers)
    data = submitted["data"]
    complete_voice(client, headers, data)
    detail = client.get(f"/api/enrollments/{data['enrollment_id']}", headers=company_headers).json()["data"]
    answer_id = detail["voice"]["answers"][0]["id"]
    url = f"/api/enrollments/{data['enrollment_id']}/voice/{answer_id}/clip"
    bucket.down.add("get")
    assert client.get(url, headers=company_headers).json()["code"] == "STORAGE_UNAVAILABLE"
    bucket.down.clear()
    name = next(name for name in bucket.objects if "/voice/" in name)
    payload, metadata = bucket.objects[name]
    bucket.objects[name] = (b"\x00" + payload[1:], metadata)  # ya no es lo que se guardó
    assert client.get(url, headers=company_headers).json()["code"] == "STORAGE_UNAVAILABLE"
    bucket.objects[name] = (payload, metadata)
    from app.services import image_storage
    from app.services.image_storage import ImageUnreadable

    def unreadable(image, row):
        raise ImageUnreadable("ilegible")

    monkeypatch.setattr(image_storage, "read", unreadable)
    assert client.get(url, headers=company_headers).json()["code"] == "VOICE_CLIP_NOT_FOUND"
    assert (
        client.get(f"/api/enrollments/{data['enrollment_id']}/voice/999/clip", headers=company_headers).json()["code"]
        == "VOICE_CLIP_NOT_FOUND"
    )
    assert (
        client.get(f"/api/enrollments/999/voice/{answer_id}/clip", headers=company_headers).json()["code"]
        == "VOICE_CLIP_NOT_FOUND"
    )


# ---------------------------------------------------------------- rechazo, borrado real y depuración


def test_rejecting_the_enrollment_releases_the_videos(client, company_headers, bucket):
    headers, submitted = _submitted(client, company_headers)
    data = submitted["data"]
    complete_voice(client, headers, data)
    rejected = client.post(
        f"/api/enrollments/{data['enrollment_id']}/reject", json={"reason": "No se ve bien"}, headers=company_headers
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["data"]["voice"]["answers"] == []
    with platform_session() as db:
        queued = set(db.scalars(select(StorageDeletion.object_name)))
    assert sum("/voice/" in name for name in queued) == 3 and any(
        "face-enrollments" in name and ".jpg" in name for name in queued
    )
    assert _answers(data["enrollment_id"]) == []


def test_deleting_the_employee_erases_the_videos_for_real(client, company_headers, bucket):
    headers, submitted = _submitted(client, company_headers)
    data = submitted["data"]
    complete_voice(client, headers, data)
    me = client.get("/api/users/me", headers=headers).json()["data"]["employee"]
    assert client.delete(f"/api/employees/{me['id']}", headers=company_headers).status_code == 200
    with platform_session() as db:
        assert db.scalar(select(func.count()).select_from(EnrollmentVoiceAnswer)) == 0
        queued = set(db.scalars(select(StorageDeletion.object_name)))
    assert sum("/voice/" in name for name in queued) == 3


def test_the_videos_expire_and_abandoned_enrollments_are_purged(client, company_headers, bucket):
    headers, submitted = _submitted(client, company_headers)
    data = submitted["data"]
    first = answer_voice(
        client, headers, data["voice"]["token"], 0, voice_clip("juan", _truth(data["voice"]["token"], 0))
    )
    assert first.status_code == 200
    # Una respuesta aceptada de un registro que nunca terminó, con fecha vieja.
    with platform_session() as db:
        enrollment = db.get(FaceEnrollment, data["enrollment_id"])
        assert enrollment is not None
        enrollment.submitted_at = datetime.now(UTC) - timedelta(hours=settings.FACE_ENROLLMENT_DRAFT_HOURS + 1)
        db.commit()
    assert any("/voice/" in name for name in bucket.objects) and any(".jpg.enc" in name for name in bucket.objects)
    with platform_session() as db:
        purged = maintenance_service.purge_expired(db)
        assert db.scalar(select(func.count()).select_from(FaceEnrollment)) == 0
        assert db.scalar(select(func.count()).select_from(EnrollmentVoiceAnswer)) == 0
    # La misma vuelta vació la cola: la respuesta aceptada y la foto del registro abandonado salieron del bucket.
    assert purged["respuestas de registros faciales sin terminar"] == 1
    assert purged["registros faciales sin terminar"] == 1
    assert not any("/voice/" in name or ".jpg.enc" in name for name in bucket.objects)
    # Y uno terminado: sus videos vencen a los FACE_VIDEO_RETENTION_DAYS (la foto se queda).
    headers2, submitted2 = _submitted(client, company_headers, email="ana@empresa.com", number="A-1")
    complete_voice(client, headers2, submitted2["data"], person="juan")
    with platform_session() as db:
        for row in db.scalars(select(EnrollmentVoiceAnswer)):
            row.created_at = datetime.now(UTC) - timedelta(days=settings.FACE_VIDEO_RETENTION_DAYS + 1)
        db.commit()
    with platform_session() as db:
        purged = maintenance_service.purge_expired(db)
        assert db.scalar(select(func.count()).select_from(EnrollmentVoiceAnswer)) == 0
        assert db.scalar(select(func.count()).select_from(FaceEnrollment)) == 1
    assert purged["videos de la verificación por voz"] == 3
    assert not any("/voice/" in name for name in bucket.objects) and any(".jpg.enc" in name for name in bucket.objects)
    detail = client.get(f"/api/enrollments/{submitted2['data']['enrollment_id']}", headers=company_headers).json()[
        "data"
    ]
    assert detail["voice"]["required"] is True and detail["voice"]["answers"] == []


# ---------------------------------------------------------------- la política


def test_without_the_policy_switch_the_photos_finish_the_enrollment(client, company_headers):
    policy = set_policy(client, company_headers, voice_verification=False)
    assert policy["voice_verification"] is False
    headers, submitted = _submitted(client, company_headers)
    data = submitted["data"]
    assert submitted["code"] == "ENROLLMENT_SUBMITTED" and data["voice"] is None
    assert data["face_status"] == "PENDING_REVIEW"
    detail = client.get(f"/api/enrollments/{data['enrollment_id']}", headers=company_headers).json()["data"]
    assert detail["voice"] is None
    public = client.get("/api/settings/verification", headers=headers).json()["data"]
    assert public["voice_verification"] is False


def test_turning_the_voice_check_off_relaxes_and_waits_for_another_admin(client, company_headers, monkeypatch):
    # La regla de dos personas está apagada por omisión (un único ADMIN); aquí se enciende para probar que relaja.
    monkeypatch.setattr(settings, "POLICY_TWO_PERSON_RULE", True)
    url, admin = admin_policy(client, company_headers)
    outcome = put(client, url, admin, voice_verification=False)["data"]
    assert outcome["change"]["status"] == "PENDING" and outcome["policy"]["voice_verification"] is True


def test_in_person_enrollment_skips_the_voice_check(client, company_headers):
    from tests.conftest import enrollment_challenge, turn_files

    headers = _employee(client, company_headers)
    me = client.get("/api/users/me", headers=headers).json()["data"]["employee"]
    challenge = enrollment_challenge(client, company_headers)
    files = [("images", (f"f{i}.jpg", b"face:juan", "image/jpeg")) for i in range(3)] + turn_files(challenge, "juan")
    response = client.post(
        f"/api/employees/{me['id']}/face/enroll",
        data={"challenge_id": challenge["challenge_id"]},
        files=files,
        headers=company_headers,
    )
    assert response.status_code == 201, response.text
    assert response.json()["data"]["face_status"] == "APPROVED" and response.json()["data"]["voice"] is None


def test_a_site_question_accepts_any_site_of_the_shift():
    option = Expected(VoiceQuestion.WORK_SITE, f"Planta Norte{ANY_OF}Oficina Centro")
    from app.services.voice_verification import _best_match

    assert _best_match(option, "trabajo en la oficina centro", "es-MX").ok
    assert not _best_match(option, "en mi casa", "es-MX").ok


# ---------------------------------------------------------------- bordes de las funciones puras y las carreras


def test_the_face_check_needs_references_and_a_measurable_face():
    """Sin muestras del registro no hay con qué comparar; sin rostro medible en ningún fotograma tampoco."""
    from app.services.voice_verification import _face_similarity
    from tests.conftest import FakePipeline

    pipeline = FakePipeline()
    assert _face_similarity(pipeline, (b"face:juan",), []) is None
    assert _face_similarity(pipeline, (b"noface", b"multi"), [pipeline.identity_of(b"face:juan")]) is None
    assert _face_similarity(pipeline, (b"noface", b"face:juan"), [pipeline.identity_of(b"face:juan")]) == 1.0


def test_finishing_a_session_whose_enrollment_is_gone_or_done_is_not_pending(client, company_headers):
    """Carrera: entre el veredicto y el cierre, el registro se reemplazó o ya terminó → 404 VOICE_NOT_PENDING."""
    from app.core.exceptions import NotFoundError
    from app.services.voice_verification import VoiceVerificationService

    headers, submitted = _submitted(client, company_headers)
    enrollment_id = submitted["data"]["enrollment_id"]
    session = _session(submitted["data"]["voice"]["token"])
    with platform_session() as db:
        service = VoiceVerificationService(db, 1)
        with pytest.raises(NotFoundError) as missing:
            service._finish(enrollment_id + 999, session, datetime.now(UTC))
        assert missing.value.code == "VOICE_NOT_PENDING"
        complete_voice(client, headers, submitted["data"])  # ya terminó: tampoco está pendiente
        with pytest.raises(NotFoundError) as done:
            service._finish(enrollment_id, session, datetime.now(UTC))
        assert done.value.code == "VOICE_NOT_PENDING"
        db.rollback()


def test_a_deleted_department_or_inactive_sites_are_not_asked_about(client, company_headers):
    """Solo se pregunta por datos vigentes: un departamento en «Eliminados» o un turno cuyos sitios se desactivaron no
    dan preguntas (nunca se pregunta por lo que la persona ya no tiene)."""
    from app.core.clock import business_today
    from app.models import Employee
    from tests.test_shifts import assign, create_shift, create_site

    department = client.post("/api/departments", json={"name": "Temporal"}, headers=company_headers).json()["data"]
    site = create_site(client, company_headers, name="Bodega")
    shift = create_shift(client, company_headers, name="Nocturno", sites=[site["id"]])
    headers = _employee(client, company_headers)
    me = client.get("/api/users/me", headers=headers).json()["data"]["employee"]
    assert assign(client, company_headers, me["id"], shift["id"], business_today()).status_code == 201
    with platform_session() as db:
        employee = db.get(Employee, me["id"])
        assert employee is not None
        employee.department_id = department["id"]
        db.commit()
    with platform_session() as db:
        from app.models import Department

        gone = db.get(Department, department["id"])
        assert gone is not None
        gone.deleted_at, gone.deleted_by = datetime.now(UTC), "rh@empresa.com"  # a «Eliminados»
        db.commit()
    deactivated = client.patch(f"/api/sites/{site['id']}/status", json={"active": False}, headers=company_headers)
    assert deactivated.status_code == 200, deactivated.text
    with platform_session() as db:
        employee = db.get(Employee, me["id"])
        assert employee is not None
        kinds = {option.kind for option in voice_questions.eligible(db, employee, "Panificadora")}
    assert VoiceQuestion.DEPARTMENT not in kinds and VoiceQuestion.WORK_SITE not in kinds
    # Lo demás sigue: los fiables de siempre, la suma y el número de empleado, que sí tiene (apellidos «Pérez» de una
    # palabra: sin primer/segundo apellido).
    assert kinds == ALWAYS_ELIGIBLE | {VoiceQuestion.EMPLOYEE_NUMBER}


def test_the_registered_text_is_suggested_to_the_model_only_for_text_questions(
    client, company_headers, speech, monkeypatch
):
    """Nombre, empresa, departamento y sitio (cualquiera de los del turno) llegan al modelo como vocabulario sugerido;
    la fecha y el número no; y el interruptor lo apaga."""
    from app.services.voice_verification import TEXT_QUESTIONS, suggested_vocabulary

    headers, submitted = _submitted(client, company_headers)
    session = _session(submitted["data"]["voice"]["token"])
    speech.hotwords.clear()
    complete_voice(client, headers, submitted["data"])
    # Solo las preguntas de TEXTO (nombres, empresa, departamento, sitio) sugieren su dato; la fecha y sus partes, el
    # número y la suma no (se oyen bien solas).
    expected = [q.answer.replace(ANY_OF, ", ") if q.kind in TEXT_QUESTIONS else None for q in session.questions]
    assert speech.hotwords == expected
    sites = Expected(VoiceQuestion.WORK_SITE, f"Planta Norte{ANY_OF}Bodega")
    assert suggested_vocabulary(sites) == "Planta Norte, Bodega"
    assert suggested_vocabulary(Expected(VoiceQuestion.SURNAMES, "Pérez López")) == "Pérez López"
    assert suggested_vocabulary(Expected(VoiceQuestion.BIRTH_DATE, "1990-05-15")) is None
    assert suggested_vocabulary(Expected(VoiceQuestion.BIRTH_MONTH, "1990-05-15")) is None
    assert suggested_vocabulary(Expected(VoiceQuestion.ARITHMETIC_SUM, "11")) is None
    monkeypatch.setattr(settings, "SPEECH_HOTWORDS_ENABLED", False)
    assert suggested_vocabulary(Expected(VoiceQuestion.FULL_NAME, "Juan Pérez")) is None


def test_the_first_and_second_surname_only_when_the_surnames_are_two_words(client, company_headers):
    """Apellidos de dos palabras («Pérez López») dan primer y segundo apellido; uno compuesto («De la Cruz») no se
    parte con certeza, así que esas dos se omiten y queda solo `SURNAMES` (se documenta)."""
    from app.models import Employee

    headers = _employee(client, company_headers)
    me = client.get("/api/users/me", headers=headers).json()["data"]["employee"]
    with platform_session() as db:
        employee = db.get(Employee, me["id"])
        assert employee is not None
        employee.last_name = "Pérez López"
        db.flush()
        two = {option.kind: option.answer for option in voice_questions.eligible(db, employee, "Acme")}
        employee.last_name = "De la Cruz"
        db.flush()
        compound = {option.kind: option.answer for option in voice_questions.eligible(db, employee, "Acme")}
        db.rollback()
    assert two[VoiceQuestion.FIRST_SURNAME] == "Pérez" and two[VoiceQuestion.SECOND_SURNAME] == "López"
    assert two[VoiceQuestion.SURNAMES] == "Pérez López"
    assert VoiceQuestion.FIRST_SURNAME not in compound and VoiceQuestion.SECOND_SURNAME not in compound
    assert compound[VoiceQuestion.SURNAMES] == "De la Cruz"


def test_the_server_renders_each_question_text_with_the_sum_numbers(client, company_headers, monkeypatch):
    """El servidor arma el texto de cada pregunta en el idioma de la petición: la suma con sus números (del catálogo de
    mensajes) y las de identidad del catálogo de la base (`voice_questions`). `client` deja los catálogos cargados."""
    from app.i18n import use_locale
    from app.services.catalog_service import get_catalogs
    from app.services.voice_verification import _prompt

    monkeypatch.setattr(settings, "VOICE_ARITHMETIC_MIN", 7)
    monkeypatch.setattr(settings, "VOICE_ARITHMETIC_MAX", 7)  # números fijos: aserción determinista
    sum_q = voice_questions._arithmetic_sum()
    assert sum_q.answer == "14" and dict(sum_q.render) == {"a": 7, "b": 7}
    with use_locale("es-MX"):
        assert _prompt(get_catalogs(), sum_q) == "¿Cuánto es 7 más 7?"
        assert _prompt(get_catalogs(), Expected(VoiceQuestion.FULL_NAME, "x")) == "¿Cuál es tu nombre completo?"
    with use_locale("en-US"):
        assert _prompt(get_catalogs(), sum_q) == "What is 7 plus 7?"
        assert _prompt(get_catalogs(), Expected(VoiceQuestion.BIRTH_MONTH, "x")) == "In what month were you born?"


def test_the_start_response_carries_the_rendered_text_including_the_sum(client, company_headers, monkeypatch):
    """La respuesta de inicio trae el texto de cada pregunta listo para mostrarse; la suma, con sus números."""
    monkeypatch.setattr(settings, "VOICE_ARITHMETIC_MIN", 3)
    monkeypatch.setattr(settings, "VOICE_ARITHMETIC_MAX", 3)  # la suma es «3 más 3»
    # Más preguntas por sesión que datos elegibles: `choose` las toma TODAS (incluida la suma), así la prueba es
    # determinista (se salta el límite del Field a propósito, como haría un ajuste del `.env`).
    monkeypatch.setattr(settings, "VOICE_QUESTIONS_PER_SESSION", 50)
    _headers, submitted = _submitted(client, company_headers)
    voice = submitted["data"]["voice"]
    by_code = {q["question"]: q["text"] for q in voice["questions"]}
    assert all(text for text in by_code.values())
    assert by_code["ARITHMETIC_SUM"] == "¿Cuánto es 3 más 3?"
    assert by_code["FULL_NAME"] == "¿Cuál es tu nombre completo?"
    assert by_code["BIRTH_MONTH"] == "¿En qué mes naciste?"


def test_an_arithmetic_sum_answer_is_validated_as_the_total(client, company_headers, bucket):
    """La suma se valida como cualquier otra: un total equivocado repite la pregunta; el correcto pasa."""
    headers, submitted = _submitted(client, company_headers)
    token = submitted["data"]["voice"]["token"]
    session = _session(token)
    forced = replace(session, questions=(Expected(VoiceQuestion.ARITHMETIC_SUM, "11"), *session.questions[1:]))
    token = seal(forced)
    wrong = answer_voice(client, headers, token, 0, voice_clip("juan", "cinco"))
    assert wrong.status_code == 422 and wrong.json()["code"] == "ANSWER_MISMATCH"
    token = wrong.json()["errors"][0]["details"]["token"]
    accepted = answer_voice(client, headers, token, 0, voice_clip("juan", "once"))
    assert accepted.status_code == 200 and accepted.json()["data"]["next_position"] == 1
