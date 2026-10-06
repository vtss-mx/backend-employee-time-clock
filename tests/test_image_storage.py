"""Imágenes y archivos: CIFRADOS en el bucket, NUNCA en la base de datos (decisión del dueño del producto).

Toda imagen o archivo que recibe la plataforma se cifra y se sube al bucket durante la petición; la BD
solo guarda su referencia. Sin bucket (o con el bucket caído) la operación falla completa con 503
`STORAGE_UNAVAILABLE` y no queda nada a medias. Lo que se deja de conservar sale del bucket. Ninguna tabla
puede guardar una imagen (el almacenamiento legado se borró en la migración `0053`).

Bucket falso en memoria (`tests/storage_support.py`, el de todas las pruebas; fixture `bucket`); el
cliente real de Google se prueba en `tests/test_gcs_storage.py` contra un servidor falso.
"""

import base64
import hashlib
import logging
import sys
from datetime import UTC, datetime

import pytest
from sqlalchemy import LargeBinary, delete, func, select, update
from sqlalchemy.exc import OperationalError

from app import cli
from app.core.clock import business_today
from app.core.config import Settings, settings
from app.core.crypto import decrypt_bytes, encrypt_bytes
from app.core.database import Base, SessionLocal
from app.core.object_storage import DisabledStorage, StorageUnavailable, use_storage
from app.models import (
    Employee,
    EnrollmentStatus,
    FaceEnrollment,
    FaceStatus,
    Payment,
    StorageDeletion,
    StorageStatus,
)
from app.repositories.storage_repository import ImageRef, StorageRepository
from app.services import image_storage, storage_jobs
from app.services.image_storage import FACE_ENROLLMENT_PHOTOS, PAYMENT_RECEIPTS, VerificationFailed, extension
from app.services.storage_jobs import DELETED
from tests.billing_support import PDF, billing_url, company_with_plan, pay
from tests.conftest import create_employee, login, submit_enrollment
from tests.storage_support import swap_object

DB_DOWN = OperationalError("SELECT", {}, Exception("la BD no respondió"))


def _broken(*_args, **_kwargs):
    raise DB_DOWN


def _run() -> dict[str, int]:
    with SessionLocal() as db:
        return storage_jobs.run(db, datetime.now(UTC))


def _row(enrollment_id: int) -> FaceEnrollment:
    with SessionLocal() as db:
        row = db.get(FaceEnrollment, enrollment_id)
        assert row is not None
        db.expunge(row)
        return row


def _new_employee(client, company_headers, number="EMP-001", email="juan@empresa.com") -> dict[str, str]:
    assert create_employee(client, company_headers, number=number, email=email).status_code == 201
    return login(client, email, "Empleado123")


def _enroll(client, company_headers, **who) -> FaceEnrollment:
    response = submit_enrollment(client, _new_employee(client, company_headers, **who))
    assert response.status_code == 201, response.text
    return _row(response.json()["data"]["enrollment_id"])


def _face_object(enrollment: FaceEnrollment) -> str:
    return (
        f"test/companies/{enrollment.company_id}/employees/{enrollment.employee_id}/face-enrollments/"
        f"{enrollment.id}.jpg.enc"
    )


def _queue() -> set[str]:
    with SessionLocal() as db:
        return set(db.scalars(select(StorageDeletion.object_name)))


def _status(task: str) -> StorageStatus | None:
    with SessionLocal() as db:
        return db.get(StorageStatus, task)


def _count(model) -> int:
    with SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(model)) or 0


# ---------------------------------------------------------------- guardar: directo al bucket, nunca a la BD


