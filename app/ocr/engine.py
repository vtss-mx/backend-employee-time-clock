"""Reconocimiento óptico de caracteres EN ESTE SERVIDOR con Tesseract (regla 13 de la raíz: los datos de la empresa
nunca salen a un servicio externo).

Decisión del dueño del producto (2026-10-07): la información de los documentos del empleado (pasaporte, INE,
licencia...) se extrae con OCR de mejor esfuerzo y la empresa confirma o corrige. Tesseract es un motor abierto (Apache
2.0) que corre en el propio servidor; su binario y los datos de cada idioma se instalan AL CONSTRUIR la imagen (apt:
`tesseract-ocr` y `tesseract-ocr-<idioma>`), nunca con una descarga en la petición (como el modelo de voz).

Diferencias con el motor facial y el de voz: Tesseract NO carga un modelo en memoria del proceso; es un subproceso con
su propio tiempo límite (`OCR_TIMEOUT_SECONDS`), así que no hace falta un «holder» ni una pausa tras falla. La lectura
corre en el hilo de la petición (las rutas que suben un documento son `def` síncronas: FastAPI las atiende en su pool
de hilos, «el pool de siempre»), SIN transacción abierta (el servicio confirma la lectura antes de llamar aquí) y
medida (`observed("ocr.extract")`).

`read_text` es la pieza que el servicio usa a través de `app/ocr/__init__.py` (`backend().read_text`); la extracción de
campos es pura (`fields.py`, `mrz.py`). Las pruebas reemplazan el motor con `use_backend` (`FakeOcr`): ningún caso de la
suite ejecuta Tesseract (el motor real tiene las suyas, `tests/test_ocr_real.py`).
"""

import io
import logging
from dataclasses import dataclass
from typing import Any

import pytesseract
from PIL import Image, ImageOps, UnidentifiedImageError

from app.core.config import settings
from app.core.observability import observed

logger = logging.getLogger(__name__)


class OcrUnavailable(RuntimeError):
    """Tesseract no está instalado o falló: el resto de la API sigue operando y el servicio degrada a sin texto
    (mejor esfuerzo; nunca bloquea el registro del documento)."""


@dataclass(frozen=True)
class OcrText:
    """Lo que leyó el motor: el texto completo y la confianza media de sus palabras (0-1)."""

    text: str
    confidence: float


@observed("ocr.extract")
def read_text(image: bytes, languages: str) -> OcrText:
    """Texto y confianza de una imagen (JPG o PNG ya limpia por `document_files.inspect`) con Tesseract, en
    `languages` (códigos de Tesseract unidos por `+`, p. ej. `spa+eng`). Tope de megapíxeles antes de decodificar y
    tiempo límite del subproceso. `OcrUnavailable` si la imagen es demasiado grande, no se decodifica o el motor
    falla: el servicio lo convierte en texto vacío (la empresa captura a mano)."""
    try:
        picture = Image.open(io.BytesIO(image))
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise OcrUnavailable(f"La imagen no se pudo abrir para OCR: {type(exc).__name__}") from exc
    with picture:
        if picture.width * picture.height > settings.OCR_MAX_MEGAPIXELS * 1_000_000:
            raise OcrUnavailable("La imagen supera el tope de megapíxeles para OCR")
        grayscale = ImageOps.grayscale(picture)
    try:
        data = pytesseract.image_to_data(
            grayscale,
            lang=languages,
            output_type=pytesseract.Output.DICT,
            timeout=settings.OCR_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # el binario ausente, un idioma sin datos o el tiempo agotado: todo es mejor esfuerzo
        raise OcrUnavailable(f"Tesseract no pudo leer el documento: {type(exc).__name__}") from exc
    return _assemble(data)


def _assemble(data: dict[str, list[Any]]) -> OcrText:
    """Une las palabras que Tesseract reconoció (las de confianza negativa son relleno) y promedia su confianza."""
    words: list[str] = []
    confidences: list[float] = []
    for text, raw_conf in zip(data.get("text", []), data.get("conf", []), strict=False):
        word = str(text).strip()
        try:
            conf = float(raw_conf)
        except TypeError, ValueError:
            conf = -1.0
        if word and conf >= 0:
            words.append(word)
            confidences.append(conf)
    mean = sum(confidences) / len(confidences) / 100.0 if confidences else 0.0
    return OcrText(text="\n".join(words), confidence=round(mean, 4))
