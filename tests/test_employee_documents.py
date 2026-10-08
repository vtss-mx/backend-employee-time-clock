"""Documentos de identidad del empleado (onboarding con OCR; decisión del dueño del producto, 2026-10-07): el empleado
sube el comprobante de domicilio y una identificación oficial, el servidor extrae la información con OCR de MEJOR
ESFUERZO (motor falso: `tests/ocr_support.py`) y la empresa la revisa, confirma o corrige en el expediente. El archivo
va CIFRADO al bucket (falso, en memoria) y la base solo guarda su referencia y los datos (el número de documento,
cifrado)."""

import base64
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError

from app.core.clock import business_today
from app.core.config import settings
from app.core.crypto import decrypt_bytes
from app.core.database import SessionLocal
from app.core.soft_delete import WITH_DELETED, with_deleted
from app.models import Company, EmployeeDocument, StorageSnapshot
from app.repositories.employee_document_repository import EmployeeDocumentRepository
from app.services import storage_jobs
from app.services.employee_document_service import _as_date
from app.services.maintenance_service import purge_expired
from app.services.usage_service import capture_storage
from tests.conftest import create_company, create_employee, login
from tests.document_support import PDF, png

MINE = "/api/me/documents"
# CURP y clave de elector de un anverso de INE (la CURP tiene dígito verificador válido; homoclave «0» → 1990).
CURP = "PEXJ900510HSRRNN09"
INE_TEXT = f"INSTITUTO NACIONAL ELECTORAL\nCURP {CURP}\nCLAVE PRLZJN85010101H200\nANA RUIZ"
DB_DOWN = OperationalError("INSERT", {}, Exception("la BD no respondió"))


def company_url(employee_id: int) -> str:
    return f"/api/validations/employees/{employee_id}/documents"


def company_id_of(client, company_headers) -> int:
    return client.get("/api/users/me", headers=company_headers).json()["data"]["company"]["id"]


def require_documents(company_id: int, value: bool = True) -> None:
    with SessionLocal() as db:
        db.execute(update(Company).where(Company.id == company_id).values(require_employee_documents=value))
        db.commit()


def employee(client, company_headers, *, number="EMP-001", email="juan@empresa.com") -> tuple[dict[str, str], int]:
    """Crea un empleado y devuelve (headers del empleado, employee_id)."""
    created = create_employee(client, company_headers, number=number, email=email)
    assert created.status_code == 201, created.text
    return login(client, email, "Empleado123"), created.json()["data"]["id"]


def upload(client, headers, url=MINE, *, name="ine.png", data=png(), type="NATIONAL_ID"):
    return client.post(
        url, files={"file": (name, data, "application/octet-stream")}, data={"type": type}, headers=headers
    )


def uploaded(client, headers, url=MINE, **kwargs) -> dict:
    response = upload(client, headers, url, **kwargs)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _row(document_id: int) -> EmployeeDocument:
    with SessionLocal() as db:
        row = db.get(EmployeeDocument, document_id, execution_options=WITH_DELETED)
        assert row is not None
        db.expunge(row)
        return row


def _rows() -> int:
    with SessionLocal() as db:
        return int(db.scalar(with_deleted(select(func.count()).select_from(EmployeeDocument))) or 0)


# ---------------------------------------------------------------- subir con OCR


def test_an_image_id_is_read_with_ocr_and_its_number_is_encrypted(client, company_headers, ocr, bucket):
    company_id = company_id_of(client, company_headers)
    headers, employee_id = employee(client, company_headers)
    ocr.text = INE_TEXT
    document = uploaded(client, headers)
    assert document["type"] == "NATIONAL_ID" and document["uploaded_by_employee"] is True
    assert document["uploaded_by"] == "juan@empresa.com" and document["ocr_processed"] is True
    assert document["mrz_verified"] is False and document["ocr_confidence"] == pytest.approx(0.9)
    data = document["data"]
    assert data["curp"] == CURP and data["document_number"] == CURP and data["sex"] == "M"
    assert data["birth_date"] == "1990-05-10" and data["voter_key"] == "PRLZJN85010101H200"
    assert ocr.languages == ["spa+eng"]  # se llamó al OCR con los idiomas configurados
    row = _row(document["id"])
    assert row.object_name == f"test/companies/{company_id}/employees/{employee_id}/documents/{row.uid}.png.enc"
    assert row.document_number_encrypted is not None and CURP.encode() not in row.document_number_encrypted.encode()
    assert (
        decrypt_bytes(row.document_number_encrypted.encode()).decode() == CURP
    )  # cifrado en reposo, legible con la llave


