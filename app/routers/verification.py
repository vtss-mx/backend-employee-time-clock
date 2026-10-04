"""Verificación de identidad del EMPLOYEE autenticado (con su rostro).

El QR ya no se verifica aquí: es dinámico y vive en el teléfono del propio empleado, que lo
muestra a un validador (punto de control) para identificarse.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, File, Request, UploadFile

from app.core.responses import ApiResponse, ok
from app.dependencies import (
    CameraLabel,
    DbSession,
    EmployeeUser,
    Liveness,
    Pipeline,
    read_image_uploads,
    request_meta,
    require_screen,
    verification_rate_limit,
)
from app.models import Screen
from app.schemas.common import ErrorResponse
from app.schemas.verification import VerificationResult
from app.services.verification_service import VerificationService

router = APIRouter(
    prefix="/verification",
    tags=["Verificación (EMPLOYEE)"],
    dependencies=[Depends(require_screen(Screen.EMPLOYEE_VERIFY)), Depends(verification_rate_limit)],
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse, "description": "Rol sin permisos o registro facial no aprobado"},
        429: {"model": ErrorResponse},
    },
)


@router.post(
    "/face",
    response_model=ApiResponse[VerificationResult],
    summary="Verificar identidad por rostro (con prueba de vida)",
    description=(
        "Multipart:\n"
        "- `images`: 1 a 3 capturas frontales (se recomiendan 2).\n"
        "- `challenge_id`, `challenge_image` (una por movimiento) y `flash_image` (una por color del "
        "destello), en orden: el reto de `/api/face/challenge` y su respuesta (obligatorios si la "
        "empresa exige prueba de vida).\n\n"
        "Cada captura frontal se valida (un solo rostro, calidad, pose frontal, sin lentes, "
        "gorra ni cubrebocas → 422 con `code` y `details`). Después se valida la prueba de vida "
        "y cada captura debe superar FACE_MATCH_THRESHOLD contra los embeddings registrados."
    ),
    responses={409: {"model": ErrorResponse}, 413: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
def verify_face(
    request: Request,
    user: EmployeeUser,
    db: DbSession,
    pipeline: Pipeline,
    images: Annotated[list[UploadFile], File(description="Capturas frontales (JPEG/PNG/WEBP)")],
    liveness: Liveness,
    camera_label: CameraLabel = None,
) -> ApiResponse[VerificationResult]:
    frontal = read_image_uploads(images, max_files=3)
    ip, user_agent = request_meta(request)
    result = VerificationService(db, ip=ip, user_agent=user_agent).verify_face(
        user, frontal, pipeline, liveness=liveness, camera_label=camera_label
    )
    return _result(result)


def _result(result: VerificationResult) -> ApiResponse[VerificationResult]:
    # La solicitud se procesó correctamente (200) aunque la identidad no coincida:
    # `data.verified` indica el resultado y `code` permite distinguirlo sin leer `data`.
    return ok(result, result.message, code="IDENTITY_VERIFIED" if result.verified else "IDENTITY_NOT_VERIFIED")