def test_a_face_photo_goes_encrypted_to_the_bucket_and_only_its_reference_to_the_database(
    client, company_headers, bucket
):
    enrollment = _enroll(client, company_headers)
    name = _face_object(enrollment)
    data, metadata = bucket.objects[name]
    assert b"face:juan" not in data and decrypt_bytes(data) == b"face:juan"  # el bucket nunca la ve legible
    digest = hashlib.sha256(data).hexdigest()
    assert metadata == {
        "kind": "face-enrollment",
        "company_id": str(enrollment.company_id),
        "content_type": "image/jpeg",
        "sha256": digest,
        "encryption": "fernet",
    }
    assert (enrollment.photo_object, enrollment.photo_size, enrollment.photo_sha256) == (name, 9, digest)
    assert enrollment.photo_uploaded_at is not None
    # Subida "solo si no existe" verificada con lo que reportó el bucket (sin otra llamada).
    assert bucket.calls == [("put", name)]

    detail = client.get(f"/api/enrollments/{enrollment.id}", headers=company_headers).json()["data"]
    assert detail["photo"] == f"data:image/jpeg;base64,{base64.b64encode(b'face:juan').decode()}"
    assert bucket.calls[-1] == ("get", name)


@pytest.mark.parametrize("storage", ["down", "disabled", "tampered"])
def test_without_the_bucket_nothing_is_saved_halfway(client, company_headers, bucket, storage):
    """Bucket caído, sin configurar o que reporta otra cosa: 503 STORAGE_UNAVAILABLE reintentable con el
    sobre de siempre y NADA guardado (ni registro, ni estado del empleado, ni objeto)."""
    headers = _new_employee(client, company_headers)
    if storage == "down":
        bucket.down = {"put"}
    elif storage == "disabled":
        use_storage(DisabledStorage("faltan GCS_BUCKET o GCS_CREDENTIALS_FILE en el .env"))
    else:
        bucket.tamper = True
    response = submit_enrollment(client, headers)
    assert response.status_code == 503 and response.json()["code"] == "STORAGE_UNAVAILABLE"
    assert response.headers["Retry-After"] == "10" and response.json()["traceId"]
    assert _count(FaceEnrollment) == 0
    with SessionLocal() as db:
        assert db.scalar(select(Employee.face_status)) == FaceStatus.NOT_ENROLLED
    assert bucket.objects == {}  # lo que alcanzó a subirse (y no se verificó) se borró


def test_an_upload_whose_transaction_fails_is_removed_from_the_bucket(client, company_headers, bucket, monkeypatch):
    from app.services.face_service import FaceService

    headers = _new_employee(client, company_headers)
    monkeypatch.setattr(FaceService, "store", _broken)  # la BD falla DESPUÉS de subir la foto
    response = submit_enrollment(client, headers)
    assert response.status_code == 503 and response.json()["code"] == "DATABASE_UNAVAILABLE"
    assert _count(FaceEnrollment) == 0 and bucket.objects == {}
    assert [operation for operation, _ in bucket.calls] == ["put", "delete"]

    bucket.calls.clear()
    bucket.down = {"delete"}  # el bucket tampoco deja borrar: el objeto queda en la cola del mantenimiento
    assert submit_enrollment(client, headers).status_code == 503
    (name,) = bucket.objects
    assert _queue() == {name}
    bucket.down = set()
    assert _run() == {DELETED: 1} and bucket.objects == {} and _queue() == set()


def test_an_orphan_that_cannot_be_deleted_nor_queued_is_logged(monkeypatch, bucket, caplog):
    bucket.down = {"delete"}
    monkeypatch.setattr(StorageRepository, "enqueue", _broken)
    with caplog.at_level(logging.ERROR):
        image_storage._discard_uploads([(bucket, "test/huerfano.enc")])
    assert "No se pudo borrar ni encolar el objeto test/huerfano.enc" in caplog.text


def test_a_savepoint_rollback_keeps_the_uploads_of_its_transaction(bucket):
    """Solo la transacción raíz decide: terminar un savepoint no borra lo que subió la transacción."""

    class Savepoint:
        parent = object()

    bucket.objects["test/en-curso.enc"] = (b"x", {})
    with SessionLocal() as db:
        db.info[image_storage._UPLOADED] = [(bucket, "test/en-curso.enc")]
        image_storage._ended(db, Savepoint())  # type: ignore[arg-type]
        assert db.info[image_storage._UPLOADED] and "test/en-curso.enc" in bucket.objects
        db.execute(select(1))
        db.commit()
    assert "test/en-curso.enc" in bucket.objects  # confirmada: el objeto tiene dueño