def test_a_pdf_is_stored_without_ocr(client, company_headers, ocr):
    headers, _ = employee(client, company_headers)
    document = uploaded(client, headers, name="recibo.pdf", data=PDF, type="PROOF_OF_ADDRESS")
    assert document["ocr_processed"] is False and document["ocr_confidence"] is None
    assert document["data"]["document_number"] is None and ocr.calls == 0  # el OCR no se llama con un PDF


def test_requirements_say_what_is_missing(client, company_headers, ocr):
    company_id = company_id_of(client, company_headers)
    require_documents(company_id)
    headers, _ = employee(client, company_headers)
    first = client.get(f"{MINE}/requirements", headers=headers).json()
    assert first["code"] == "EMPLOYEE_DOCUMENT_REQUIREMENTS"
    assert first["data"]["required"] is True
    assert first["data"]["needs_official_id"] is True and first["data"]["needs_proof_of_address"] is True
    categories = {t["code"]: t["category"] for t in first["data"]["types"]}
    assert categories["PASSPORT"] == "OFFICIAL_ID" and categories["PROOF_OF_ADDRESS"] == "PROOF_OF_ADDRESS"
    uploaded(client, headers)  # una identificación oficial
    uploaded(client, headers, name="recibo.pdf", data=PDF, type="PROOF_OF_ADDRESS")
    done = client.get(f"{MINE}/requirements", headers=headers).json()["data"]
    assert (
        done["needs_official_id"] is False and done["needs_proof_of_address"] is False and len(done["documents"]) == 2
    )


def test_requirements_when_the_company_does_not_ask_for_documents(client, company_headers):
    headers, _ = employee(client, company_headers)
    data = client.get(f"{MINE}/requirements", headers=headers).json()["data"]
    assert data["required"] is False and data["needs_official_id"] is False and data["needs_proof_of_address"] is False


# ---------------------------------------------------------------- descargar, listar, papelera


def test_list_download_delete_and_restore(client, company_headers, bucket):
    headers, _ = employee(client, company_headers)
    document = uploaded(client, headers, name="recibo.pdf", data=PDF, type="PROOF_OF_ADDRESS")
    listed = client.get(MINE, headers=headers).json()
    assert listed["code"] == "EMPLOYEE_DOCUMENTS" and listed["message"] == "1 documento"
    body = client.get(f"{MINE}/{document['id']}/file", headers=headers).json()["data"]
    assert base64.b64decode(body["data"]) == PDF and body["content_type"] == "application/pdf"
    assert client.delete(f"{MINE}/{document['id']}", headers=headers).json()["code"] == "EMPLOYEE_DOCUMENT_DELETED"
    assert client.get(MINE, headers=headers).json()["data"]["items"] == []
    (trashed,) = client.get(f"{MINE}?deleted=true", headers=headers).json()["data"]["items"]
    assert trashed["id"] == document["id"] and trashed["deleted_by"] == "juan@empresa.com"
    assert client.delete(f"{MINE}/{document['id']}", headers=headers).json()["code"] == "ALREADY_DELETED"
    restored = client.post(f"{MINE}/{document['id']}/restore", headers=headers)
    assert restored.status_code == 200 and restored.json()["data"]["deleted_at"] is None
    assert client.post(f"{MINE}/{document['id']}/restore", headers=headers).json()["code"] == "NOT_DELETED"


# ---------------------------------------------------------------- la empresa revisa y confirma


