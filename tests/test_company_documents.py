"""Documentos de la empresa (decisión del dueño del producto, 2026-10-06): el ADMIN desde la ficha de cada empresa y la
empresa desde su pantalla "Documentos" suben, descargan, eliminan y restauran los archivos que se guardan para
facturarle. Cada archivo va CIFRADO al bucket (falso, en memoria: `tests/storage_support.py`) y la base solo guarda su
referencia; la depuración de lo eliminado lo saca del bucket."""

import base64
import hashlib
import io
from datetime import UTC, datetime, timedelta

import pytest
from PIL import Image
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError

from app.core.clock import business_today
from app.core.config import settings
from app.core.crypto import decrypt_bytes
from app.core.database import SessionLocal
from app.core.object_storage import DisabledStorage, use_storage
from app.core.soft_delete import WITH_DELETED, with_deleted
from app.models import CatalogCompanyDocumentType, Company, CompanyDocument, StorageDeletion, StorageSnapshot
from app.repositories.company_document_repository import CompanyDocumentRepository
from app.services import storage_jobs
from app.services.catalog_service import clear_catalog_cache
from app.services.maintenance_service import purge_expired
from app.services.usage_service import capture_storage
from tests.billing_support import company_with_plan, pay, set_today
from tests.conftest import create_company, login
from tests.document_support import CFDI, PDF, XML_BOMB, doc, docx, jpeg, ooxml, png, xls, xlsx
from tests.document_support import WORD_MACROS as DOCM
from tests.storage_support import swap_object

OWN = "/api/documents"
LATER = datetime.now(UTC) + timedelta(days=settings.SOFT_DELETE_RETENTION_DAYS + 1)
DB_DOWN = OperationalError("INSERT", {}, Exception("la BD no respondió"))


def admin_url(company_id: int) -> str:
    return f"/api/admin/companies/{company_id}/documents"


def company_id_of(client, company_headers) -> int:
    return client.get("/api/users/me", headers=company_headers).json()["data"]["company"]["id"]


def upload(client, headers, url=OWN, name="constancia.pdf", data=PDF, *, type="TAX_CERTIFICATE", note=None):
    form = {"type": type} if note is None else {"type": type, "note": note}
    return client.post(url, files={"file": (name, data, "application/octet-stream")}, data=form, headers=headers)


def uploaded(client, headers, url=OWN, **kwargs) -> dict:
    response = upload(client, headers, url, **kwargs)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _row(document_id: int) -> CompanyDocument:
    with SessionLocal() as db:
        row = db.get(CompanyDocument, document_id, execution_options=WITH_DELETED)
        assert row is not None
        db.expunge(row)
        return row


def _rows() -> int:
    with SessionLocal() as db:
        return int(db.scalar(with_deleted(select(func.count()).select_from(CompanyDocument))) or 0)


