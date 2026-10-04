"""Dependencias compartidas (app/dependencies.py) y validadores de esquemas (app/schemas/validators.py)
en sus casos límite."""

import io
from contextlib import contextmanager

import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers

from app import dependencies
from app.core.config import settings
from app.core.exceptions import PayloadTooLargeError, UnprocessableError
from app.dependencies import get_pipeline, read_image_upload, read_image_uploads
from app.main import app
from app.schemas import validators
from tests.conftest import FakePipeline

# ---------------------------------------------------------------- worker facial


@pytest.fixture
def leases(client, monkeypatch) -> list[str]:
    """El motor facial real (sin la sustitución de las pruebas) con un pool simulado que anota cada
    préstamo y devolución de su worker."""
    events: list[str] = []

    @contextmanager
    def lease_pipeline():
        events.append("lease")
        try:
            yield FakePipeline()
        finally:
            events.append("return")

    monkeypatch.delitem(app.dependency_overrides, get_pipeline)
    monkeypatch.setattr(dependencies, "lease_pipeline", lease_pipeline)
    return events


def _check(client, headers, image: bytes):
    files = [("images", ("f.jpg", image, "image/jpeg"))]
    return client.post("/api/face/check", files=files, headers=headers)


def test_the_face_worker_is_returned_after_every_request(client, company_headers, leases):
    """El worker (1 por núcleo) vuelve al pool al terminar, también cuando la captura se rechaza:
    nunca queda apartado y la cola facial no se agota con capturas inválidas."""
    assert _check(client, company_headers, b"face:ana").status_code == 200
    rejected = _check(client, company_headers, b"noface")
    assert rejected.status_code == 422 and rejected.json()["code"] == "NO_FACE"
    assert leases == ["lease", "return", "lease", "return"]


# ---------------------------------------------------------------- imágenes recibidas


def _upload(content: bytes, content_type: str | None = "image/jpeg") -> UploadFile:
    headers = Headers({"content-type": content_type}) if content_type else Headers({})
    return UploadFile(io.BytesIO(content), filename="captura", headers=headers)


@pytest.mark.parametrize(
    ("upload", "code"),
    [
        (lambda: _upload(b"%PDF-1.7", "application/pdf"), "INVALID_IMAGE_FORMAT"),
        (lambda: _upload(b""), "EMPTY_IMAGE"),
    ],
    ids=["formato", "vacia"],
)
def test_invalid_uploads_are_rejected_before_analysis(upload, code):
    with pytest.raises(UnprocessableError) as exc:
        read_image_upload(upload())
    assert exc.value.code == code


def test_an_oversized_image_is_rejected_without_reading_it_whole(monkeypatch):
    monkeypatch.setattr(settings, "MAX_IMAGE_SIZE_MB", 0.001)  # ≈ 1 KB
    big = _upload(b"x" * 10_000)
    with pytest.raises(PayloadTooLargeError):
        read_image_upload(big)
    assert big.file.tell() == settings.max_image_bytes + 1  # leyó solo lo necesario para saberlo


def test_an_upload_without_declared_type_is_accepted_and_analysed_by_content():
    """Algunos navegadores no declaran el tipo: no se rechaza por eso (el motor valida el contenido)."""
    assert read_image_upload(_upload(b"face:ana", None)) == b"face:ana"


@pytest.mark.parametrize(("count", "code"), [(0, "EMPTY_IMAGE"), (4, "TOO_MANY_IMAGES")])
def test_the_number_of_images_per_request_is_bounded(count, code):
    with pytest.raises(UnprocessableError) as exc:
        read_image_uploads([_upload(b"face:ana") for _ in range(count)], max_files=3)
    assert exc.value.code == code


# ---------------------------------------------------------------- datos de la empresa


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (" - ", "obligatorio"),
        ("XAXX010101000", "genérico"),  # 13 caracteres: se valida como persona física
        ("AB1231231XX1", "12 caracteres"),  # 12 con formato incorrecto
        ("ABC991332XX1", "fecha"),  # mes 13
    ],
)
def test_invalid_company_rfc(raw, message):
    with pytest.raises(ValueError, match=message):
        validators.normalize_company_rfc(raw)


def test_company_rfc_of_a_legal_entity_is_normalized():
    assert validators.normalize_company_rfc("abc-990101-xx1") == "ABC990101XX1"


@pytest.mark.parametrize(("raw", "message"), [("  a  ", "La razón social es obligatorio"), ("x" * 201, "200")])
def test_company_name_limits(raw, message):
    with pytest.raises(ValueError, match=message):
        validators.normalize_company_name(raw, "La razón social")


def test_nss_is_required():
    with pytest.raises(ValueError, match="obligatorio"):
        validators.normalize_nss(" - ")