def test_the_company_reviews_and_confirms_or_corrects_the_data(client, company_headers, ocr):
    headers, employee_id = employee(client, company_headers)
    ocr.text = INE_TEXT
    document = uploaded(client, headers)
    url = company_url(employee_id)
    listed = client.get(url, headers=company_headers).json()["data"]["items"]
    assert len(listed) == 1 and listed[0]["data"]["curp"] == CURP and listed[0]["confirmed"] is False
    file = client.get(f"{url}/{document['id']}/file", headers=company_headers)
    assert file.status_code == 200 and file.json()["data"]["file_name"] == "ine.png"
    corrected = client.patch(
        f"{url}/{document['id']}/data",
        json={"full_name": "Ana Ruiz", "document_number": "CORRECTED123"},
        headers=company_headers,
    )
    assert corrected.status_code == 200 and corrected.json()["code"] == "EMPLOYEE_DOCUMENT_DATA_SAVED"
    saved = corrected.json()["data"]
    assert saved["confirmed"] is True and saved["confirmed_by"] == "admin@empresa.com"
    assert saved["data"]["full_name"] == "Ana Ruiz" and saved["data"]["document_number"] == "CORRECTED123"
    assert saved["data"]["curp"] == CURP  # lo no enviado no cambió
    assert decrypt_bytes(_row(document["id"]).document_number_encrypted.encode()).decode() == "CORRECTED123"


def test_the_employee_cannot_delete_a_document_the_company_confirmed(client, company_headers, ocr):
    headers, employee_id = employee(client, company_headers)
    document = uploaded(client, headers)
    assert (
        client.patch(f"{company_url(employee_id)}/{document['id']}/data", json={}, headers=company_headers).status_code
        == 200
    )
    denied = client.delete(f"{MINE}/{document['id']}", headers=headers)
    assert denied.status_code == 409 and denied.json()["code"] == "DOCUMENT_CONFIRMED"


def test_clearing_a_field_sets_it_to_null(client, company_headers, ocr):
    headers, employee_id = employee(client, company_headers)
    ocr.text = INE_TEXT
    document = uploaded(client, headers)
    cleared = client.patch(
        f"{company_url(employee_id)}/{document['id']}/data",
        json={"document_number": "", "curp": None},
        headers=company_headers,
    )
    assert cleared.json()["data"]["data"]["document_number"] is None
    assert cleared.json()["data"]["data"]["curp"] is None
    assert _row(document["id"]).document_number_encrypted is None


# ---------------------------------------------------------------- límites y fallas


def test_type_size_and_rate_limits(client, company_headers, monkeypatch, bucket):
    headers, _ = employee(client, company_headers)
    unknown = upload(client, headers, type="FACTURA")
    assert unknown.status_code == 422 and unknown.json()["code"] == "DOCUMENT_TYPE_INVALID"
    monkeypatch.setattr(settings, "EMPLOYEE_DOCUMENT_MAX_MB", 0.00001)  # 10 bytes
    too_large = upload(client, headers)
    assert too_large.status_code == 413 and too_large.json()["code"] == "EMPLOYEE_DOCUMENT_TOO_LARGE"
    monkeypatch.undo()
    # El límite es por usuario (`employee-document:user:{id}`); los intentos de arriba ya gastaron el cupo de `headers`,
    # así que para probarlo con límite 1 se usa un empleado NUEVO (contador en cero): la 1.ª pasa y la 2.ª es 429.
    fresh, _ = employee(client, company_headers, number="RL-1", email="rate@empresa.com")
    monkeypatch.setattr(settings, "RATE_LIMIT_EMPLOYEE_DOCUMENT_UPLOADS_PER_MINUTE", 1)
    assert upload(client, fresh).status_code == 201
    assert upload(client, fresh).status_code == 429


def test_without_the_bucket_nothing_is_saved_and_the_ocr_failure_degrades(client, company_headers, ocr, bucket):
    headers, _employee_id = employee(client, company_headers)
    bucket.down = {"put"}
    assert upload(client, headers).json()["code"] == "STORAGE_UNAVAILABLE"
    bucket.down = set()
    assert _rows() == 0
    ocr.down = True  # el motor no disponible nunca bloquea: se guarda sin datos
    document = uploaded(client, headers)
    assert document["ocr_processed"] is False and document["data"]["document_number"] is None