def test_a_repeated_upload_is_accepted_only_if_identical(bucket):
    ref = ImageRef(key=7, company_id=3, content_type="image/png", extra=(5,))
    payload = encrypt_bytes(b"foto")
    first = image_storage.upload(bucket, FACE_ENROLLMENT_PHOTOS, ref, payload)
    # La respuesta se perdió y se repitió: el objeto ya está y es idéntico (nunca se reemplaza).
    assert image_storage.upload(bucket, FACE_ENROLLMENT_PHOTOS, ref, payload) == first
    assert first[0] == "test/companies/3/employees/5/face-enrollments/7.png.enc"
    with pytest.raises(VerificationFailed):  # otro contenido con el mismo nombre: no se acepta
        image_storage.upload(bucket, FACE_ENROLLMENT_PHOTOS, ref, encrypt_bytes(b"otra"))
    bucket.down = {"stat"}  # al verificar el que ya estaba, el bucket no responde: no se da por subido
    with pytest.raises(StorageUnavailable):
        image_storage.upload(bucket, FACE_ENROLLMENT_PHOTOS, ref, payload)
    bucket.down = set()
    del bucket.objects[first[0]]
    bucket.objects[first[0]] = (payload, {})  # el que estaba no trae su SHA-256: no se acepta
    with pytest.raises(VerificationFailed):
        image_storage.upload(bucket, FACE_ENROLLMENT_PHOTOS, ref, payload)


def test_payment_receipts_go_encrypted_to_the_bucket(client, admin_headers, bucket):
    company_id = company_with_plan(client, admin_headers)
    files = {"receipt": ("r.pdf", PDF, "application/pdf")}
    paid = pay(client, admin_headers, company_id, "100.00", business_today(), files=files)
    assert paid.status_code == 201, paid.text
    payment_id = paid.json()["data"]["payment"]["id"]
    name = f"test/companies/{company_id}/billing/payments/{payment_id}/receipt.pdf.enc"
    data, metadata = bucket.objects[name]
    assert PDF not in data and decrypt_bytes(data) == PDF
    assert metadata["kind"] == "payment-receipt" and metadata["content_type"] == "application/pdf"
    with SessionLocal() as db:
        payment = db.get(Payment, payment_id)
        assert payment is not None and payment.receipt_object == name
        assert payment.receipt_sha256 == hashlib.sha256(data).hexdigest() and payment.receipt_size == len(PDF)

    url = billing_url(company_id, f"/payments/{payment_id}/receipt")
    assert base64.b64decode(client.get(url, headers=admin_headers).json()["data"]["data"]) == PDF
    bucket.down = {"get"}
    down = client.get(url, headers=admin_headers)
    assert down.status_code == 503 and down.json()["code"] == "STORAGE_UNAVAILABLE"
    bucket.down = set()
    bucket.objects[name] = (b"otro-contenido", {})  # no es el que se guardó (SHA-256 distinto)
    assert client.get(url, headers=admin_headers).json()["code"] == "STORAGE_UNAVAILABLE"
    with SessionLocal() as db:  # el SHA-256 coincide pero no se puede descifrar (otra llave)
        db.execute(update(Payment).values(receipt_sha256=swap_object(bucket, name)))
        db.commit()
    assert client.get(url, headers=admin_headers).json()["code"] == "STORAGE_UNAVAILABLE"


def test_a_payment_is_not_registered_if_its_receipt_cannot_be_saved(client, admin_headers, bucket):
    company_id = company_with_plan(client, admin_headers)
    bucket.down = {"put"}
    files = {"receipt": ("r.pdf", PDF, "application/pdf")}
    response = pay(client, admin_headers, company_id, "100.00", business_today(), files=files)
    assert response.status_code == 503 and response.json()["code"] == "STORAGE_UNAVAILABLE"
    assert _count(Payment) == 0  # ni el pago ni su aplicación: nada a medias


