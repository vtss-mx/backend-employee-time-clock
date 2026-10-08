"""Reconocimiento facial evolutivo: la galería de cada empleado aprende de sus identificaciones
seguras sin desviarse de su registro aprobado (app/services/face_learning.py).

Con la confianza por defecto (99.999 %) la empresa exige similitud 0.653 y aprender exige 0.703
(holgura de 0.05). "juan~0.80~luz" es una captura de juan con similitud exacta 0.80 a su registro.
"""

import pytest

from app.core.config import settings
from app.core.crypto import decrypt_bytes
from app.core.database import SessionLocal
from app.facial_recognition import matcher
from app.facial_recognition.matcher import embedding_from_bytes
from app.models import Employee, FaceEmbedding
from app.services import face_service
from app.services.face_gallery import face_galleries
from app.services.face_learning import Evidence, FaceLearning
from app.services.policy_service import PolicySnapshot
from tests.conftest import FakePipeline, _analysis, approved_employee, face_vector, login, turn_files
from tests.test_policy import admin_company, set_policy
from tests.test_validators import approved, identify_face, validator_headers


@pytest.fixture(autouse=True)
def _fresh_gallery():
    face_galleries.clear()  # cada prueba usa una BD nueva
    yield


def verify(client, headers, person: str, *, live: bool = True):
    """El empleado se verifica con dos capturas de `person` (con o sin el reto de prueba de vida)."""
    files = [("images", (f"c{i}.jpg", f"face:{person}".encode(), "image/jpeg")) for i in range(2)]
    data = {}
    if live:
        challenge = client.post("/api/face/challenge", headers=headers).json()["data"]
        files += turn_files(challenge, person)
        data = {"challenge_id": challenge["challenge_id"]}
    response = client.post("/api/verification/face", data=data, files=files, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def samples(employee_id: int) -> list[FaceEmbedding]:
    with SessionLocal() as db:
        return db.query(FaceEmbedding).filter_by(employee_id=employee_id).order_by(FaceEmbedding.id).all()


def learned(employee_id: int) -> list[FaceEmbedding]:
    return [row for row in samples(employee_id) if row.learned]


def vector(row: FaceEmbedding):
    return embedding_from_bytes(decrypt_bytes(row.embedding_encrypted), row.dimension)


def employee_id_of(client, company_headers) -> int:
    return client.get("/api/employees", headers=company_headers).json()["data"]["items"][0]["id"]


@pytest.fixture
def no_pause(monkeypatch):
    """Sin la pausa entre muestras aprendidas (para enseñar varias en una prueba)."""
    monkeypatch.setattr(settings, "FACE_LEARNING_INTERVAL_HOURS", 0.0)


def test_safe_verification_teaches_the_gallery_and_keeps_the_anchor(client, company_headers):
    headers = approved_employee(client, company_headers)
    juan = employee_id_of(client, company_headers)
    anchors = [row.id for row in samples(juan)]

    assert verify(client, headers, "juan~0.80~luz")["verified"] is True
    taught = learned(juan)
    assert len(taught) == 1 and taught[0].enrollment_id is None
    assert abs(float(vector(taught[0]) @ face_vector("juan~0.80~luz")) - 1) < 1e-5
    # El ancla decidió la verificación (suma utilidad) y sigue intacta.
    rows = samples(juan)
    assert [row.id for row in rows if not row.learned] == anchors
    assert sum(row.matches for row in rows) == 1 and rows[0].last_matched_at is not None

    # Como máximo una muestra por lapso (FACE_LEARNING_INTERVAL_HOURS): variedad de días y luz.
    assert verify(client, headers, "juan~0.80~sombra")["verified"] is True
    assert len(learned(juan)) == 1

    employee = client.get(f"/api/employees/{juan}", headers=company_headers).json()["data"]
    assert employee["face_samples"] == 4
    base, admin = admin_company(client, company_headers)
    listed = client.get(f"{base}/employees", headers=admin).json()["data"]["items"][0]
    assert listed["face_learned_samples"] == 1 and listed["face_last_learned_at"] is not None


def test_gallery_improves_with_use_without_drifting(client, company_headers, no_pause):
    headers = approved_employee(client, company_headers)
    juan = employee_id_of(client, company_headers)

    # Antes de aprender: una captura con otra luz (0.6375 al registro) no alcanza el 99.999 %.
    assert verify(client, headers, "juan~0.75~luz~0.85~noche")["verified"] is False
    # Holgura: pasó (0.68 ≥ 0.653) pero justa (< 0.703): no se aprende de ella.
    assert verify(client, headers, "juan~0.68~tarde")["verified"] is True
    # Novedad: casi idéntica al registro (0.97): no enseña nada nuevo.
    assert verify(client, headers, "juan~0.97~igual")["verified"] is True
    assert learned(juan) == []

    # Aprende de una identificación holgada con otra luz...
    assert verify(client, headers, "juan~0.75~luz")["verified"] is True
    assert len(learned(juan)) == 1
    # ...y ahora reconoce la captura que antes rechazaba (gracias a la muestra aprendida).
    assert verify(client, headers, "juan~0.75~luz~0.85~noche")["verified"] is True
    # Ancla: esa captura se aleja del registro aprobado (0.6375 < 0.653): no enseña. Una muestra
    # aprendida nunca enseña a otra, así la galería no se desvía poco a poco hacia otra cara.
    taught = learned(juan)
    assert len(taught) == 1 and taught[0].matches == 1


def test_learning_follows_the_company_policy_and_needs_liveness(client, company_headers, no_pause):
    headers = approved_employee(client, company_headers)
    juan = employee_id_of(client, company_headers)

    assert set_policy(client, company_headers, adaptive_learning=False)["adaptive_learning"] is False
    assert verify(client, headers, "juan~0.80~luz")["verified"] is True
    assert learned(juan) == []

    # Sin reto de prueba de vida no se aprende: una foto no debe poder enseñar nada.
    set_policy(client, company_headers, adaptive_learning=True, liveness_challenge=False)
    assert verify(client, headers, "juan~0.80~sombra", live=False)["verified"] is True
    assert learned(juan) == []

    set_policy(client, company_headers, liveness_challenge=True)
    assert verify(client, headers, "juan~0.80~sol")["verified"] is True
    assert len(learned(juan)) == 1


def test_full_gallery_replaces_the_least_useful_learned_sample(client, company_headers, no_pause, monkeypatch):
    monkeypatch.setattr(settings, "FACE_LEARNING_MAX_SAMPLES", 2)
    headers = approved_employee(client, company_headers)
    juan = employee_id_of(client, company_headers)
    anchors = [row.id for row in samples(juan)]

    verify(client, headers, "juan~0.75~a")
    verify(client, headers, "juan~0.75~b")
    first, second = (row.id for row in learned(juan))
    # La muestra "a" vuelve a servir (decide una verificación; casi igual a ella: no se aprende).
    assert verify(client, headers, "juan~0.75~a~0.97~x")["verified"] is True
    verify(client, headers, "juan~0.75~c")

    rows = samples(juan)
    kept = [row.id for row in rows if row.learned]
    # Con los lugares llenos sale "b" (nunca sirvió); "a" sobrevive y entra "c". El ancla no se toca.
    assert first in kept and second not in kept and len(kept) == 2
    newest = next(row for row in rows if row.id == max(kept))
    assert abs(float(vector(newest) @ face_vector("juan~0.75~c")) - 1) < 1e-5
    assert [row.id for row in rows if not row.learned] == anchors


def test_validator_learns_only_from_clear_identifications(client, company_headers):
    juan = approved(client, company_headers, "juan", number="EMP-001")
    approved(client, company_headers, "ana", number="EMP-002")
    headers = validator_headers(client, company_headers, mode="FACE")

    assert identify_face(client, headers, "juan~0.75~luz~0.85~noche").json()["data"]["verified"] is False
    assert identify_face(client, headers, "juan~0.75~luz").json()["data"]["employee_id"] == juan["id"]
    assert len(learned(juan["id"])) == 1
    # La galería en memoria del validador incluye lo aprendido (se actualiza sola).
    after = identify_face(client, headers, "juan~0.75~luz~0.85~noche").json()["data"]
    assert after["verified"] is True and after["employee_id"] == juan["id"]


def test_identification_margin_rule():
    """1:N: si la segunda persona más parecida está cerca, se identifica pero no se aprende."""
    learning = FaceLearning(faces=None, policy=PolicySnapshot())
    assert learning._allowed(Evidence((), live=True, gap=None)) is True
    assert learning._allowed(Evidence((), live=True, gap=0.25)) is True
    assert learning._allowed(Evidence((), live=True, gap=0.07)) is False
    assert learning._allowed(Evidence((), live=False)) is False
    disabled = FaceLearning(faces=None, policy=PolicySnapshot(adaptive_learning=False))
    assert disabled._allowed(Evidence((), live=True)) is False


def test_a_capture_without_a_deciding_sample_credits_none(client, company_headers):
    """Si ninguna muestra decidió la identificación (`closest` = None) no se acredita utilidad a ninguna,
    y la captura igual puede enseñar si cumple las reglas."""
    approved_employee(client, company_headers)
    juan = employee_id_of(client, company_headers)
    with SessionLocal() as db:
        learning = FaceLearning(face_service.FaceService(db, FakePipeline()), PolicySnapshot())
        evidence = Evidence([_analysis("juan~0.80~luz")], live=True)
        assert learning.reinforce(db.get(Employee, juan), None, evidence) is True
        db.commit()
    assert len(learned(juan)) == 1 and sum(row.matches for row in samples(juan)) == 0


def test_only_the_admin_sees_how_the_recognition_evolves(client, company_headers):
    headers = approved_employee(client, company_headers)
    verify(client, headers, "juan~0.80~luz")  # la decide el ancla y enseña una muestra
    base, admin = admin_company(client, company_headers)

    summary = client.get(f"{base}/face-learning", headers=admin)
    assert summary.status_code == 200 and summary.json()["code"] == "FACE_LEARNING_SUMMARY"
    data = summary.json()["data"]
    assert data["enabled"] is True and data["last_learned_at"] is not None
    assert (data["approved_employees"], data["employees_learning"], data["learned_samples"]) == (1, 1, 1)
    assert (data["identifications"], data["learned_identifications"]) == (1, 0)
    # Su ficha en la consola dice cuánto aprendió; la empresa ya no ve nada del aprendizaje.
    listed = client.get(f"{base}/employees", headers=admin).json()["data"]["items"][0]
    assert listed["face_learned_samples"] == 1 and listed["face_last_learned_at"] is not None
    record = client.get(f"/api/employees/{listed['id']}", headers=company_headers).json()["data"]
    assert "face_learned_samples" not in record and "face_last_learned_at" not in record

    set_policy(client, company_headers, adaptive_learning=False)
    base, admin = admin_company(client, company_headers)  # set_policy abrió otra sesión del ADMIN
    assert client.get(f"{base}/face-learning", headers=admin).json()["data"]["enabled"] is False
    assert client.get("/api/admin/companies/999999/face-learning", headers=admin).status_code == 404


def test_the_admin_forgets_what_the_gallery_learned(client, company_headers):
    headers = approved_employee(client, company_headers)
    juan = employee_id_of(client, company_headers)
    verify(client, headers, "juan~0.80~luz")
    assert len(learned(juan)) == 1
    base, admin = admin_company(client, company_headers)

    url = f"{base}/employees/{juan}/face/learned"
    assert client.delete(url, headers=company_headers).status_code == 403  # la empresa no lo administra
    assert client.delete(url, headers=headers).status_code == 403
    missing = client.delete(f"{base}/employees/999999/face/learned", headers=admin)
    assert missing.status_code == 404 and missing.json()["code"] == "EMPLOYEE_NOT_FOUND"
    gone = client.delete(f"/api/admin/companies/999999/employees/{juan}/face/learned", headers=admin)
    assert gone.status_code == 404 and gone.json()["code"] == "COMPANY_NOT_FOUND"
    forgotten = client.delete(url, headers=admin)
    assert forgotten.status_code == 200 and forgotten.json()["code"] == "FACE_LEARNING_FORGOTTEN"
    data = forgotten.json()["data"]
    assert (data["face_learned_samples"], data["face_last_learned_at"], data["face_status"]) == (0, None, "APPROVED")
    assert learned(juan) == [] and len(samples(juan)) == 3
    assert verify(client, headers, "juan")["verified"] is True  # sigue identificándose con su registro


def test_gallery_cache_decrypts_only_new_samples(client, company_headers, monkeypatch):
    juan = approved(client, company_headers, "juan", number="EMP-001")
    approved(client, company_headers, "ana", number="EMP-002")
    headers = login(client, "juan@empresa.com", "Empleado123")
    calls: list[int] = []
    real_decrypt = matcher.try_decrypt  # readable_embedding vive ahora en matcher (sin ciclo servicio ↔ repositorio)
    monkeypatch.setattr(matcher, "try_decrypt", lambda data: calls.append(1) or real_decrypt(data))
    face_galleries.clear()

    def gallery_size() -> tuple[int, int]:
        """(muestras en la galería, cuántas se descifraron para armarla)."""
        before = len(calls)
        with SessionLocal() as db:
            company_id = db.get(Employee, juan["id"]).company_id
            return face_galleries.get(db, company_id, "fake-model").size, len(calls) - before

    assert gallery_size() == (6, 6)
    verify(client, headers, "juan~0.80~luz")  # una muestra aprendida
    assert gallery_size() == (7, 1)  # solo se descifró la nueva
    base, admin = admin_company(client, company_headers)
    client.delete(f"{base}/employees/{juan['id']}/face/learned", headers=admin)
    assert gallery_size() == (6, 0)  # salió sin descifrar nada

    verify(client, headers, "juan~0.80~sol")
    monkeypatch.setattr(settings, "FACE_GALLERY_INCREMENTAL_LIMIT", 0)
    assert gallery_size() == (7, 7)  # demasiadas nuevas: se reconstruye completa