def test_an_upload_whose_row_cannot_be_saved_leaves_the_bucket(client, company_headers, bucket, monkeypatch):
    headers, _ = employee(client, company_headers)

    def broken(*_args, **_kwargs):
        raise DB_DOWN

    monkeypatch.setattr(EmployeeDocumentRepository, "add", broken)
    response = upload(client, headers)
    assert response.status_code == 503 and response.json()["code"] == "DATABASE_UNAVAILABLE"
    assert bucket.objects == {} and _rows() == 0


# ---------------------------------------------------------------- aislamiento y permisos


def test_other_companys_employee_and_unknown_ids_are_not_found(client, admin_headers, company_headers):
    headers, employee_id = employee(client, company_headers)
    uploaded(client, headers)
    create_company(client, admin_headers, rfc="OTR120315AB1", admin_email="otra@otra.com")
    other_headers = login(client, "otra@otra.com", "Empresa1234")
    other_company = company_id_of(client, other_headers)
    other_employee_headers, _other_employee_id = employee(client, other_headers, number="OT-1", email="pedro@otra.com")
    # La empresa B no ve al empleado de A (otra empresa: 404 EMPLOYEE_NOT_FOUND).
    assert client.get(company_url(employee_id), headers=other_headers).json()["code"] == "EMPLOYEE_NOT_FOUND"
    assert client.get(company_url(10**6), headers=company_headers).json()["code"] == "EMPLOYEE_NOT_FOUND"
    # El empleado de B no ve los documentos del empleado de A (su alcance sale de su sesión).
    assert client.get(MINE, headers=other_employee_headers).json()["data"]["total"] == 0
    assert other_company != company_id_of(client, company_headers)


def test_roles_do_not_cross(client, admin_headers, company_headers):
    headers, employee_id = employee(client, company_headers)
    # El ADMIN y el empleado no usan las rutas de la empresa; la empresa no usa las del empleado.
    assert client.get(MINE, headers=company_headers).status_code == 403
    assert client.get(MINE, headers=admin_headers).status_code == 403
    assert client.get(company_url(employee_id), headers=headers).status_code == 403
    assert client.get(company_url(employee_id), headers=admin_headers).status_code == 403


# ---------------------------------------------------------------- almacenamiento y depuración


def test_documents_count_in_the_storage_of_their_company(client, company_headers, bucket):
    company_id = company_id_of(client, company_headers)
    headers, _ = employee(client, company_headers)
    document = uploaded(client, headers, name="recibo.pdf", data=PDF, type="PROOF_OF_ADDRESS")
    day = business_today()
    with SessionLocal() as db:
        capture_storage(db, day)
        db.commit()
        snapshot = db.get(StorageSnapshot, (company_id, day, "PEOPLE"))
        assert snapshot is not None and snapshot.bytes >= document["size"]
        stored = {image.kind: image.stored for image in storage_jobs.status(db).images}
    assert stored["employee-document"] == 1


def test_the_purge_takes_the_file_out_of_the_bucket(client, company_headers, bucket):
    headers, _ = employee(client, company_headers)
    document = uploaded(client, headers, name="recibo.pdf", data=PDF, type="PROOF_OF_ADDRESS")
    assert client.delete(f"{MINE}/{document['id']}", headers=headers).status_code == 200
    name = _row(document["id"]).object_name
    with SessionLocal() as db:
        when = datetime.now(UTC) - timedelta(days=settings.SOFT_DELETE_RETENTION_DAYS + 1)
        db.execute(
            with_deleted(update(EmployeeDocument).where(EmployeeDocument.id == document["id"]).values(deleted_at=when))
        )
        db.commit()
    with SessionLocal() as db:
        removed = purge_expired(db)
    assert removed["documentos de empleados eliminados"] == 1 and removed[storage_jobs.DELETED] == 1
    assert _rows() == 0 and name not in bucket.objects