def _download(client, headers, url: str) -> bytes:
    response = client.get(url, headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["size"] == len(base64.b64decode(body["data"]))
    return base64.b64decode(body["data"])


FORMATS = [
    ("constancia.pdf", PDF, "application/pdf", "pdf"),
    ("acta.docx", docx(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"),
    ("nomina.xlsx", xlsx(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"),
    ("contrato.doc", doc(), "application/msword", "doc"),
    ("libro.xls", xls(), "application/vnd.ms-excel", "xls"),
    ("factura.xml", CFDI, "application/xml", "xml"),
    ("ine.jpg", jpeg(), "image/jpeg", "jpg"),
    ("comprobante.png", png(), "image/png", "png"),
]


@pytest.mark.parametrize(("name", "data", "content_type", "extension"), FORMATS)
def test_each_allowed_format_goes_encrypted_to_the_bucket(
    client, admin_headers, company_headers, bucket, name, data, content_type, extension
):
    company_id = company_id_of(client, company_headers)
    response = upload(client, admin_headers, admin_url(company_id), name=name, data=data, note="  Para facturar  ")
    assert response.status_code == 201 and response.json()["code"] == "COMPANY_DOCUMENT_UPLOADED"
    assert response.json()["message"] == "Documento guardado"
    document = response.json()["data"]
    assert document["content_type"] == content_type and document["file_name"] == name
    assert document["note"] == "Para facturar" and document["type"] == "TAX_CERTIFICATE"
    assert document["uploaded_by"] == "superadmin@plataforma.com" and document["uploaded_by_platform"] is True
    assert document["can_delete"] is True and document["deleted_at"] is None
    row = _row(document["id"])
    name_in_bucket = f"test/companies/{company_id}/documents/{row.uid}.{extension}.enc"
    assert row.object_name == name_in_bucket and len(row.uid) == 32
    encrypted, metadata = bucket.objects[name_in_bucket]
    stored = decrypt_bytes(encrypted)
    assert metadata["kind"] == "company-document" and metadata["company_id"] == str(company_id)
    assert row.sha256 == hashlib.sha256(encrypted).hexdigest() and row.byte_size == len(stored) == document["size"]
    assert data[:64] not in encrypted  # solo objetos ilegibles en el bucket
    if content_type.startswith("image/"):  # la imagen se guarda limpia (sin EXIF ni GPS) y sigue siendo la misma
        assert Image.open(io.BytesIO(stored)).size and b"secreto" not in stored
    else:
        assert stored == data
    url = f"{admin_url(company_id)}/{document['id']}/file"
    assert _download(client, admin_headers, url) == stored
    assert _download(client, company_headers, f"{OWN}/{document['id']}/file") == stored


def test_the_company_keeps_its_own_documents_and_sees_the_platform_ones(client, admin_headers, company_headers):
    company_id = company_id_of(client, company_headers)
    first = uploaded(client, company_headers, name="domicilio.pdf", type="PROOF_OF_ADDRESS")
    second = uploaded(client, admin_headers, admin_url(company_id), name="contrato.pdf", type="CONTRACT")
    third = uploaded(client, company_headers, name="sin nota.pdf", type="OTHER", note="   ")
    assert first["uploaded_by"] == "admin@empresa.com" and first["uploaded_by_platform"] is False
    assert third["note"] is None

    listed = client.get(OWN, headers=company_headers)
    assert listed.json()["code"] == "COMPANY_DOCUMENTS" and listed.json()["message"] == "3 documentos"
    items = listed.json()["data"]["items"]
    assert [item["id"] for item in items] == [third["id"], second["id"], first["id"]]  # el más reciente primero
    assert {item["id"]: item["can_delete"] for item in items} == {
        first["id"]: True,
        second["id"]: False,
        third["id"]: True,
    }
    as_admin = client.get(admin_url(company_id), headers=admin_headers).json()["data"]["items"]
    assert all(item["can_delete"] for item in as_admin)
    page = client.get(f"{OWN}?size=1&page=2", headers=company_headers).json()["data"]
    assert page["total"] == 3 and [item["id"] for item in page["items"]] == [second["id"]]
    english = client.get(OWN, headers={**company_headers, "Accept-Language": "en-US"}).json()
    assert english["message"] == "3 documents"


def test_a_fake_extension_or_a_dangerous_file_never_gets_in(client, company_headers, bucket):
    rejected = {
        "DOCUMENT_FORMAT_NOT_ALLOWED": ("constancia.pdf", b"MZ\x90\x00 un ejecutable"),
        "DOCUMENT_MACROS_NOT_ALLOWED": ("acta.docx", ooxml(DOCM)),
        "DOCUMENT_XML_UNSAFE": ("factura.xml", XML_BOMB),
        "DOCUMENT_EMPTY": ("vacio.pdf", b""),
    }
    for code, (name, data) in rejected.items():
        response = upload(client, company_headers, name=name, data=data)
        body = response.json()
        assert response.status_code == 422 and body["code"] == code, response.text
        assert body["errors"][0]["field"] == "file"
    assert bucket.objects == {} and _rows() == 0
    # Una imagen real con extensión de PDF se acepta como lo que es.
    renamed = uploaded(client, company_headers, name="acta.pdf", data=png())
    assert renamed["file_name"] == "acta.png" and renamed["content_type"] == "image/png"
    english = upload(client, {**company_headers, "Accept-Language": "en-US"}, data=b"hola")
    assert english.json()["message"] == "The file must be a PDF, Word, Excel, XML, JPG, or PNG file"


def test_size_type_and_note_limits(client, company_headers, monkeypatch, bucket):
    monkeypatch.setattr(settings, "COMPANY_DOCUMENT_MAX_MB", 0.00001)  # 10 bytes
    too_large = upload(client, company_headers)
    assert too_large.status_code == 413 and too_large.json()["code"] == "DOCUMENT_TOO_LARGE"
    assert too_large.json()["errors"][0]["field"] == "file" and "MB" in too_large.json()["message"]
    monkeypatch.undo()
    unknown = upload(client, company_headers, type="FACTURA")
    assert unknown.status_code == 422 and unknown.json()["code"] == "DOCUMENT_TYPE_INVALID"
    assert unknown.json()["errors"][0]["field"] == "type"
    with SessionLocal() as db:  # un tipo que se desactivó ya no se ofrece
        db.execute(
            update(CatalogCompanyDocumentType).where(CatalogCompanyDocumentType.code == "OTHER").values(active=False)
        )
        db.commit()
    clear_catalog_cache()
    assert upload(client, company_headers, type="OTHER").json()["code"] == "DOCUMENT_TYPE_INVALID"
    long_note = upload(client, company_headers, note="n" * 301)
    assert long_note.status_code == 422 and long_note.json()["errors"][0]["field"] == "note"
    assert bucket.objects == {} and _rows() == 0


def test_uploads_per_minute_are_limited(client, company_headers, monkeypatch):
    monkeypatch.setattr(settings, "RATE_LIMIT_DOCUMENT_UPLOADS_PER_MINUTE", 1)
    assert upload(client, company_headers).status_code == 201
    assert upload(client, company_headers).status_code == 429


def test_without_the_bucket_nothing_is_saved_halfway(client, company_headers, bucket):
    bucket.down = {"put"}
    down = upload(client, company_headers)
    assert down.status_code == 503 and down.json()["code"] == "STORAGE_UNAVAILABLE"
    bucket.down = set()
    bucket.tamper = True  # el bucket reporta otra cosa de lo que se subió: se borra lo subido
    assert upload(client, company_headers).json()["code"] == "STORAGE_UNAVAILABLE"
    bucket.tamper = False
    use_storage(DisabledStorage("faltan GCS_BUCKET o GCS_CREDENTIALS_FILE en el .env"))
    assert upload(client, company_headers).json()["code"] == "STORAGE_UNAVAILABLE"
    assert bucket.objects == {} and _rows() == 0


def test_an_upload_whose_row_cannot_be_saved_leaves_the_bucket(client, company_headers, bucket, monkeypatch):
    def broken(*_args, **_kwargs):
        raise DB_DOWN

    monkeypatch.setattr(CompanyDocumentRepository, "add", broken)
    response = upload(client, company_headers)
    assert response.status_code == 503 and response.json()["code"] == "DATABASE_UNAVAILABLE"
    assert bucket.objects == {} and _rows() == 0


def test_downloading_with_the_bucket_down_or_a_wrong_object_is_503(client, company_headers, bucket):
    document = uploaded(client, company_headers)
    url = f"{OWN}/{document['id']}/file"
    bucket.down = {"get"}
    assert client.get(url, headers=company_headers).json()["code"] == "STORAGE_UNAVAILABLE"
    bucket.down = set()
    row = _row(document["id"])
    with SessionLocal() as db:  # el SHA-256 coincide pero no se puede descifrar (otra llave)
        db.execute(update(CompanyDocument).values(sha256=swap_object(bucket, row.object_name)))
        db.commit()
    unreadable = client.get(url, headers=company_headers)
    assert unreadable.status_code == 503 and unreadable.json()["code"] == "STORAGE_UNAVAILABLE"


def test_trash_and_restore(client, company_headers):
    document = uploaded(client, company_headers)
    url = f"{OWN}/{document['id']}"
    deleted = client.delete(url, headers=company_headers)
    assert deleted.status_code == 200 and deleted.json()["code"] == "COMPANY_DOCUMENT_DELETED"
    assert client.get(OWN, headers=company_headers).json()["data"]["items"] == []
    (trashed,) = client.get(f"{OWN}?deleted=true", headers=company_headers).json()["data"]["items"]
    assert trashed["id"] == document["id"] and trashed["deleted_by"] == "admin@empresa.com" and trashed["deleted_at"]
    assert client.get(f"{url}/file", headers=company_headers).json()["code"] == "DOCUMENT_NOT_FOUND"
    assert client.delete(url, headers=company_headers).json()["code"] == "ALREADY_DELETED"
    restored = client.post(f"{url}/restore", headers=company_headers)
    assert restored.status_code == 200 and restored.json()["code"] == "COMPANY_DOCUMENT_RESTORED"
    assert restored.json()["data"]["deleted_at"] is None
    assert client.post(f"{url}/restore", headers=company_headers).json()["code"] == "NOT_DELETED"
    assert [item["id"] for item in client.get(OWN, headers=company_headers).json()["data"]["items"]] == [document["id"]]


def test_the_company_only_deletes_and_restores_what_it_uploaded(client, admin_headers, company_headers):
    company_id = company_id_of(client, company_headers)
    platform = uploaded(client, admin_headers, admin_url(company_id))
    own = uploaded(client, company_headers)
    denied = client.delete(f"{OWN}/{platform['id']}", headers=company_headers)
    assert denied.status_code == 403 and denied.json()["code"] == "DOCUMENT_UPLOADED_BY_PLATFORM"
    assert client.delete(f"{admin_url(company_id)}/{platform['id']}", headers=admin_headers).status_code == 200
    restore = client.post(f"{OWN}/{platform['id']}/restore", headers=company_headers)
    assert restore.status_code == 403 and restore.json()["code"] == "DOCUMENT_UPLOADED_BY_PLATFORM"
    # El ADMIN puede eliminar lo que subió la empresa; ella lo restaura (es suyo).
    assert client.delete(f"{admin_url(company_id)}/{own['id']}", headers=admin_headers).status_code == 200
    trash = client.get(f"{OWN}?deleted=true", headers=company_headers).json()["data"]["items"]
    assert {item["id"]: item["can_delete"] for item in trash} == {platform["id"]: False, own["id"]: True}
    assert client.post(f"{OWN}/{own['id']}/restore", headers=company_headers).status_code == 200
    restored = client.post(f"{admin_url(company_id)}/{platform['id']}/restore", headers=admin_headers)
    assert restored.status_code == 200 and restored.json()["data"]["can_delete"] is True


def test_unknown_or_foreign_ids_are_not_found(client, admin_headers, company_headers):
    company_id = company_id_of(client, company_headers)
    other = create_company(client, admin_headers, rfc="OTR120315AB1", admin_email="otra@otra.com").json()["data"]["id"]
    foreign = uploaded(client, admin_headers, admin_url(other))
    for document_id in (foreign["id"], 10**6):  # de otra empresa (por la ruta de A) o inexistente: 404 igual
        base = f"{admin_url(company_id)}/{document_id}"
        for method, url in (("GET", f"{base}/file"), ("DELETE", base), ("POST", f"{base}/restore")):
            assert client.request(method, url, headers=admin_headers).json()["code"] == "DOCUMENT_NOT_FOUND", url
        for method, url in (("GET", f"{OWN}/{document_id}/file"), ("DELETE", f"{OWN}/{document_id}")):
            assert client.request(method, url, headers=company_headers).json()["code"] == "DOCUMENT_NOT_FOUND", url
    assert client.delete(f"/api/admin/companies/{other}", headers=admin_headers).status_code == 200
    for missing in (other, 10**6):  # eliminada o inexistente: como lo que no existe
        base = admin_url(missing)
        calls = [
            client.get(base, headers=admin_headers),
            upload(client, admin_headers, base),
            client.get(f"{base}/{foreign['id']}/file", headers=admin_headers),
            client.delete(f"{base}/{foreign['id']}", headers=admin_headers),
            client.post(f"{base}/{foreign['id']}/restore", headers=admin_headers),
        ]
        assert {response.json()["code"] for response in calls} == {"COMPANY_NOT_FOUND"}


def _age(document_id: int, days: int) -> None:
    with SessionLocal() as db:
        when = datetime.now(UTC) - timedelta(days=days)
        stmt = update(CompanyDocument).where(CompanyDocument.id == document_id).values(deleted_at=when)
        db.execute(with_deleted(stmt))
        db.commit()


def _queued() -> set[str]:
    with SessionLocal() as db:
        return set(db.scalars(select(StorageDeletion.object_name)))


def test_the_purge_takes_the_file_out_of_the_bucket(client, company_headers, bucket):
    kept, expired, restored = (uploaded(client, company_headers, name=f"{n}.pdf") for n in ("vigente", "viejo", "otra"))
    for document in (expired, restored):
        assert client.delete(f"{OWN}/{document['id']}", headers=company_headers).status_code == 200
    _age(expired["id"], settings.SOFT_DELETE_RETENTION_DAYS + 1)
    _age(restored["id"], settings.SOFT_DELETE_RETENTION_DAYS - 1)  # aún dentro de su año
    names = {document["id"]: _row(document["id"]).object_name for document in (kept, expired, restored)}
    with SessionLocal() as db:
        removed = purge_expired(db)
    assert removed["documentos de empresas eliminados"] == 1 and removed[storage_jobs.DELETED] == 1
    assert _rows() == 2 and names[expired["id"]] not in bucket.objects
    assert {names[kept["id"]], names[restored["id"]]} <= set(bucket.objects) and _queued() == set()
    # Con el bucket caído, la fila sale y su objeto espera en la cola (se borra en la siguiente vuelta).
    assert client.post(f"{OWN}/{restored['id']}/restore", headers=company_headers).status_code == 200
    assert client.delete(f"{OWN}/{restored['id']}", headers=company_headers).status_code == 200
    _age(restored["id"], settings.SOFT_DELETE_RETENTION_DAYS + 1)
    bucket.down = {"delete"}
    with SessionLocal() as db:
        assert purge_expired(db)["documentos de empresas eliminados"] == 1
    assert _queued() == {names[restored["id"]]} and names[restored["id"]] in bucket.objects
    bucket.down = set()
    with SessionLocal() as db:
        assert storage_jobs.run(db, datetime.now(UTC))[storage_jobs.DELETED] == 1
    assert names[restored["id"]] not in bucket.objects and set(bucket.objects) == {names[kept["id"]]}


def test_documents_leave_with_their_purged_company_but_never_with_a_billed_one(
    client, admin_headers, bucket, monkeypatch
):
    set_today(monkeypatch, business_today())
    clean = create_company(client, admin_headers, rfc="CLN120315AB1", admin_email="c@c.com").json()["data"]["id"]
    clean_document = uploaded(client, admin_headers, admin_url(clean))
    billed = company_with_plan(client, admin_headers)
    billed_document = uploaded(client, admin_headers, admin_url(billed))
    assert pay(client, admin_headers, billed, "50", business_today()).status_code == 201
    assert client.delete(f"/api/admin/companies/{clean}", headers=admin_headers).status_code == 200
    with SessionLocal() as db:  # una con cobranza en «Eliminados» (no debería existir) conserva todo
        db.execute(with_deleted(update(Company).where(Company.id == billed).values(deleted_at=datetime.now(UTC))))
        db.commit()
    with SessionLocal() as db:
        early = purge_expired(db)
    assert early["documentos de empresas que se depuran"] == 0 and _rows() == 2  # aún dentro de su año
    with SessionLocal() as db:
        removed = purge_expired(db, now=LATER)
    assert removed["documentos de empresas que se depuran"] == 1 and removed["empresas eliminadas"] == 1
    assert _rows() == 1 and _row(billed_document["id"]).company_id == billed
    assert _row_missing(clean_document["id"]) and clean not in _company_ids()
    assert all(f"/companies/{clean}/" not in name for name in bucket.objects)


def _row_missing(document_id: int) -> bool:
    with SessionLocal() as db:
        return db.get(CompanyDocument, document_id, execution_options=WITH_DELETED) is None


def _company_ids() -> set[int]:
    with SessionLocal() as db:
        return set(db.scalars(with_deleted(select(Company.id))))


def test_documents_count_in_the_storage_of_their_company(client, admin_headers, company_headers, bucket):
    company_id = company_id_of(client, company_headers)
    first = uploaded(client, company_headers)
    second = uploaded(client, company_headers, name="otro.pdf", data=PDF + b"%%comentario\n")
    assert client.delete(f"{OWN}/{second['id']}", headers=company_headers).status_code == 200  # sigue en el bucket
    day = business_today()
    with SessionLocal() as db:
        capture_storage(db, day)
        db.commit()
        snapshot = db.get(StorageSnapshot, (company_id, day, "BILLING"))
        assert snapshot is not None and snapshot.rows >= 2 and snapshot.bytes >= first["size"] + second["size"]
        stored = {image.kind: image.stored for image in storage_jobs.status(db).images}
    assert stored["company-document"] == 2


def test_another_role_or_a_second_login_cannot_reach_them(client, admin_headers, company_headers):
    """Además de la matriz de `test_authorization.py`: la empresa no usa las rutas del ADMIN ni el ADMIN la de la
    empresa (su alcance sale de la sesión)."""
    company_id = company_id_of(client, company_headers)
    assert client.get(admin_url(company_id), headers=company_headers).status_code == 403
    assert client.get(OWN, headers=admin_headers).status_code == 403
    other = create_company(client, admin_headers, rfc="OTR120315AB1", admin_email="otra@otra.com")
    other_headers = login(client, "otra@otra.com", "Empresa1234")
    assert other.status_code == 201
    uploaded(client, company_headers, name="de A.pdf")
    assert client.get(OWN, headers=other_headers).json()["data"]["total"] == 0
