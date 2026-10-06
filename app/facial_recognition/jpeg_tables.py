"""Qué codificador produjo un JPEG, por su tabla de cuantización (señal JPEG_TABLE_UNKNOWN, docs/rd §2.5 y §4.2).

La app captura cada fotograma con `canvas.toBlob('image/jpeg', 0.92)`: en Chrome, Edge, Firefox y Android eso es
libjpeg(-turbo) con las tablas estándar del IJG escaladas a la calidad 92; Safari usa el codificador de Apple, con sus
propias tablas (siempre las mismas). Un programa que fabrica o reenvía capturas suele usar otro codificador u otra
calidad (PIL y OpenCV guardan con 75 o 95 por omisión) o mezcla imágenes de fuentes distintas en un mismo intento.

Solo se leen los encabezados (microsegundos, sin decodificar la imagen). Es una SEÑAL: un JPEG de otro codificador que
no es del IJG (Safari) no se marca; lo que sí, es una tabla IJG de otra calidad, una captura que no es JPEG o tablas
distintas dentro del mismo intento.
"""

from collections.abc import Sequence
from functools import cache

#: Tabla de luminancia estándar del JPEG (anexo K.1), en orden natural (fila por fila).
_STANDARD_LUMA = (
    16, 11, 10, 16, 24, 40, 51, 61,
    12, 12, 14, 19, 26, 58, 60, 55,
    14, 13, 16, 24, 40, 57, 69, 56,
    14, 17, 22, 29, 51, 87, 80, 62,
    18, 22, 37, 56, 68, 109, 103, 77,
    24, 35, 55, 64, 81, 104, 113, 92,
    49, 64, 78, 87, 103, 121, 120, 101,
    72, 92, 95, 98, 112, 100, 103, 99,
)  # fmt: skip
#: Orden zigzag: la posición natural de cada uno de los 64 valores que guarda el segmento DQT.
_ZIGZAG = (
    0, 1, 8, 16, 9, 2, 3, 10, 17, 24, 32, 25, 18, 11, 4, 5,
    12, 19, 26, 33, 40, 48, 41, 34, 27, 20, 13, 6, 7, 14, 21, 28,
    35, 42, 49, 56, 57, 50, 43, 36, 29, 22, 15, 23, 30, 37, 44, 51,
    58, 59, 52, 45, 38, 31, 39, 46, 53, 60, 61, 54, 47, 55, 62, 63,
)  # fmt: skip
#: Captura que no es un JPEG con tablas (o que no se pudo leer).
NOT_JPEG = 0
#: Marcadores sin longitud (inicio de imagen, reinicios, relleno).
_STANDALONE = frozenset({0xD8, 0x01, *range(0xD0, 0xD8)})
#: Inicio del barrido o fin de la imagen: después ya no hay tablas.
_NO_MORE_TABLES = frozenset({0xDA, 0xD9})


def ijg_table(quality: int) -> tuple[int, ...]:
    """La tabla de luminancia que escribe libjpeg con `jpeg_set_quality(quality)` (orden zigzag, línea base)."""
    scale = 5000 // quality if quality < 50 else 200 - quality * 2
    return tuple(min(255, max(1, (_STANDARD_LUMA[index] * scale + 50) // 100)) for index in _ZIGZAG)


@cache
def _ijg_qualities() -> dict[tuple[int, ...], int]:
    """Cada tabla del IJG con su calidad (la más alta si dos calidades dan la misma tabla)."""
    return {ijg_table(quality): quality for quality in range(1, 101)}


def luma_table(data: bytes) -> tuple[int, ...] | None:
    """La tabla 0 (luminancia) del JPEG, en orden zigzag; None si no es un JPEG o no la trae antes del barrido."""
    if not data.startswith(b"\xff\xd8"):
        return None
    position = 2
    while position + 4 <= len(data):
        if data[position] != 0xFF:
            return None
        marker = data[position + 1]
        if marker == 0xFF or marker in _STANDALONE:
            position += 1 if marker == 0xFF else 2
            continue
        if marker in _NO_MORE_TABLES:
            return None
        length = int.from_bytes(data[position + 2 : position + 4], "big")
        if marker == 0xDB:
            table = _table_zero(data[position + 4 : position + 2 + length])
            if table is not None:
                return table
        position += 2 + length
    return None


def _table_zero(segment: bytes) -> tuple[int, ...] | None:
    """La tabla 0 de un segmento DQT (puede traer varias, de 8 o de 16 bits)."""
    index = 0
    while index < len(segment):
        precision, table_id = segment[index] >> 4, segment[index] & 0x0F
        width = 2 if precision else 1
        body = segment[index + 1 : index + 1 + 64 * width]
        if len(body) < 64 * width:
            return None
        if table_id == 0:
            return tuple(int.from_bytes(body[i : i + width], "big") for i in range(0, len(body), width))
        index += 1 + 64 * width
    return None


def encoder_quality(data: bytes) -> int | None:
    """La calidad IJG (1-100) del JPEG; `NOT_JPEG` (0) si no es un JPEG con tabla; None si la tabla es de otro
    codificador que no es el del IJG (p. ej. el de Apple en Safari)."""
    table = luma_table(data)
    if table is None:
        return NOT_JPEG
    return _ijg_qualities().get(table)


def unexpected(qualities: Sequence[int | None], expected: int) -> tuple[bool, int | None]:
    """¿Las capturas de un intento delatan otro codificador? (y la calidad que se encontró, si es una sola).

    Sí, si mezclan tablas distintas (imágenes de fuentes diferentes), si alguna no es un JPEG o si todas son del IJG
    con una calidad distinta a la de la app. Un codificador ajeno al IJG pero el MISMO en todas (Safari) no se marca.
    """
    found = set(qualities)
    if not found:
        return False, None
    if len(found) > 1:
        return True, None
    quality = next(iter(found))
    return quality is not None and quality != expected, quality
