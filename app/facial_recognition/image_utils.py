"""Decodificación y validación segura de imágenes recibidas."""

import io
import warnings

import numpy as np
from PIL import Image, ImageOps

from app.facial_recognition.errors import FaceValidationError

ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}
ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp"}
#: Lado máximo con el que se procesa la imagen (reduce costo de CPU).
PROCESSING_MAX_SIDE = 1280


def decode_image(data: bytes, *, min_dimension: int, max_dimension: int) -> np.ndarray:
    """Valida formato/tamaño y devuelve la imagen en BGR (uint8) lista para OpenCV."""
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