def test_no_table_can_hold_an_image():
    """Regla del dueño del producto: ninguna imagen ni archivo en la BD. La única columna binaria es la
    plantilla facial cifrada (un vector de números, no una imagen): una columna binaria nueva hace fallar
    esta prueba y obliga a pasar por `STORED_IMAGES`."""
    binary = {
        f"{table.fullname}.{column.name}"
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, LargeBinary)
    }
    # También el embedding CIFRADO de las capturas recientes (anti-reenvío perceptual, 30 días): un vector, no una
    # imagen (migración 0062).
    assert binary == {
        "biometrics.face_embeddings.embedding_encrypted",
        "biometrics.capture_traces.embedding_encrypted",
    }


# ---------------------------------------------------------------- leer: del bucket


def test_the_review_photo_degrades_when_the_bucket_is_down(client, company_headers, bucket, caplog):
    enrollment = _enroll(client, company_headers)
    bucket.down = {"get"}
    with caplog.at_level(logging.ERROR):
        detail = client.get(f"/api/enrollments/{enrollment.id}", headers=company_headers)
    assert detail.status_code == 200 and detail.json()["data"]["photo"] is None  # el revisor sigue trabajando
    assert "No se pudo leer del bucket la foto" in caplog.text


def test_a_face_migration_reads_the_approved_photo_from_the_bucket(client, company_headers, bucket, caplog):
    from app.models import FaceEmbedding
    from app.services import face_service
    from tests.conftest import FakePipeline

    enrollment = _enroll(client, company_headers)
    assert client.post(f"/api/enrollments/{enrollment.id}/approve", headers=company_headers).status_code == 200

    def references(employee_id: int) -> list:
        with SessionLocal() as db:  # cambió el motor: las muestras se generan desde la foto aprobada
            db.execute(delete(FaceEmbedding))
            db.commit()
            return face_service.FaceService(db, FakePipeline()).references_for(db.get(Employee, employee_id))

    assert len(references(enrollment.employee_id)) == 1
    bucket.down = {"get"}
    with caplog.at_level(logging.ERROR):
        assert references(enrollment.employee_id) == []
    assert "No se pudo leer del bucket la foto aprobada" in caplog.text
    assert face_service.migration_blocked(enrollment.employee_id)  # no se reintenta en cada identificación

    bucket.down = set()
    pending = _enroll(client, company_headers, number="EMP-002", email="ana@empresa.com")
    calls = len(bucket.calls)
    assert references(pending.employee_id) == []  # un registro sin aprobar no sirve para migrar
    assert len(bucket.calls) == calls


def test_an_object_that_vanishes_while_verifying_is_not_accepted(bucket):
    class Vanishing(type(bucket)):
        def put(self, name, data, metadata, *, interactive=False):
            raise image_storage.ObjectExists(name)

    ref = ImageRef(key=1, company_id=1, content_type=None, extra=(1,))
    with pytest.raises(VerificationFailed):
        image_storage.upload(Vanishing(), FACE_ENROLLMENT_PHOTOS, ref, b"x")


def test_forget_rules(bucket):
    """Una fila sin objeto solo pierde su referencia (nada que encolar); una con objeto lo encola."""
    with SessionLocal() as db:
        row = FaceEnrollment(photo_object=None, photo_size=9)
        image_storage.forget(db, FACE_ENROLLMENT_PHOTOS, row)
        assert row.photo_size is None and _queue() == set()
        payment = Payment(receipt_object="test/r.enc", receipt_sha256="x", receipt_size=3)
        image_storage.forget(db, PAYMENT_RECEIPTS, payment)
        db.commit()
    assert (payment.receipt_object, payment.receipt_size) == (None, None) and _queue() == {"test/r.enc"}


def test_read_rules(bucket):
    row = FaceEnrollment(photo_object=None, photo_sha256=None, status=EnrollmentStatus.APPROVED)
    assert image_storage.read(FACE_ENROLLMENT_PHOTOS, row) is None  # sin imagen: ni siquiera va al bucket
    assert image_storage.read(PAYMENT_RECEIPTS, Payment(receipt_object=None)) is None and bucket.calls == []
    assert extension("image/png") == "png" and extension(None) == "bin" and extension("text/plain") == "bin"


# ---------------------------------------------------------------- lo que se deja de conservar sale del bucket


