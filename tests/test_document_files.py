"""Qué archivo se acepta como documento de la empresa (`app/services/document_files.py`): formato por su contenido, sin
macros, XML sin DTD, imágenes sin metadatos y nombre limpio (reglas puras: sin BD ni red)."""

import io
import struct
import zipfile

import pytest
from PIL import Image

from app.core.config import settings
from app.core.exceptions import UnprocessableError
from app.services import document_files
from app.services.document_files import DOC, DOCX, JPEG, PDF, PNG, XLS, XLSX, XML, clean_name, inspect, ole_entries
from tests.document_support import (
    CFDI,
    SLIDES_MAIN,
    STORAGE,
    STREAM,
    WORD_MACROS,
    XML_BOMB,
    XML_BROKEN,
    XML_EXTERNAL,
    doc,
    docx,
    jpeg,
    ole,
    ooxml,
    plain_zip,
    png,
    xls,
    xlsx,
)
from tests.document_support import PDF as PDF_FILE


def _code(raw_name: str | None, data: bytes) -> str:
    with pytest.raises(UnprocessableError) as error:
        inspect(raw_name, data)
    assert error.value.field == "file"
    return error.value.code


@pytest.mark.parametrize(
    ("name", "data", "content_type", "file_name"),
    [
        ("constancia.pdf", PDF_FILE, PDF, "constancia.pdf"),
        ("acta.docx", docx(), DOCX, "acta.docx"),
        ("nomina.xlsx", xlsx(), XLSX, "nomina.xlsx"),
        ("contrato.doc", doc(), DOC, "contrato.doc"),
        ("libro.XLS", xls(), XLS, "libro.XLS"),
        ("factura.xml", CFDI, XML, "factura.xml"),
        ("factura.xml", b"\xef\xbb\xbf" + CFDI, XML, "factura.xml"),
    ],
)
def test_each_allowed_format_is_recognized_by_its_content(name, data, content_type, file_name):
    accepted = inspect(name, data)
    assert (accepted.content_type, accepted.file_name, accepted.data) == (content_type, file_name, data)


def test_the_extension_never_decides_the_format():
    # Un ejecutable con extensión de PDF, un texto, un ZIP, una presentación y un archivo OLE que no es Word ni Excel.
    for data in (b"MZ\x90\x00 programa", b"hola", plain_zip(), ooxml(SLIDES_MAIN, "ppt/presentation.xml")):
        assert _code("constancia.pdf", data) == "DOCUMENT_FORMAT_NOT_ALLOWED"
    assert _code("presentacion.doc", ole([("PowerPoint Document", STREAM)])) == "DOCUMENT_FORMAT_NOT_ALLOWED"
    assert _code("x.xml", b"<r/>") == "DOCUMENT_FORMAT_NOT_ALLOWED"  # sin declaración `<?xml`: no es un CFDI
    assert _code("x.xml", b"  " + CFDI) == "DOCUMENT_FORMAT_NOT_ALLOWED"  # la declaración va al principio
    # Lo que es de verdad se acepta con la extensión de su formato real.
    assert inspect("acta.pdf", png()).file_name == "acta.png"
    assert inspect("foto.heic", jpeg()).file_name == "foto.jpg"
    assert inspect("factura.txt", CFDI).file_name == "factura.xml"


def test_an_empty_file_is_rejected():
    assert _code("vacio.pdf", b"") == "DOCUMENT_EMPTY"


def test_macros_are_never_accepted():
    assert _code("acta.docx", docx(extra={"word/vbaProject.bin": b"vba"})) == "DOCUMENT_MACROS_NOT_ALLOWED"
    assert _code("acta.docx", ooxml(WORD_MACROS)) == "DOCUMENT_MACROS_NOT_ALLOWED"  # un DOCM renombrado
    assert _code("contrato.doc", doc(("Macros", STORAGE), ("VBA", STORAGE))) == "DOCUMENT_MACROS_NOT_ALLOWED"
    assert _code("libro.xls", xls(("_VBA_PROJECT_CUR", STORAGE))) == "DOCUMENT_MACROS_NOT_ALLOWED"


def test_a_damaged_office_file_is_rejected():
    truncated = docx()[:40]  # la firma de un ZIP sin su directorio
    assert _code("acta.docx", truncated) == "DOCUMENT_DAMAGED"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:  # su `[Content_Types].xml` dice usar un método que no existe
        archive.writestr("[Content_Types].xml", "x")
    broken = bytearray(buffer.getvalue())
    for offset in (8, broken.rfind(b"PK\x01\x02") + 10):  # método de compresión (local y central)
        struct.pack_into("<H", broken, offset, 99)
    assert _code("acta.docx", bytes(broken)) == "DOCUMENT_DAMAGED"


def test_xml_is_parsed_without_dtd_or_entities():
    assert _code("bomba.xml", XML_BOMB) == "DOCUMENT_XML_UNSAFE"
    assert _code("externa.xml", XML_EXTERNAL) == "DOCUMENT_XML_UNSAFE"
    assert _code("rota.xml", XML_BROKEN) == "DOCUMENT_DAMAGED"
    assert _code("indefinida.xml", b'<?xml version="1.0"?><r>&nada;</r>') == "DOCUMENT_DAMAGED"


