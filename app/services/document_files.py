"""Documentos de la empresa: qué archivo se acepta y cómo se guarda (reglas puras: sin BD ni red).

Decisión del dueño del producto (2026-10-06): la empresa y la plataforma guardan archivos de la empresa (constancia de
situación fiscal, acta constitutiva, comprobante de domicilio, identificación del representante legal, contratos...)
para facturarle. Lo que se recibe se revisa aquí ANTES de cifrarlo y subirlo al bucket (README, "Documentos de la
empresa"):

1. **Formatos por su CONTENIDO** (firma y estructura), nunca por la extensión ni por lo que declara el navegador: PDF,
   Word (DOC y DOCX), Excel (XLS y XLSX), XML (p. ej. un CFDI) e imágenes JPG o PNG. Lo demás (un ejecutable con
   extensión `.pdf`, un ZIP, una presentación, un archivo cifrado con contraseña): 422 `DOCUMENT_FORMAT_NOT_ALLOWED`.
2. **Sin macros**: un DOCX o XLSX con un proyecto de VBA (`vbaProject.bin`) o declarado con macros (`macroEnabled`:
   DOCM, XLSM y sus plantillas), y un DOC o XLS (formato OLE) con su almacenamiento de macros (`Macros`,
   `_VBA_PROJECT_CUR` o `VBA`): 422 `DOCUMENT_MACROS_NOT_ALLOWED`. El directorio OLE se lee como dice su formato
   ([MS-CFB]: encabezado, FAT y cadena del directorio), sin bibliotecas nuevas. Las macros de Excel 4.0 (XLM, hojas de
   macros dentro del libro) no se detectan sin interpretar el libro completo: quedan para el antivirus (decisión
   pendiente del dueño, p. ej. un contenedor ClamAV).
3. **XML seguro**: solo con su declaración `<?xml ...?>` (la llevan los CFDI) y analizado COMPLETO con expat de la
   biblioteca estándar (sin dependencias nuevas) rechazando toda declaración de tipo de documento (DOCTYPE): sin DTD no
   hay entidades propias ni externas (ni la "bomba de mil millones de risas" ni la lectura de archivos del servidor).
   Con DOCTYPE o entidades: 422 `DOCUMENT_XML_UNSAFE`; mal formado: 422 `DOCUMENT_DAMAGED`.
4. **Imágenes con la seguridad de la foto de perfil** (`avatar_image`): tope de megapíxeles ANTES de decodificar
   (`COMPANY_DOCUMENT_MAX_MEGAPIXELS`, 422 `DOCUMENT_IMAGE_TOO_LARGE`), orientación EXIF aplicada y TODOS los metadatos
   fuera (EXIF con la ubicación GPS, XMP, comentarios, perfil de color): se vuelven a codificar desde sus píxeles (JPEG
   con `COMPANY_DOCUMENT_JPEG_QUALITY`, PNG sin pérdida).
5. **Nombre limpio**: sin carpetas, sin caracteres de control ni de formato (p. ej. el que invierte el texto para
   disfrazar una extensión), sin los que rompen un sistema de archivos, a lo más `DOCUMENT_FILE_NAME_MAX` caracteres y
   con la extensión de su formato REAL (un PNG llamado `acta.pdf` se guarda como `acta.png`).

Toda falla es un 4xx con código estable en el campo `file`: un archivo que no sirve es un resultado normal, no algo que
el ADMIN deba corregir.
"""

import io
import re
import struct
import unicodedata
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, NoReturn
from xml.parsers import expat

from PIL import Image, ImageOps, UnidentifiedImageError

from app.core.config import settings
from app.core.exceptions import UnprocessableError
from app.i18n import Params
from app.models.company_document import DOCUMENT_FILE_NAME_MAX
from app.services.avatar_image import to_srgb