def test_a_rejected_photo_leaves_the_bucket(client, company_headers, bucket):
    enrollment = _enroll(client, company_headers)
    name = _face_object(enrollment)
    rejected = client.post(
        f"/api/enrollments/{enrollment.id}/reject", json={"reason": "No es el empleado"}, headers=company_headers
    )
    assert rejected.status_code == 200 and rejected.json()["data"]["photo"] is None
    row = _row(enrollment.id)
    assert (row.photo_object, row.photo_size, row.photo_sha256, row.photo_uploaded_at) == (None, None, None, None)
    assert _queue() == {name}  # encolado en la misma transacción del rechazo
    assert _run() == {DELETED: 1} and bucket.objects == {} and _queue() == set()


def test_requesting_a_new_enrollment_removes_pending_photos(client, company_headers, bucket):
    enrollment = _enroll(client, company_headers)
    reset = client.post(f"/api/employees/{enrollment.employee_id}/face/reset", headers=company_headers)
    assert reset.status_code == 200
    assert _row(enrollment.id).photo_object is None and _queue() == {_face_object(enrollment)}
    assert _run() == {DELETED: 1} and bucket.objects == {}


def test_deleting_an_employee_removes_their_photos_from_the_bucket(client, company_headers, bucket):
    enrollment = _enroll(client, company_headers)
    assert client.delete(f"/api/employees/{enrollment.employee_id}", headers=company_headers).status_code == 200
    assert _queue() == {_face_object(enrollment)}  # encolado en la MISMA transacción que borró al empleado
    assert _run() == {DELETED: 1} and bucket.objects == {}


def _queued(*names: str) -> None:
    with SessionLocal() as db:
        db.add_all(StorageDeletion(object_name=name, requested_at=datetime.now(UTC)) for name in names)
        db.commit()


def test_the_deletion_queue_is_bounded_and_survives_outages(bucket, monkeypatch, caplog):
    _queued("test/a.enc", "test/b.enc", "test/c.enc")
    bucket.down = {"delete"}
    with caplog.at_level(logging.ERROR):
        assert _run() == {DELETED: 0}
    status = _status(storage_jobs.DELETE_TASK)
    assert status is not None and "StorageUnavailable" in (status.last_error or "") and status.last_success_at is None
    assert len(_queue()) == 3

    bucket.down = set()
    monkeypatch.setattr(settings, "GCS_BATCH_SIZE", 2)
    answers = [True, True, False]  # pide un lote de dos, borra uno... y se acaba el tiempo de la vuelta
    monkeypatch.setattr(storage_jobs, "_has_time", lambda deadline: answers.pop(0) if answers else False)
    assert _run() == {DELETED: 1} and len(_queue()) == 2
    monkeypatch.undo()
    monkeypatch.setattr(settings, "GCS_BATCH_SIZE", 1)
    assert _run() == {DELETED: 2} and _queue() == set()  # lotes de uno hasta vaciarla
    recovered = _status(storage_jobs.DELETE_TASK)
    assert recovered is not None and recovered.last_success_at is not None and recovered.last_error


def test_the_deletion_task_fails_alone(bucket, monkeypatch, caplog):
    _queued("test/a.enc")
    monkeypatch.setattr(StorageRepository, "deletions", _broken)
    with caplog.at_level(logging.ERROR):
        assert _run() == {DELETED: 0}
    assert "falló el borrado de objetos" in caplog.text
    monkeypatch.setattr(StorageRepository, "record", _broken)
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        _run()
    assert "No se pudo anotar el resultado" in caplog.text


def test_a_round_without_time_left_touches_nothing(bucket, monkeypatch):
    _queued("test/a.enc")
    monkeypatch.setattr(storage_jobs, "_has_time", lambda deadline: False)
    assert _run() == {DELETED: 0} and _queue() == {"test/a.enc"} and bucket.calls == []


def test_without_a_bucket_the_queue_waits(bucket):
    _queued("test/a.enc")
    use_storage(DisabledStorage("sin llave"))
    assert _run() == {DELETED: 0} and _queue() == {"test/a.enc"} and _status(storage_jobs.DELETE_TASK) is None


# ---------------------------------------------------------------- estado del ADMIN y línea de comandos


