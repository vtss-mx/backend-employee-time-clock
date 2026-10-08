"""Decodificación y validación segura de imágenes recibidas."""

import io
import logging
import warnings

import numpy as np
from PIL import Image, ImageOps

from app.facial_recognition.errors import FaceValidationError

ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}
ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp"}
#: Lado máximo con el que se procesa la imagen (reduce costo de CPU).
PROCESSING_MAX_SIDE = 1280

logger = logging.getLogger(__name__)

#: Bloque principal (TIFF): Make y Model (la cámara que tomó la foto), Artist y Copyright (un autor). La captura de la
#: app debe pasar en CUALQUIER navegador de cualquier dispositivo (decisión del dueño del producto): DateTime, Software,
#: orientación y resolución NO cuentan, porque un navegador o el sistema pueden escribirlas al codificar el lienzo y no
#: prueban nada por sí solas (una foto de galería trae además la cámara o los datos de la toma, que sí se revisan).
_FOREIGN_IFD0_TAGS = frozenset({0x010F, 0x0110, 0x013B, 0x8298})
#: Bloque GPS: solo lo escribe una cámara.
_GPS_IFD = 0x8825
#: Bloque Exif (0x8769). Su sola presencia NO delata una cámara: Safari en iPhone codifica el lienzo de la captura con
#: un bloque Exif propio (espacio de color y tamaño en píxeles) y la regla anterior, que rechazaba el bloque entero,
#: dejaba sin registro facial a todo iPhone (IMAGE_NOT_FROM_CAMERA en cada captura, 2026-10-06). Delata una TOMA real
#: lo que solo escribe una cámara o un editor: fechas de la toma, exposición, apertura, ISO, distancia focal, nota del
#: fabricante, comentario (las capturas de pantalla del iPhone), lente y número de serie.
_EXIF_IFD = 0x8769
_SHOT_EXIF_TAGS = frozenset({0x9003, 0x9004, 0x829A, 0x829D, 0x8827, 0x920A, 0x927C, 0x9286, 0xA431, 0xA433, 0xA434})


def foreign_metadata(image: Image.Image) -> list[str]:
    """Las etiquetas que delatan una cámara o un editor (vacía: la tomó la app en vivo). Solo códigos, nunca valores."""
    exif = image.getexif()
    found = [f"{tag:#06x}" for tag in sorted(_FOREIGN_IFD0_TAGS.intersection(exif))]
    if _GPS_IFD in exif:
        found.append("gps")
    found += [f"exif:{tag:#06x}" for tag in sorted(_SHOT_EXIF_TAGS.intersection(exif.get_ifd(_EXIF_IFD)))]
    return found


def decode_image(data: bytes, *, min_dimension: int, max_dimension: int, reject_foreign: bool = False) -> np.ndarray:
    """Valida formato/tamaño y devuelve la imagen en BGR (uint8) lista para OpenCV.

    reject_foreign: rechaza imágenes con metadatos de cámara o de edición (IMAGE_NOT_FROM_CAMERA),
    una foto de la galería o un archivo inyectado en lugar de la captura en vivo.
    """
    if not data:
        raise FaceValidationError("EMPTY_IMAGE")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            source = Image.open(io.BytesIO(data))
            # El formato se determina por el contenido real (magic bytes), no por la extensión.
            if source.format not in ALLOWED_FORMATS:
                raise FaceValidationError("INVALID_IMAGE_FORMAT")
            width, height = source.size
            if max(width, height) > max_dimension:
                raise FaceValidationError("IMAGE_TOO_LARGE", {"max_dimension": max_dimension})
            if min(width, height) < min_dimension:
                raise FaceValidationError("IMAGE_TOO_SMALL", {"min_dimension": min_dimension})
            if reject_foreign and (tags := foreign_metadata(source)):
                # Solo los códigos de las etiquetas (nunca sus valores): sirve para revisar un rechazo de un navegador.
                logger.info("Captura con metadatos de cámara o editor: %s", ", ".join(tags))
                raise FaceValidationError("IMAGE_NOT_FROM_CAMERA")
            oriented = ImageOps.exif_transpose(source) or source  # fotos de móvil con orientación EXIF
            image: Image.Image = oriented.convert("RGB")
            image.thumbnail((PROCESSING_MAX_SIDE, PROCESSING_MAX_SIDE))
    except FaceValidationError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise FaceValidationError("IMAGE_TOO_LARGE", {"max_dimension": max_dimension}) from exc
    except Exception as exc:
        raise FaceValidationError("INVALID_IMAGE") from exc

    rgb = np.asarray(image, dtype=np.uint8)
    return np.ascontiguousarray(rgb[:, :, ::-1])  # RGB -> BGR
