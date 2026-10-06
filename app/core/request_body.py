"""Cuerpo JSON acotado de las rutas públicas que reciben datos del navegador (fallas de la app y su rendimiento).

Se lee aquí (no lo lee FastAPI) para que el tope sea el de la ruta y no el general de la API (pensado para fotos):
un `Content-Length` mayor se rechaza sin leer nada y un envío por partes se corta en cuanto lo pasa (413). Un campo
inválido responde el mismo 422 `VALIDATION_ERROR` (por campo) que el resto de la API, y un texto con NUL u otro
control, el mismo 422 `INVALID_CHARACTERS` (`app/core/input_guard.py`).
"""

import json
from contextlib import suppress

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError

from app.core.exceptions import PayloadTooLargeError
from app.core.input_guard import ensure_valid_text
from app.i18n import Megabytes


def _too_large(limit: int) -> PayloadTooLargeError:
    return PayloadTooLargeError(key="REQUEST_TOO_LARGE", params={"size": Megabytes(limit)})


async def bounded_json[ModelT: BaseModel](request: Request, model: type[ModelT], limit: int) -> ModelT:
    """El cuerpo de la petición validado con `model`, sin leer más de `limit` bytes (413 con el tope en MB)."""
    if int(request.headers.get("content-length") or 0) > limit:
        raise _too_large(limit)
    raw = b""
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > limit:
            raise _too_large(limit)
    # La misma regla de texto que el resto de la API (`input_guard`: NUL, controles): FastAPI no leyó este cuerpo.
    data: object = None
    with suppress(ValueError):  # un JSON mal formado lo responde la validación de abajo (422 `json_invalid`)
        data = json.loads(raw)
    ensure_valid_text(data)
    try:
        return model.model_validate_json(raw)
    except ValidationError as exc:
        errors = [{**error, "loc": ("body", *error["loc"])} for error in exc.errors(include_url=False)]
        raise RequestValidationError(errors) from exc