def test_the_admin_sees_how_the_bucket_is_doing(client, company_headers, admin_headers, bucket):
    _enroll(client, company_headers)
    _enroll(client, company_headers, number="EMP-002", email="ana@empresa.com")
    url = "/api/admin/errors/server"
    (deletions,) = client.get(url, headers=admin_headers).json()["data"]["storage"]["tasks"]
    assert deletions["pending"] == 0 and deletions["last_run_at"] is None  # el mantenimiento aún no corre

    _queued("test/sobrante.enc")
    bucket.down = {"delete"}
    _run()
    storage = client.get(url, headers=admin_headers).json()["data"]["storage"]
    assert storage["configured"] is True and storage["bucket"] == "bucket-de-pruebas" and storage["prefix"] == "test"
    assert storage["count_cap"] == 10_000
    assert storage["images"] == [
        {"kind": "face-enrollment", "label": "Fotos de referencia del registro facial", "stored": 2},
        {"kind": "payment-receipt", "label": "Comprobantes de pago", "stored": 0},
        {"kind": "user-avatar", "label": "Fotos de perfil (un objeto por tamaño)", "stored": 0},
        {"kind": "fraud-evidence", "label": "Fotogramas de evidencia de casos de fraude", "stored": 0},
        {"kind": "company-document", "label": "Documentos de las empresas", "stored": 0},
    ]
    (deletions,) = storage["tasks"]
    assert deletions["task"] == "delete" and deletions["pending"] == 1
    assert "StorageUnavailable" in deletions["last_error"] and deletions["last_success_at"] is None
    assert client.get(url, headers=company_headers).status_code == 403

    use_storage(DisabledStorage("faltan GCS_BUCKET o GCS_CREDENTIALS_FILE en el .env"))
    off = client.get("/api/admin/errors/server", headers=admin_headers).json()["data"]["storage"]
    assert off["configured"] is False and off["backend"] == "disabled" and "GCS_BUCKET" in off["reason"]


def _cli(monkeypatch, *args: str) -> int:
    monkeypatch.setattr(sys, "argv", ["app.cli", *args])
    return cli.main()


def test_storage_status_command(client, company_headers, bucket, monkeypatch, capsys):
    _enroll(client, company_headers)
    _queued("test/sobrante.enc")
    bucket.down = {"delete"}
    _run()
    assert _cli(monkeypatch, "storage", "status") == 0
    out = capsys.readouterr().out
    assert "gs://bucket-de-pruebas/test/" in out
    assert "- Fotos de referencia del registro facial: en el bucket: 1" in out
    assert "- Objetos por borrar del bucket: 1" in out and "último error" in out
    assert "migrar" not in out  # las imágenes solo viven en el bucket: no hay nada que migrar


def test_storage_status_without_a_bucket(client, monkeypatch, capsys):
    use_storage(DisabledStorage("faltan GCS_BUCKET o GCS_CREDENTIALS_FILE en el .env"))
    assert _cli(monkeypatch, "storage", "status") == 0
    out = capsys.readouterr().out
    assert "apagado (faltan GCS_BUCKET" in out and "- Objetos por borrar del bucket: 0" in out
    assert "último error" not in out  # el mantenimiento nunca corrió


# ---------------------------------------------------------------- configuración


def test_bucket_and_prefix_settings(monkeypatch):
    for name in ("GCS_BUCKET", "GCS_PREFIX", "ENVIRONMENT"):
        monkeypatch.delenv(name, raising=False)
    base = {"DATA_ENCRYPTION_KEY": settings.DATA_ENCRYPTION_KEY, "_env_file": None}
    bucket = " gs://employee-time-clock-fb8ba.firebasestorage.app/ "
    configured = Settings(**base, GCS_BUCKET=bucket, GCS_PREFIX="production")
    assert configured.GCS_BUCKET == "employee-time-clock-fb8ba.firebasestorage.app"
    assert configured.storage_prefix == "production"
    assert Settings(**base, ENVIRONMENT="Staging QA").storage_prefix == "staging-qa"
    assert Settings(**base, ENVIRONMENT="***").storage_prefix == "default"
    with pytest.raises(ValueError, match="GCS_PREFIX"):
        Settings(**base, GCS_PREFIX="Producción/2")
