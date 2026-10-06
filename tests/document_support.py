"""Archivos de prueba de los documentos de la empresa (sin archivos binarios en el repositorio: cada uno se arma aquí).

- PDF, XML (un CFDI mínimo y sus variantes peligrosas o dañadas), imágenes JPG/PNG (con EXIF y GPS para probar que se
  quitan) y Word/Excel modernos (un ZIP con su `[Content_Types].xml`, con o sin macros).
- Word/Excel 97-2003: `ole(...)` arma un archivo OLE ([MS-CFB]) válido con las entradas de directorio que se pidan
  (con sectores de 512 o 4096 bytes y, si se pide, su FAT listada en la cadena DIFAT). El lector del servidor se probó
  además con libros XLS reales de 1 MB y de 45 MB (con 5 sectores DIFAT) generados por otra biblioteca.
"""

import io
import struct
import zipfile

from PIL import Image, PngImagePlugin

PDF = b"%PDF-1.7\n1 0 obj << /Type /Catalog >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n"
CFDI = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<cfdi:Comprobante xmlns:cfdi="http://www.sat.gob.mx/cfd/4" Version="4.0" Total="116.00">'
    b'<cfdi:Emisor Rfc="PNO120315AB1" Nombre="Panificadora &amp; Hijos"/></cfdi:Comprobante>\n'
)
#: La "bomba de mil millones de risas": entidades que se expanden unas a otras (necesita un DTD).
XML_BOMB = (
    b'<?xml version="1.0"?>\n<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;">]>\n'
    b"<lolz>&lol2;</lolz>\n"
)
#: Entidad externa: leería un archivo del servidor si el analizador la resolviera.
XML_EXTERNAL = b'<?xml version="1.0"?>\n<!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>\n<r>&x;</r>\n'
XML_BROKEN = b'<?xml version="1.0"?>\n<r><sin-cerrar></r>\n'

WORD_MAIN = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
EXCEL_MAIN = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
WORD_MACROS = "application/vnd.ms-word.document.macroEnabled.main+xml"
SLIDES_MAIN = "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"

FREESECT, ENDOFCHAIN, FATSECT, DIFSECT = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFC
STORAGE, STREAM, ROOT = 1, 2, 5


def ooxml(main: str, part: str = "word/document.xml", *, extra: dict[str, bytes] | None = None) -> bytes:
    """Un DOCX/XLSX mínimo: `[Content_Types].xml` con su parte principal y esa parte."""
    types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        f'<Override PartName="/{part}" ContentType="{main}"/></Types>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", types)
        archive.writestr(part, "<document>hola</document>")
        for name, content in (extra or {}).items():
            archive.writestr(name, content)
    return buffer.getvalue()


def docx(**kwargs) -> bytes:
    return ooxml(WORD_MAIN, **kwargs)


def xlsx() -> bytes:
    return ooxml(EXCEL_MAIN, "xl/workbook.xml")


def plain_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("leeme.txt", "hola")
    return buffer.getvalue()


def _entry(name: str, kind: int, *, right: int = FREESECT, child: int = FREESECT) -> bytes:
    """Una entrada del directorio (128 bytes): nombre UTF-16 con su largo, tipo y sus enlaces del árbol (hermano
    izquierdo, derecho e hijo; FREESECT = ninguno)."""
    encoded = name.encode("utf-16-le") + b"\x00\x00"
    entry = bytearray(128)
    entry[: len(encoded)] = encoded
    struct.pack_into("<HBBIII", entry, 64, len(encoded), kind, 1, FREESECT, right, child)
    return bytes(entry)


def ole(entries: list[tuple[str, int]], *, shift: int = 9, difat: bool = False) -> bytes:
    """Un archivo OLE con la entrada raíz y `entries` en su directorio (sin contenido en los flujos: el servidor solo
    lee el directorio). Sector 0 = FAT, después el directorio y, con `difat`, un sector DIFAT al final que lista la FAT
    (en lugar del encabezado)."""
    size = 1 << shift
    per_sector = size // 128
    last = len(entries)
    directory = [
        _entry("Root Entry", ROOT, child=1 if entries else FREESECT),
        *(_entry(name, kind, right=i + 2 if i + 1 < last else FREESECT) for i, (name, kind) in enumerate(entries)),
    ]
    sectors = [
        b"".join(directory[i : i + per_sector]).ljust(size, b"\x00") for i in range(0, len(directory), per_sector)
    ]
    fat = [FATSECT, *(index + 1 for index in range(1, len(sectors))), ENDOFCHAIN]
    if difat:
        fat.append(DIFSECT)
    fat += [FREESECT] * (size // 4 - len(fat))
    header = bytearray(512)
    header[:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    struct.pack_into("<HHHHH", header, 24, 0x3E, 3 if shift == 9 else 4, 0xFFFE, shift, 6)
    struct.pack_into("<IIIIIIII", header, 44, 1, 1, 0, 4096, ENDOFCHAIN, 0, ENDOFCHAIN, 0)
    locations = [FREESECT] * 109
    if difat:
        struct.pack_into("<II", header, 68, len(sectors) + 1, 1)
    else:
        locations[0] = 0
    struct.pack_into("<109I", header, 76, *locations)
    body = [struct.pack(f"<{size // 4}I", *fat), *sectors]
    if difat:
        body.append(struct.pack(f"<{size // 4}I", 0, *([FREESECT] * (size // 4 - 2)), ENDOFCHAIN))
    return bytes(header).ljust(size, b"\x00") + b"".join(body)


def doc(*extra: tuple[str, int]) -> bytes:
    return ole([("WordDocument", STREAM), ("1Table", STREAM), *extra])


def xls(*extra: tuple[str, int]) -> bytes:
    return ole([("Workbook", STREAM), *extra])


def _gps_exif(orientation: int) -> Image.Exif:
    exif = Image.Exif()
    exif[0x0112] = orientation  # orientación
    exif[0x010F] = "Fabricante"  # marca de la cámara
    exif.get_ifd(0x8825)[2] = (19.0, 25.0, 10.0)  # latitud GPS
    return exif


def jpeg(*, size: tuple[int, int] = (40, 20), orientation: int = 6, noisy: bool = False) -> bytes:
    """Una foto JPG con EXIF (orientación girada, cámara y ubicación GPS) y un comentario; con `noisy`, píxeles al azar
    (sus datos comprimidos ocupan casi todo el archivo: truncarlo deja el encabezado completo)."""
    image = Image.effect_noise(size, 64).convert("RGB") if noisy else Image.new("RGB", size, (200, 30, 30))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", exif=_gps_exif(orientation), comment=b"secreto")
    return buffer.getvalue()


def png(*, mode: str = "RGB", transparent: bool = False) -> bytes:
    """Un PNG con EXIF (GPS) y un texto; con `transparent`, una paleta de dos colores cuyo primero (el fondo) es
    transparente."""
    image = Image.new(mode, (30, 30), 0 if mode == "P" else (10, 20, 30))
    if mode == "P":
        image.putpalette([255, 0, 0, 0, 0, 255])
        image.putpixel((1, 1), 1)
    text = PngImagePlugin.PngInfo()
    text.add_text("Comment", "secreto")
    info: dict = {"exif": _gps_exif(1), "pnginfo": text}
    if transparent:
        info["transparency"] = 0
    buffer = io.BytesIO()
    image.save(buffer, "PNG", **info)
    return buffer.getvalue()
