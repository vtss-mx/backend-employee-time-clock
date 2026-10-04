"""Validación en vivo por HTTP: respaldo del canal WebSocket `/api/ws/validation`.

Misma regla y mismos permisos que el canal (app/services/live_validation.py), para clientes
donde un proxy bloquea los WebSockets.
"""

from typing import Annotated

from fastapi import APIRouter, Query

from app.core.config import settings
from app.core.responses import ApiResponse, ok
from app.dependencies import CurrentUser, DbSession
from app.middleware.rate_limit import enforce
from app.schemas.common import ErrorResponse
from app.services.live_validation import validate_field

router = APIRouter(
    prefix="/validation",
    tags=["Validación en vivo"],
    responses={
        401: {"model": ErrorResponse, "description": "No autenticado"},
        403: {"model": ErrorResponse, "description": "El rol no tiene la pantalla que usa el campo"},
    },
)


@router.get(
    "", response_model=ApiResponse[dict], summary="Validar un campo mientras se escribe (formato y disponibilidad)"
)
def validate(
    user: CurrentUser,
    db: DbSession,
    field: Annotated[str, Query(max_length=40, description="Campo (p. ej. email, phone, company_rfc)")],
    value: Annotated[str, Query(max_length=255)] = "",
    exclude_id: Annotated[int | None, Query(description="Al editar: id del registro (su valor no cuenta)")] = None,
    related: Annotated[
        str | None, Query(max_length=255, description="Valor relacionado (el correo, al validar el teléfono)")
    ] = None,
) -> ApiResponse[dict]:
    # Cada consulta pregunta por correos/teléfonos de toda la plataforma: con límite por usuario.
    enforce(f"validation:user:{user.id}", settings.RATE_LIMIT_VALIDATION_PER_MINUTE)
    result = validate_field(db, user, field, value, exclude_id, related)
    return ok(result.as_dict(), result.message, code=result.code)
