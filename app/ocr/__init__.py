"""OCR de los documentos del empleado EN ESTE SERVIDOR (regla 13 de la raíz; decisión del dueño del producto,
2026-10-07): se extrae la información de mejor esfuerzo y la EMPRESA confirma o corrige; una lectura imperfecta NUNCA
bloquea.

`backend()` es la pieza que el servicio usa (`backend().read_text`): Tesseract (`engine`). La extracción de campos es
pura (`fields` + `mrz`). Las pruebas reemplazan el motor con `use_backend` (como el bucket con `use_storage` y la voz
con `use_backend`), así ningún caso de la suite ejecuta Tesseract; el motor real
tiene las suyas (`tests/test_ocr_real.py`).

`extract(...)` orquesta lo anterior y NUNCA lanza: si el motor no está disponible o falla, devuelve un resultado vacío
(`available=False`) y lo registra. El servicio guarda el documento igual y la empresa captura los datos a mano.
"""

import logging
from dataclasses import dataclass, field
from typing import Protocol

from app.ocr.engine import OcrText, OcrUnavailable, read_text
from app.ocr.fields import (
    DRIVER_LICENSE,
    MRZ_TYPES,
    NATIONAL_ID,
    OFFICIAL_ID_TYPES,
    OTHER_OFFICIAL_ID,
    PASSPORT,
    PROOF_OF_ADDRESS,
    extract_fields,
)

logger = logging.getLogger(__name__)


class OcrBackend(Protocol):
    def read_text(self, image: bytes, languages: str) -> OcrText: ...


class RealBackend:
    """Tesseract (subproceso con su tiempo límite; los datos de idioma vienen en la imagen)."""

    def read_text(self, image: bytes, languages: str) -> OcrText:
        return read_text(image, languages)


_backend: OcrBackend = RealBackend()


def backend() -> OcrBackend:
    return _backend


def use_backend(replacement: OcrBackend | None) -> None:
    """Reemplaza el motor (pruebas); None vuelve al real."""
    global _backend
    _backend = replacement if replacement is not None else RealBackend()


@dataclass(frozen=True)
class OcrResult:
    """Lo que el OCR aportó de un documento (mejor esfuerzo): el texto reconocido, la confianza media, los campos
    estructurados (las columnas del expediente, como texto editable), si una MRZ cuadró sus dígitos verificadores y si
    el motor estuvo disponible. `available=False` = no se leyó nada (la empresa captura a mano)."""

    document_type: str
    text: str = ""
    confidence: float = 0.0
    fields: dict[str, str] = field(default_factory=dict)
    mrz_verified: bool = False
    available: bool = False


def extract(document_type: str, image: bytes, languages: str) -> OcrResult:
    """Lee la imagen y extrae sus campos. Mejor esfuerzo: nunca lanza (un fallo del motor se registra y devuelve un
    resultado vacío). Solo debe llamarse con una imagen (el servicio no llama al OCR con un PDF o un Word)."""
    try:
        read = backend().read_text(image, languages)
    except OcrUnavailable as exc:
        logger.warning("OCR no disponible para un documento (%s): %s", document_type, exc)
        return OcrResult(document_type=document_type)
    detected, mrz = extract_fields(document_type, read.text)
    return OcrResult(
        document_type=document_type,
        text=read.text,
        confidence=read.confidence,
        fields=detected,
        mrz_verified=bool(mrz is not None and mrz.verified),
        available=True,
    )


__all__ = [
    "DRIVER_LICENSE",
    "MRZ_TYPES",
    "NATIONAL_ID",
    "OFFICIAL_ID_TYPES",
    "OTHER_OFFICIAL_ID",
    "PASSPORT",
    "PROOF_OF_ADDRESS",
    "OcrBackend",
    "OcrResult",
    "OcrText",
    "OcrUnavailable",
    "RealBackend",
    "backend",
    "extract",
    "extract_fields",
    "read_text",
    "use_backend",
]