PDF: Final = "application/pdf"
DOC: Final = "application/msword"
DOCX: Final = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLS: Final = "application/vnd.ms-excel"
XLSX: Final = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
XML: Final = "application/xml"
JPEG: Final = "image/jpeg"
PNG: Final = "image/png"
#: Extensiones de cada formato aceptado (la primera es la que se pone si el nombre no trae una correcta).
EXTENSIONS: Final[dict[str, tuple[str, ...]]] = {
    PDF: ("pdf",),
    DOC: ("doc",),
    DOCX: ("docx",),
    XLS: ("xls",),
    XLSX: ("xlsx",),
    XML: ("xml",),
    JPEG: ("jpg", "jpeg"),
    PNG: ("png",),
}
#: Extensiones de documentos que, si no corresponden al formato real, se reemplazan (otra cosa se conserva y se le
#: agrega la correcta: "contrato.v2" → "contrato.v2.pdf").
_KNOWN_EXTENSIONS: Final = frozenset(
    {ext for group in EXTENSIONS.values() for ext in group}
    | {"docm", "dotx", "dotm", "xlsm", "xltx", "xltm", "xlsb", "csv", "txt", "rtf", "odt", "ods", "heic", "webp", "gif"}
    | {"tif", "tiff", "bmp", "zip", "exe", "html", "htm", "svg"}
)
#: Nombre cuando el original no deja nada útil (es un dato, como "comprobante" de un pago).
FALLBACK_NAME: Final = "documento"

_PDF_SIGNATURE: Final = b"%PDF-"
_ZIP_SIGNATURE: Final = b"PK\x03\x04"
_OLE_SIGNATURE: Final = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_JPEG_SIGNATURE: Final = b"\xff\xd8\xff"
_PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
_UTF8_BOM: Final = b"\xef\xbb\xbf"

#: Tope de lo que se descomprime de `[Content_Types].xml` de un DOCX/XLSX (unos cientos de bytes en un archivo real).
_CONTENT_TYPES_MAX_BYTES: Final = 256 * 1024
#: Tipo de la parte principal de un documento de Word o de un libro de Excel (las plantillas no se aceptan).
_OOXML_MAIN: Final = (
    (b"wordprocessingml.document.main+xml", DOCX),
    (b"spreadsheetml.sheet.main+xml", XLSX),
)

#: [MS-CFB]: valores especiales de la FAT y tipos de entrada del directorio.
_MAX_REGULAR_SECTOR: Final = 0xFFFFFFFA
_DIRECTORY_ENTRY_BYTES: Final = 128
_STORAGE, _STREAM = 1, 2
#: Entradas que delatan un proyecto de VBA (Word guarda el suyo en `Macros`, Excel en `_VBA_PROJECT_CUR`; ambos con un
#: almacenamiento `VBA` dentro).
_OLE_MACROS: Final = frozenset({"Macros", "_VBA_PROJECT_CUR", "VBA"})
#: El flujo principal de un documento de Word y de un libro de Excel (97-2003 y anteriores).
_OLE_MAIN: Final = (("WordDocument", DOC), ("Workbook", XLS), ("Book", XLS))

#: Caracteres que no van en un nombre de archivo (Windows, macOS y Linux) además de los de control.
_RESERVED: Final = re.compile(r'[<>:"/\\|?*]')
_JPEG_FORMATS: Final = ("JPEG", "MPO")


@dataclass(frozen=True)
class DocumentFile:
    """Un archivo aceptado: su nombre limpio, su formato real y lo que se guarda (una imagen ya sin metadatos)."""

    file_name: str
    content_type: str
    data: bytes


def _reject(code: str, key: str | None = None, params: Params | None = None) -> NoReturn:
    raise UnprocessableError(code=code, key=key, params=params, field="file")


def inspect(raw_name: str | None, data: bytes) -> DocumentFile:
    """El archivo listo para guardarse, o 422 con el motivo (ver el docstring del módulo)."""
    if not data:
        _reject("DOCUMENT_EMPTY")
    content_type, clean = _recognize(data)
    return DocumentFile(file_name=clean_name(raw_name, content_type), content_type=content_type, data=clean)


def _recognize(data: bytes) -> tuple[str, bytes]:
    """(formato real, lo que se guarda) por la firma y la estructura del contenido."""
    if data.startswith(_PDF_SIGNATURE):
        return PDF, data
    if data.startswith(_ZIP_SIGNATURE):
        return _ooxml(data), data
    if data.startswith(_OLE_SIGNATURE):
        return _ole(data), data
    if data.startswith((_JPEG_SIGNATURE, _PNG_SIGNATURE)):
        return _image(data)
    if data.removeprefix(_UTF8_BOM).startswith(b"<?xml"):  # la declaración va al principio (XML 1.0 §2.8)
        _check_xml(data)
        return XML, data
    _reject("DOCUMENT_FORMAT_NOT_ALLOWED")


# ---------------------------------------------------------------- Word y Excel (Office Open XML: un ZIP)