def test_images_are_stored_without_any_metadata():
    for data, kind in ((jpeg(), JPEG), (png(), PNG), (png(mode="P", transparent=True), PNG)):
        accepted = inspect("foto", data)
        assert accepted.content_type == kind and b"secreto" not in accepted.data
        image = Image.open(io.BytesIO(accepted.data))
        assert not image.getexif() and "exif" not in image.info and "comment" not in image.info
    # La orientación EXIF (girada 90°) se aplica a los píxeles antes de quitarla.
    assert Image.open(io.BytesIO(inspect("foto.jpg", jpeg(size=(40, 20))).data)).size == (20, 40)
    transparent = Image.open(io.BytesIO(inspect("logo.png", png(mode="P", transparent=True)).data))
    assert transparent.mode == "RGBA" and transparent.getpixel((0, 0))[3] == 0  # la transparencia, en su canal alfa


def test_huge_or_broken_images_are_rejected(monkeypatch):
    monkeypatch.setattr(settings, "COMPANY_DOCUMENT_MAX_MEGAPIXELS", 0.0001)  # 100 píxeles
    with pytest.raises(UnprocessableError) as error:
        inspect("foto.jpg", jpeg())
    assert error.value.code == "DOCUMENT_IMAGE_TOO_LARGE" and error.value.params == {"max": "0.0001"}
    monkeypatch.undo()

    def bomb(*_args, **_kwargs):
        raise Image.DecompressionBombError("demasiados píxeles")

    monkeypatch.setattr(document_files.Image, "open", bomb)
    assert _code("foto.png", png()) == "DOCUMENT_IMAGE_TOO_LARGE"
    monkeypatch.undo()
    assert _code("foto.png", png()[:30]) == "DOCUMENT_DAMAGED"  # encabezado sin imagen
    noisy = jpeg(size=(300, 300), noisy=True)
    assert _code("foto.jpg", noisy[: len(noisy) * 2 // 3]) == "DOCUMENT_DAMAGED"  # abre, pero se trunca al decodificar


def test_the_ole_directory_is_read_like_its_format_says():
    entries = [(f"Flujo {i}", STREAM) for i in range(9)]  # tres sectores de directorio encadenados por la FAT
    assert ole_entries(ole(entries)) == {*entries}
    assert ole_entries(ole(entries, shift=12)) == {*entries}  # sectores de 4096 bytes (versión 4)
    assert ole_entries(ole([("Workbook", STREAM)], difat=True)) == {("Workbook", STREAM)}  # FAT listada en la DIFAT
    assert inspect("libro.xls", ole([("Book", STREAM)])).content_type == XLS  # Excel 5/95


def _patched(data: bytes, fmt: str, offset: int, value: int) -> bytes:
    changed = bytearray(data)
    struct.pack_into(fmt, changed, offset, value)
    return bytes(changed)


def test_a_damaged_ole_file_is_rejected():
    valid = doc()
    damaged = {
        "corto": valid[:300],
        "tamaño de sector inválido": _patched(valid, "<H", 30, 7),
        "directorio fuera de la FAT": _patched(valid, "<I", 48, 500),
        "directorio fuera del archivo": _patched(valid, "<I", 48, 100),
        "FAT fuera del archivo": _patched(valid, "<I", 76, 40),
        "cadena del directorio en círculo": _patched(valid, "<I", 512 + 4, 1),
        # El sector 2 (en 1536) es el de la DIFAT; su última entrada (la siguiente DIFAT) apunta a él mismo.
        "cadena DIFAT en círculo": _patched(ole([("Workbook", STREAM)], difat=True), "<I", 1536 + 508, 2),
    }
    for name, data in damaged.items():
        assert _code("x.doc", data) == "DOCUMENT_DAMAGED", name


def test_names_are_cleaned_and_keep_their_real_extension():
    long = "a" * 300 + ".pdf"
    cases = {
        "C:\\Users\\ana\\Escritorio\\acta.pdf": "acta.pdf",
        "../../etc/constancia.pdf": "constancia.pdf",
        "fac\u202etura\x00<1>:?.pdf": "fac tura 1.pdf",  # control, formato (invierte el texto) y reservados
        "  .oculto.pdf. ": "oculto.pdf",
        "contrato.v2": "contrato.v2.pdf",  # una extensión que no es de documento se conserva
        "acta.docx": "acta.pdf",  # una extensión de documento equivocada se reemplaza
        "sin extensión": "sin extensión.pdf",
        "e\u0301xito.pdf": "éxito.pdf",  # forma compuesta (NFC)
        "": "documento.pdf",
        "...": "documento.pdf",
    }
    for raw, expected in cases.items():
        assert clean_name(raw, PDF) == expected, raw
    assert clean_name(None, JPEG) == "documento.jpg"
    assert clean_name("foto.JPEG", JPEG) == "foto.JPEG"
    cleaned = clean_name(long, PDF)
    assert len(cleaned) == 200 and cleaned.endswith("a.pdf")
