"""Los tres pasos INDEPENDIENTES del registro facial (decisión del dueño del producto, 2026-10-07; migración 0083).

- Paso 1 (`POST /enrollment/photo`): la foto inicial aceptada queda como borrador (foto cifrada en el bucket, plantilla
  cifrada en la base, vencimiento); repetirla reemplaza el anterior (su objeto a la cola); los rechazos son los de la
  validación previa; sin bucket, 503 y nada a medias.
- Paso 2 (`POST /enrollment/face`): sin foto inicial, 409 ENROLLMENT_PHOTO_REQUIRED (vencida, con su llave); otra
  persona en las capturas, 422 ENROLLMENT_PHOTO_MISMATCH y la foto se conserva; aceptadas, la foto inicial pasa a ser la
  foto de referencia del registro (el mismo objeto) y el borrador sale.
- Paso 3 (`POST /enrollment/voice/start`): la sesión se emite las veces que haga falta conservando las respuestas
  aceptadas (continúa con la pregunta que falta); sin registro pendiente, 409; con los intentos agotados, 422.
- `GET /enrollment/progress`: el estado de los tres pasos en cada situación.
- Depuración (borradores vencidos, registros a medias a las FACE_ENROLLMENT_DRAFT_HOURS), borrado real con la persona,
  presupuestos de consultas y el registro en persona sin cambios.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.core.config import settings
from app.core.crypto import decrypt_bytes
from app.core.database import platform_session
from app.models import EnrollmentVoiceAnswer, FaceEnrollment, FaceEnrollmentDraft, StorageDeletion
from app.services import maintenance_service
from app.services.voice_questions import _open
from tests.conftest import (
    answer_voice,
    create_employee,
    enrollment_challenge,
    initial_photo,
    login,
    submit_enrollment,
    turn_files,
)
from tests.speech_support import voice_clip
from tests.test_performance import count_queries
from tests.test_policy import set_policy


def _employee(client, company_headers, email: str = "juan@empresa.com", number: str = "EMP-001") -> dict[str, str]:
    assert create_employee(client, company_headers, email=email, number=number).status_code == 201
    return login(client, email, "Empleado123")


def _drafts() -> list[FaceEnrollmentDraft]:
    with platform_session() as db:
        rows = list(db.scalars(select(FaceEnrollmentDraft)))
        for row in rows:
            db.expunge(row)
        return rows


def _queued() -> set[str]:
    with platform_session() as db:
        return set(db.scalars(select(StorageDeletion.object_name)))


def progress(client, headers) -> dict:
    response = client.get("/api/enrollment/progress", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def start_voice(client, headers):
    return client.post("/api/enrollment/voice/start", headers=headers)


def _captures(client, headers, person: str = "juan"):
    """Solo el paso 2 (sin la foto inicial): las capturas con el reto del registro."""
    challenge = enrollment_challenge(client, headers)
    files = [("images", (f"f{i}.jpg", f"face:{person}".encode(), "image/jpeg")) for i in range(3)]
    files += turn_files(challenge, person)
    return client.post(
        "/api/enrollment/face", data={"challenge_id": challenge["challenge_id"]}, files=files, headers=headers
    )


# ---------------------------------------------------------------- paso 1: la foto inicial


def test_the_first_photo_is_saved_as_an_encrypted_draft_and_retaking_it_replaces_the_previous_one(
    client, company_headers, bucket
):
    headers = _employee(client, company_headers)
    before = progress(client, headers)
    assert before["photo"] == {"status": "pending", "checked_at": None, "expires_at": None}
    assert before["capture"]["status"] == "locked" and before["voice"]["status"] == "locked"

    taken = initial_photo(client, headers, b"glasses:juan")
    assert taken.status_code == 201, taken.text
    body = taken.json()
    assert body["code"] == "ENROLLMENT_PHOTO_SAVED"
    data = body["data"]
    assert data["accessories"] == ["GLASSES"] and data["quality_score"] == 0.9  # la insignia, sin bloquear
    expires = datetime.fromisoformat(data["expires_at"])
    checked = datetime.fromisoformat(data["checked_at"])
    assert expires - checked == timedelta(hours=settings.FACE_ENROLLMENT_DRAFT_HOURS)
    (draft,) = _drafts()
    # La foto va CIFRADA al bucket (solo su referencia en la base) y la plantilla es un vector cifrado, no una imagen.
    assert draft.photo_object is not None and draft.photo_object.endswith("/face-enrollments/drafts/1.jpg.enc")
    stored, _ = bucket.objects[draft.photo_object]
    assert b"glasses:juan" not in stored and decrypt_bytes(stored) == b"glasses:juan"
    assert decrypt_bytes(draft.template_encrypted) != b"glasses:juan" and draft.dimension > 0
    assert draft.model_name == "fake-model" and draft.photo_size == len(b"glasses:juan")

    after = progress(client, headers)
    assert after["photo"]["status"] == "done" and after["photo"]["expires_at"] == data["expires_at"]
    assert after["capture"]["status"] == "pending" and after["voice"]["status"] == "locked"

    # Repetirla reemplaza el borrador: una sola fila, el objeto anterior a la cola del bucket.
    again = initial_photo(client, headers, b"face:juan")
    assert again.status_code == 201
    (replaced,) = _drafts()
    assert replaced.id != draft.id and replaced.photo_object != draft.photo_object
    assert draft.photo_object in _queued() and replaced.photo_object not in _queued()


def test_the_first_photo_is_validated_like_the_precheck(client, company_headers):
    headers = _employee(client, company_headers)
    blurry = initial_photo(client, headers, b"noface")
    assert blurry.status_code == 422 and blurry.json()["code"] == "NO_FACE"
    masked = initial_photo(client, headers, b"mask:juan")
    assert masked.status_code == 422 and masked.json()["code"] == "ACCESSORIES_DETECTED"
    assert masked.json()["errors"][0]["details"]["accessories"] == ["MASK"]
    assert _drafts() == []  # nada se guarda de una foto rechazada
    # Con varias capturas se guarda la de mejor calidad.
    files = [("images", (f"p{i}.jpg", image, "image/jpeg")) for i, image in enumerate((b"dim:juan", b"face:juan"))]
    accepted = client.post("/api/enrollment/photo", files=files, headers=headers)
    assert accepted.status_code == 201 and accepted.json()["data"]["quality_score"] == 0.9
    (draft,) = _drafts()
    assert draft.quality_score == 0.9 and draft.photo_size == len(b"face:juan")
    too_many = client.post(
        "/api/enrollment/photo",
        files=[("images", (f"p{i}.jpg", b"face:juan", "image/jpeg")) for i in range(4)],
        headers=headers,
    )
    assert too_many.status_code == 422 and too_many.json()["code"] == "TOO_MANY_IMAGES"


def test_the_first_photo_needs_an_employee_who_can_still_enroll(client, company_headers):
    headers = _employee(client, company_headers)
    assert submit_enrollment(client, headers).status_code == 201
    pending = initial_photo(client, headers)
    assert pending.status_code == 409 and pending.json()["code"] == "ENROLLMENT_PENDING"
    refused = client.post(
        "/api/enrollment/photo", files={"images": ("p.jpg", b"face:x", "image/jpeg")}, headers=company_headers
    )
    assert refused.status_code == 403
    assert client.get("/api/enrollment/progress", headers=company_headers).status_code == 403
    assert start_voice(client, company_headers).status_code == 403


def test_with_the_bucket_down_the_first_photo_is_not_saved(client, company_headers, bucket):
    headers = _employee(client, company_headers)
    bucket.down = {"put"}
    failed = initial_photo(client, headers)
    assert (
        failed.status_code == 503 and failed.json()["code"] == "STORAGE_UNAVAILABLE" and failed.headers["Retry-After"]
    )
    assert _drafts() == [] and bucket.objects == {}
    assert progress(client, headers)["photo"]["status"] == "pending"


# ---------------------------------------------------------------- paso 2: las capturas exigen la foto inicial


def test_the_captures_need_a_valid_first_photo_and_consume_it(client, company_headers, bucket):
    headers = _employee(client, company_headers)
    missing = _captures(client, headers)
    assert missing.status_code == 409 and missing.json()["code"] == "ENROLLMENT_PHOTO_REQUIRED"
    assert missing.json()["message"] == "Primero toma tu foto inicial"
    assert initial_photo(client, headers).status_code == 201
    (draft,) = _drafts()
    # Vencida: el mismo código con su propia explicación, y el índice la muestra vencida.
    with platform_session() as db:
        row = db.get(FaceEnrollmentDraft, draft.id)
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        db.commit()
    expired = _captures(client, headers)
    assert expired.status_code == 409 and expired.json()["code"] == "ENROLLMENT_PHOTO_REQUIRED"
    assert expired.json()["message"] == "Tu foto inicial venció. Tómala de nuevo."
    state = progress(client, headers)
    assert state["photo"]["status"] == "expired" and state["capture"]["status"] == "locked"
    # Vigente otra vez: las capturas pasan y la foto inicial PASA A SER la foto de referencia del registro (el mismo
    # objeto cifrado, sin volver a subirlo ni mandarlo a la cola); la fila del borrador sale.
    assert initial_photo(client, headers).status_code == 201
    (draft,) = _drafts()
    puts = [call for call in bucket.calls if call[0] == "put"]
    accepted = _captures(client, headers)
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["code"] == "ENROLLMENT_PHOTOS_ACCEPTED" and accepted.json()["data"]["voice"]["answered"] == 0
    assert _drafts() == [] and draft.photo_object not in _queued()
    assert [call for call in bucket.calls if call[0] == "put"] == puts  # el paso 2 no sube nada
    with platform_session() as db:
        (enrollment,) = db.scalars(select(FaceEnrollment))
        assert (enrollment.photo_object, enrollment.photo_sha256) == (draft.photo_object, draft.photo_sha256)
        assert enrollment.photo_size == draft.photo_size and enrollment.photo_content_type == "image/jpeg"
    detail = client.get(f"/api/enrollments/{enrollment.id}", headers=company_headers)  # aún espera su video: 404
    assert detail.status_code == 404
    state = progress(client, headers)
    assert state["photo"]["status"] == "done" and state["capture"]["status"] == "done"
    assert state["voice"] == {"status": "pending", "answered": 0, "total": 3, "attempts_left": 9}


def test_another_person_in_the_captures_is_refused_and_the_first_photo_stays(client, company_headers):
    headers = _employee(client, company_headers)
    assert initial_photo(client, headers, b"face:ana").status_code == 201
    refused = _captures(client, headers, person="juan")
    assert refused.status_code == 422 and refused.json()["code"] == "ENROLLMENT_PHOTO_MISMATCH"
    assert len(_drafts()) == 1  # la foto inicial sigue: se repiten las capturas (o la foto)
    with platform_session() as db:
        assert db.scalar(select(func.count()).select_from(FaceEnrollment)) == 0
    assert _captures(client, headers, person="ana").status_code == 201


def test_a_first_photo_replaced_while_the_captures_are_analyzed_saves_nothing(client, company_headers, monkeypatch):
    """«Repetir foto» en otra pestaña mientras se analizan las capturas: el registro no toma un borrador que ya no
    existe (su objeto iría a la cola): 409 y nada se guarda."""
    from app.repositories.enrollment_draft_repository import FaceEnrollmentDraftRepository

    headers = _employee(client, company_headers)
    assert initial_photo(client, headers).status_code == 201
    monkeypatch.setattr(FaceEnrollmentDraftRepository, "take", lambda self, draft_id: None)
    raced = _captures(client, headers)
    assert raced.status_code == 409 and raced.json()["code"] == "ENROLLMENT_PHOTO_REQUIRED"
    with platform_session() as db:
        assert db.scalar(select(func.count()).select_from(FaceEnrollment)) == 0


def test_a_first_photo_of_another_face_engine_or_unreadable_asks_for_a_new_one(client, company_headers, monkeypatch):
    headers = _employee(client, company_headers)
    assert initial_photo(client, headers).status_code == 201
    with platform_session() as db:
        (draft,) = db.scalars(select(FaceEnrollmentDraft))
        draft.model_name = "older-model"
        db.commit()
    outdated = _captures(client, headers)
    assert outdated.status_code == 409 and outdated.json()["code"] == "ENROLLMENT_PHOTO_REQUIRED"
    assert initial_photo(client, headers).status_code == 201
    with platform_session() as db:
        (draft,) = db.scalars(select(FaceEnrollmentDraft))
        draft.template_encrypted = b"basura"
        db.commit()
    unreadable = _captures(client, headers)
    assert unreadable.status_code == 409 and unreadable.json()["code"] == "ENROLLMENT_PHOTO_REQUIRED"


def test_in_person_enrollment_needs_no_first_photo(client, company_headers):
    employee = create_employee(client, company_headers).json()["data"]
    challenge = enrollment_challenge(client, company_headers)
    files = [("images", (f"f{i}.jpg", b"face:juan", "image/jpeg")) for i in range(3)] + turn_files(challenge)
    enrolled = client.post(
        f"/api/employees/{employee['id']}/face/enroll",
        data={"challenge_id": challenge["challenge_id"]},
        files=files,
        headers=company_headers,
    )
    assert enrolled.status_code == 201 and enrolled.json()["data"]["face_status"] == "APPROVED"
    assert _drafts() == []


# ---------------------------------------------------------------- paso 3: la sesión de voz se retoma


def test_the_voice_session_resumes_with_the_missing_question(client, company_headers):
    headers = _employee(client, company_headers)
    none_pending = start_voice(client, headers)
    assert none_pending.status_code == 409 and none_pending.json()["code"] == "VOICE_NOT_PENDING"
    submitted = submit_enrollment(client, headers, voice=False).json()["data"]
    first = start_voice(client, headers)
    assert first.status_code == 200 and first.json()["code"] == "VOICE_SESSION_STARTED"
    session = first.json()["data"]
    assert [q["position"] for q in session["questions"]] == [0, 1, 2]
    assert session["total"] == 3 and session["answered"] == 0 and session["expires_in"] == 900
    # La sesión que llegó con las fotos (compatibilidad) sigue sirviendo igual.
    assert submitted["voice"]["total"] == 3 and len(submitted["voice"]["questions"]) == 3

    # Una respuesta aceptada; la persona cierra la app y vuelve: la sesión nueva sigue con las dos que faltan.
    truth = _open(session["token"]).questions[0].answer.split("\n")[0]
    accepted = answer_voice(client, headers, session["token"], 0, voice_clip("juan", truth))
    assert accepted.status_code == 200 and accepted.json()["data"]["next_position"] == 1
    state = progress(client, headers)["voice"]
    assert state == {"status": "pending", "answered": 1, "total": 3, "attempts_left": 9}
    resumed = start_voice(client, headers).json()["data"]
    assert [q["position"] for q in resumed["questions"]] == [1, 2] and resumed["answered"] == 1
    inner = _open(resumed["token"])
    assert inner is not None and inner.passed == (True, False, False) and inner.tries == (1, 0, 0)
    assert inner.questions[0].kind.value == session["questions"][0]["question"]  # la misma pregunta, en su lugar
    assert {q.kind for q in inner.questions[1:]} & {inner.questions[0].kind} == set()  # las nuevas, de otros datos
    # La aceptada no se repite y las que faltan se responden con la sesión nueva hasta terminar.
    repeated = answer_voice(client, headers, resumed["token"], 0, voice_clip("juan", truth))
    assert repeated.status_code == 409 and repeated.json()["code"] == "ANSWER_ALREADY_ACCEPTED"
    token = resumed["token"]
    for question in resumed["questions"]:
        expected = _open(token).questions[question["position"]].answer.split("\n")[0]
        response = answer_voice(client, headers, token, question["position"], voice_clip("juan", expected))
        assert response.status_code == 200, response.text
        token = response.json()["data"]["token"]
    assert response.json()["data"]["done"] is True
    me = client.get("/api/users/me", headers=headers).json()["data"]
    assert me["employee"]["face_status"] == "PENDING_REVIEW"
    done = progress(client, headers)
    assert (
        done["voice"]["status"] == "done" and done["capture"]["status"] == "done" and done["photo"]["status"] == "done"
    )
    assert start_voice(client, headers).status_code == 409
    with platform_session() as db:
        assert db.scalar(select(func.count()).select_from(EnrollmentVoiceAnswer)) == 3


def test_resuming_reuses_data_when_no_other_question_is_left(client, company_headers, monkeypatch):
    """Una empresa que pide más preguntas que datos elegibles tiene el empleado: la sesión completa son los elegibles
    y, al retomar, las que faltan salen de los datos que quedan (si ninguno queda, de cualquiera)."""
    monkeypatch.setattr(settings, "VOICE_QUESTIONS_PER_SESSION", 50)
    headers = _employee(client, company_headers, number="")
    submit_enrollment(client, headers, voice=False)
    session = start_voice(client, headers).json()["data"]
    # Pide 50 pero el empleado solo tiene nueve datos fiables: nombre, apellidos, nombre completo, fecha de nacimiento y
    # sus tres partes (mes, año, día), empresa y la suma; sin número, departamento ni sitio (y «Pérez» no se parte en
    # dos apellidos). La sesión completa son esos nueve.
    assert session["total"] == 9
    token = session["token"]
    for position in (0, 1):
        truth = _open(token).questions[position].answer.split("\n")[0]
        token = answer_voice(client, headers, token, position, voice_clip("juan", truth)).json()["data"]["token"]
    resumed = start_voice(client, headers).json()["data"]
    assert resumed["answered"] == 2 and resumed["total"] == 9
    assert [q["position"] for q in resumed["questions"]] == [2, 3, 4, 5, 6, 7, 8]


def test_when_the_tries_run_out_the_steps_start_over(client, company_headers, bucket):
    headers = _employee(client, company_headers)
    submit_enrollment(client, headers, voice=False)
    session = start_voice(client, headers).json()["data"]
    with platform_session() as db:
        (enrollment,) = db.scalars(select(FaceEnrollment))
        enrollment.voice_attempts = settings.VOICE_MAX_RETRIES_PER_QUESTION * 3
        db.commit()
    exhausted = start_voice(client, headers)
    assert exhausted.status_code == 422 and exhausted.json()["code"] == "VOICE_RETRIES_EXHAUSTED"
    assert "Repite la foto inicial y las capturas" in exhausted.json()["message"]
    state = progress(client, headers)
    assert state["voice"] == {"status": "exhausted", "answered": 0, "total": 3, "attempts_left": 0}
    assert state["photo"]["status"] == "pending" and state["capture"]["status"] == "locked"
    # Los pasos 1 y 2 de nuevo reemplazan el registro agotado (su foto a la cola) y abren otra sesión.
    assert submit_enrollment(client, headers, voice=False).status_code == 201
    fresh = start_voice(client, headers).json()["data"]
    assert fresh["answered"] == 0 and fresh["token"] != session["token"]
    with platform_session() as db:
        assert db.scalar(select(func.count()).select_from(FaceEnrollment)) == 1


# ---------------------------------------------------------------- el índice


def test_progress_follows_the_policy_and_the_finished_states(client, company_headers):
    headers = _employee(client, company_headers)
    set_policy(client, company_headers, voice_verification=False)
    assert progress(client, headers)["voice"]["status"] == "not_required"
    accepted = submit_enrollment(client, headers)
    assert accepted.status_code == 201 and accepted.json()["data"]["voice"] is None
    done = progress(client, headers)
    assert done["face_status"] == "PENDING_REVIEW" and done["capture"]["status"] == "done"
    assert done["voice"]["status"] == "not_required" and done["photo"]["checked_at"] == done["capture"]["submitted_at"]
    enrollment_id = accepted.json()["data"]["enrollment_id"]
    assert client.post(f"/api/enrollments/{enrollment_id}/approve", headers=company_headers).status_code == 200
    approved = progress(client, headers)
    assert approved["face_status"] == "APPROVED" and approved["capture"]["status"] == "done"
    # Rechazado: todo pendiente otra vez (el registro rechazado no cuenta).
    assert create_employee(client, company_headers, email="ana@empresa.com", number="EMP-002").status_code == 201
    ana = login(client, "ana@empresa.com", "Empleado123")
    rejected_id = submit_enrollment(client, ana).json()["data"]["enrollment_id"]
    reject = client.post(f"/api/enrollments/{rejected_id}/reject", json={"reason": "Borrosa"}, headers=company_headers)
    assert reject.status_code == 200
    state = progress(client, ana)
    assert state["face_status"] == "REJECTED" and state["photo"]["status"] == "pending"
    assert state["capture"]["status"] == "locked" and state["voice"]["status"] == "not_required"


# ---------------------------------------------------------------- depuración, borrado real y presupuestos


def test_expired_drafts_and_stale_unfinished_enrollments_are_purged_with_their_objects(client, company_headers, bucket):
    headers = _employee(client, company_headers)
    assert initial_photo(client, headers).status_code == 201
    other = _employee(client, company_headers, email="ana@empresa.com", number="EMP-002")
    ana = (b"face:ana",) * 3
    assert submit_enrollment(client, other, frontal=ana, turn_person="ana", voice=False).status_code == 201
    stale = datetime.now(UTC) - timedelta(hours=settings.FACE_ENROLLMENT_DRAFT_HOURS + 1)
    with platform_session() as db:
        (draft,) = db.scalars(select(FaceEnrollmentDraft))
        draft.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        (enrollment,) = db.scalars(select(FaceEnrollment))
        enrollment.submitted_at = stale
        db.commit()
    # La foto inicial de juan (vigente) y la de ana, que ya es la foto de referencia de su registro a medias.
    assert len(bucket.objects) == 2
    with platform_session() as db:
        purged = maintenance_service.purge_expired(db)
    assert purged["fotos iniciales del registro facial vencidas"] == 1
    assert purged["registros faciales sin terminar"] == 1
    assert _drafts() == [] and bucket.objects == {}  # la misma vuelta vació la cola
    assert progress(client, headers)["photo"]["status"] == "pending"
    # Un registro a medias más reciente que FACE_ENROLLMENT_DRAFT_HOURS se queda: la persona vuelve otro día.
    submit_enrollment(client, headers, voice=False)
    with platform_session() as db:
        (enrollment,) = db.scalars(select(FaceEnrollment))
        enrollment.submitted_at = datetime.now(UTC) - timedelta(hours=settings.FACE_ENROLLMENT_DRAFT_HOURS - 1)
        db.commit()
        assert maintenance_service.purge_expired(db)["registros faciales sin terminar"] == 0


def test_deleting_the_employee_erases_the_draft_for_real(client, company_headers, bucket):
    headers = _employee(client, company_headers)
    assert initial_photo(client, headers).status_code == 201
    employee_id = client.get("/api/users/me", headers=headers).json()["data"]["employee"]["id"]
    (draft,) = _drafts()
    assert client.delete(f"/api/employees/{employee_id}", headers=company_headers).status_code == 200
    assert _drafts() == [] and draft.photo_object in _queued()


def test_the_steps_fit_their_query_budgets(client, company_headers):
    from tests.test_performance import BUDGETS

    headers = _employee(client, company_headers)
    progress(client, headers)  # calienta política y catálogos
    with count_queries() as photo_statements:
        assert initial_photo(client, headers).status_code == 201
    with count_queries() as progress_statements:
        progress(client, headers)
    submit_enrollment(client, headers, voice=False)
    with count_queries() as start_statements:
        assert start_voice(client, headers).status_code == 200
    with count_queries() as pending_statements:
        progress(client, headers)
    assert len(photo_statements) <= BUDGETS["POST /api/enrollment/photo"], photo_statements
    assert len(progress_statements) <= BUDGETS["GET /api/enrollment/progress"], progress_statements
    assert len(pending_statements) <= BUDGETS["GET /api/enrollment/progress"], pending_statements
    assert len(start_statements) <= BUDGETS["POST /api/enrollment/voice/start"], start_statements