def test_documents_are_erased_when_the_employee_is_deleted(client, company_headers, bucket):
    """Al eliminar al empleado sus documentos de identidad se borran DE VERDAD con la persona (regla 13, LFPDPPP: una
    identificación lleva la foto y los datos de la persona), como la biometría: la fila y el archivo del bucket salen en
    la misma transacción —incluidos los que el propio empleado ya había eliminado de su lista—, sin esperar a la
    depuración (decisión del dueño del producto, 2026-10-07; antes se conservaban un año)."""
    headers, employee_id = employee(client, company_headers)
    live = uploaded(client, headers, name="recibo.pdf", data=PDF, type="PROOF_OF_ADDRESS")  # vigente
    trashed = uploaded(client, headers, type="NATIONAL_ID")  # lo eliminó el propio empleado (su archivo sigue subido)
    assert client.delete(f"{MINE}/{trashed['id']}", headers=headers).status_code == 200
    live_name, trashed_name = _row(live["id"]).object_name, _row(trashed["id"]).object_name
    assert live_name in bucket.objects and trashed_name in bucket.objects
    # La empresa elimina al empleado: AL MOMENTO desaparecen sus filas (vigentes y eliminadas) y sus objetos se encolan.
    assert client.delete(f"/api/employees/{employee_id}", headers=company_headers).status_code == 200
    assert _rows() == 0  # ninguna fila queda, ni la que estaba en «Eliminados»
    with SessionLocal() as db:  # la depuración solo vacía la cola del bucket (los objetos ya estaban encolados)
        purge_expired(db)
    assert live_name not in bucket.objects and trashed_name not in bucket.objects


def test_restoring_the_employee_does_not_bring_its_documents_back(client, company_headers):
    """Restaurar al empleado NO regresa sus documentos (se borraron de verdad, como la biometría): el expediente queda
    vacío y los vuelve a subir."""
    headers, employee_id = employee(client, company_headers)
    uploaded(client, headers, name="recibo.pdf", data=PDF, type="PROOF_OF_ADDRESS")
    assert client.delete(f"/api/employees/{employee_id}", headers=company_headers).status_code == 200
    assert client.post(f"/api/employees/{employee_id}/restore", headers=company_headers).status_code == 200
    assert _rows() == 0  # no regresaron
    listing = client.get(company_url(employee_id), headers=company_headers)
    assert listing.status_code == 200 and listing.json()["data"]["total"] == 0


def test_the_download_degrades_when_the_bucket_is_down(client, company_headers, bucket):
    """Descargar un documento con el bucket caído responde 503 (reintentable), nunca un 500: la referencia es NOT NULL
    pero el archivo vive solo en el bucket."""
    headers, _ = employee(client, company_headers)
    document = uploaded(client, headers, name="recibo.pdf", data=PDF, type="PROOF_OF_ADDRESS")
    bucket.down = {"get"}
    response = client.get(f"{MINE}/{document['id']}/file", headers=headers)
    assert response.status_code == 503 and response.json()["code"] == "STORAGE_UNAVAILABLE"


def test_get_reads_a_deleted_document_without_locking_it(client, company_headers, bucket):
    """`get(include_deleted=True)` sin bloqueo lee un documento de «Eliminados» (lo que la papelera y el historial
    necesitan); la lectura vigente (por omisión) lo excluye."""
    company_id = company_id_of(client, company_headers)
    headers, employee_id = employee(client, company_headers)
    document = uploaded(client, headers, name="recibo.pdf", data=PDF, type="PROOF_OF_ADDRESS")
    assert client.delete(f"{MINE}/{document['id']}", headers=headers).status_code == 200
    with SessionLocal() as db:
        repo = EmployeeDocumentRepository(db, company_id)
        assert repo.get(document["id"], employee_id) is None  # vigente: el borrado lógico lo oculta
        found = repo.get(document["id"], employee_id, include_deleted=True)
        assert found is not None and found.id == document["id"] and found.deleted_at is not None


def test_an_iso_date_from_the_ocr_is_parsed_and_anything_else_is_ignored():
    """El OCR entrega las fechas en ISO (`AAAA-MM-DD`); una vacía o en otro formato se ignora (mejor esfuerzo), nunca
    rompe el guardado del expediente."""
    assert _as_date("2026-03-15") == date(2026, 3, 15)
    assert _as_date(None) is None and _as_date("") is None and _as_date("15/03/2026") is None