def _ooxml(data: bytes) -> str:
    """DOCX o XLSX sin macros. Solo se lee el directorio del ZIP y `[Content_Types].xml` (con tope): nada más se
    descomprime (una "bomba ZIP" no cuesta nada aquí)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            if any(name.lower().rsplit("/", 1)[-1] == "vbaproject.bin" for name in names):
                _reject("DOCUMENT_MACROS_NOT_ALLOWED")
            if "[Content_Types].xml" not in names:
                _reject("DOCUMENT_FORMAT_NOT_ALLOWED")
            with archive.open("[Content_Types].xml") as member:
                content_types = member.read(_CONTENT_TYPES_MAX_BYTES + 1)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, RuntimeError, EOFError, NotImplementedError) as exc:
        raise UnprocessableError(code="DOCUMENT_DAMAGED", field="file") from exc
    declared = content_types.lower()
    if b"macroenabled" in declared:
        _reject("DOCUMENT_MACROS_NOT_ALLOWED")
    return next((kind for main, kind in _OOXML_MAIN if main in declared), None) or _reject(
        "DOCUMENT_FORMAT_NOT_ALLOWED"
    )


# ---------------------------------------------------------------- Word y Excel 97-2003 (OLE, Compound File Binary)


def _ole(data: bytes) -> str:
    """DOC o XLS sin proyecto de VBA (por las entradas de su directorio)."""
    entries = ole_entries(data)
    if any(name in _OLE_MACROS and kind in (_STORAGE, _STREAM) for name, kind in entries):
        _reject("DOCUMENT_MACROS_NOT_ALLOWED")
    streams = {name for name, kind in entries if kind == _STREAM}
    return next((kind for main, kind in _OLE_MAIN if main in streams), None) or _reject("DOCUMENT_FORMAT_NOT_ALLOWED")


def ole_entries(data: bytes) -> set[tuple[str, int]]:
    """(nombre, tipo) de cada entrada del directorio de un archivo OLE ([MS-CFB] §2): el encabezado dice el tamaño de
    sector y dónde empieza el directorio; la FAT (con sus sectores en el encabezado y en la cadena DIFAT) dice cómo
    sigue. Lectura acotada al archivo: un sector fuera de él o una cadena que se repite es un archivo dañado."""
    if len(data) < 512:
        _damaged()
    shift = _u16(data, 30)
    if shift not in (9, 12):  # sectores de 512 (versión 3) o 4096 bytes (versión 4)
        _damaged()
    size = 1 << shift
    total = (len(data) >> shift) + 1

    def sector(number: int) -> bytes:
        start = (number + 1) << shift
        chunk = data[start : start + size]
        if len(chunk) != size:
            _damaged()
        return chunk

    fat = _fat(data, sector, size, total)
    entries: set[tuple[str, int]] = set()
    current, visited = _u32(data, 48), set[int]()
    while current < _MAX_REGULAR_SECTOR:
        if current in visited or current >= len(fat):
            _damaged()
        visited.add(current)
        entries |= _directory_entries(sector(current))
        current = fat[current]
    return entries


def _fat(data: bytes, sector: Callable[[int], bytes], size: int, total: int) -> list[int]:
    """La tabla de asignación completa: sus sectores salen de los 109 del encabezado y de la cadena DIFAT."""
    per_sector = size // 4
    locations = list(struct.unpack_from("<109I", data, 76))
    following, steps = _u32(data, 68), 0
    while following < _MAX_REGULAR_SECTOR:
        steps += 1
        if steps > total:
            _damaged()
        values = struct.unpack(f"<{per_sector}I", sector(following))
        locations.extend(values[:-1])
        following = values[-1]
    fat: list[int] = []
    for location in locations:
        if location < _MAX_REGULAR_SECTOR:
            fat.extend(struct.unpack(f"<{per_sector}I", sector(location)))
    return fat


def _directory_entries(chunk: bytes) -> set[tuple[str, int]]:
    """Las entradas de un sector del directorio (128 bytes cada una: nombre UTF-16 de hasta 32 caracteres, su largo en
    bytes con el cero final y su tipo)."""
    found = set()
    for offset in range(0, len(chunk), _DIRECTORY_ENTRY_BYTES):
        entry = chunk[offset : offset + _DIRECTORY_ENTRY_BYTES]
        length, kind = _u16(entry, 64), entry[66]
        if kind in (_STORAGE, _STREAM) and 2 <= length <= 64 and length % 2 == 0:
            found.add((entry[: length - 2].decode("utf-16-le", errors="replace"), kind))
    return found


def _u16(data: bytes, offset: int) -> int:
    value: int = struct.unpack_from("<H", data, offset)[0]
    return value


def _u32(data: bytes, offset: int) -> int:
    value: int = struct.unpack_from("<I", data, offset)[0]
    return value


def _damaged() -> NoReturn:
    _reject("DOCUMENT_DAMAGED")


# ---------------------------------------------------------------- XML


class _UnsafeXml(Exception):
    """El XML declara un tipo de documento o entidades (DTD): no se acepta."""


def _forbid(*_args: object) -> NoReturn:
    raise _UnsafeXml


def _check_xml(data: bytes) -> None:
    """Analiza el XML completo sin DTD ni entidades (ver el docstring del módulo)."""
    parser = expat.ParserCreate()
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.StartDoctypeDeclHandler = _forbid
    parser.EntityDeclHandler = _forbid
    parser.UnparsedEntityDeclHandler = _forbid
    parser.ExternalEntityRefHandler = _forbid
    try:
        parser.Parse(data, True)
    except _UnsafeXml:
        _reject("DOCUMENT_XML_UNSAFE")
    except expat.ExpatError:
        _reject("DOCUMENT_DAMAGED")


# ---------------------------------------------------------------- imágenes (JPG y PNG)


def _image(data: bytes) -> tuple[str, bytes]:
    """La imagen vuelta a codificar desde sus píxeles: orientada y sin ningún metadato."""
    try:
        image = Image.open(io.BytesIO(data), formats=("JPEG", "PNG"))
    except Image.DecompressionBombError:
        _too_many_pixels()
    except UnidentifiedImageError, OSError, ValueError:
        _damaged()
    if image.width * image.height > settings.COMPANY_DOCUMENT_MAX_MEGAPIXELS * 1_000_000:
        _too_many_pixels()
    jpeg = image.format in _JPEG_FORMATS
    try:  # decodificar puede descubrir una imagen truncada o dañada después del encabezado
        oriented = ImageOps.exif_transpose(image)
        return (JPEG, _jpeg(oriented)) if jpeg else (PNG, _png(oriented))
    except OSError, ValueError:
        _damaged()


def _too_many_pixels() -> NoReturn:
    _reject("DOCUMENT_IMAGE_TOO_LARGE", params={"max": f"{settings.COMPANY_DOCUMENT_MAX_MEGAPIXELS:g}"})


def _jpeg(image: Image.Image) -> bytes:
    """JPEG sin metadatos: los colores de un perfil RGB llevados a sRGB y codificado solo desde los píxeles (en su
    mismo modo: gris, RGB o CMYK, los únicos que decodifica un JPEG)."""
    picture = to_srgb(image)
    picture.info = {}
    buffer = io.BytesIO()
    picture.save(buffer, "JPEG", quality=settings.COMPANY_DOCUMENT_JPEG_QUALITY, optimize=True)
    return buffer.getvalue()


def _png(image: Image.Image) -> bytes:
    """PNG sin metadatos (sin pérdida): solo los píxeles. Un color transparente (paleta, gris o RGB con `tRNS`) pasa a
    un canal alfa (RGBA): así se conserva aunque se descarte todo lo demás que la imagen arrastra."""
    picture = to_srgb(image)
    if "transparency" in picture.info:
        picture = picture.convert("RGBA")
    picture.info = {}
    buffer = io.BytesIO()
    picture.save(buffer, "PNG")
    return buffer.getvalue()


# ---------------------------------------------------------------- nombre


def clean_name(raw: str | None, content_type: str) -> str:
    """El nombre original sin carpetas, controles ni caracteres reservados, acotado y con la extensión de su formato
    real (ver el docstring del módulo)."""
    text = unicodedata.normalize("NFC", raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    text = "".join(" " if unicodedata.category(char) in ("Cc", "Cf") else char for char in text)
    text = " ".join(_RESERVED.sub(" ", text).split()).strip(" .")
    stem, dot, suffix = text.rpartition(".")
    if not dot:
        stem, suffix = text, ""
    accepted = EXTENSIONS[content_type]
    if suffix.lower() not in accepted:
        if suffix.lower() not in _KNOWN_EXTENSIONS:
            stem = text  # "contrato.v2" conserva su ".v2"
        suffix = accepted[0]
    stem = stem.rstrip(" .")[: DOCUMENT_FILE_NAME_MAX - len(suffix) - 1].rstrip(" .") or FALLBACK_NAME
    return f"{stem}.{suffix}"
