"""Seguridad facial automática de la plataforma (solo el ADMIN): umbrales autocalibrados, empresas
reforzadas por ataques y lo medido del destello de colores."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends

from app.core.responses import ApiResponse, ok
from app.dependencies import AdminUser, DbSession, require_screen
from app.models import Screen
from app.schemas.common import ErrorResponse
from app.schemas.face_security import FaceSecurityOverview
from app.services import face_security

router = APIRouter(
    prefix="/admin/face-security",
    tags=["Seguridad facial (ADMIN)"],
    dependencies=[Depends(require_screen(Screen.ADMIN_FACE_SECURITY))],
    responses={
        401: {"model": ErrorResponse, "description": "No autenticado"},
        403: {"model": ErrorResponse, "description": "Solo el ADMIN de la plataforma"},
    },
)


@router.get(
    "",
    response_model=ApiResponse[FaceSecurityOverview],
    summary="Umbrales autocalibrados, empresas reforzadas y mediciones del destello",
)
def overview(_: AdminUser, db: DbSession) -> ApiResponse[FaceSecurityOverview]:
    return ok(face_security.overview(db, datetime.now(UTC)))


@router.post(
    "/recalibrate",
    response_model=ApiResponse[FaceSecurityOverview],
    summary="Recalcular ahora los umbrales con las mediciones recientes",
    description=(
        "Lo mismo que hace el mantenimiento cada FACE_AUTOCALIBRATION_INTERVAL_HOURS (idempotente). Solo "
        "endurece: ningún umbral baja del mínimo de la configuración ni sube de su tope."
    ),
)
def recalibrate(_: AdminUser, db: DbSession) -> ApiResponse[FaceSecurityOverview]:
    now = datetime.now(UTC)
    changed = face_security.recalibrate(db, now)
    message = f"Umbrales recalculados ({changed} cambiaron)" if changed else "Umbrales recalculados: sin cambios"
    return ok(face_security.overview(db, now), message, code="THRESHOLDS_RECALIBRATED")
