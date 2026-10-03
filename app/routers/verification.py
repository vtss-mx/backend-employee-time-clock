"""Verificación de identidad del EMPLOYEE autenticado. Métodos independientes."""

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile

from app.core.responses import ApiResponse, ok
from app.dependencies import (
    CameraLabel,
    ChallengeImages,
    DbSession,
    EmployeeUser,
    Pipeline,
    read_challenge_images,
    read_image_uploads,
    request_meta,
    require_screen,
    verification_rate_limit,
)
from app.models import Screen
from app.schemas.common import ErrorResponse
from app.schemas.verification import QrVerificationRequest, VerificationResult
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
        "- `challenge_id` + `challenge_image` (una por giro, en orden): reto de `/api/face/challenge` y "
        "la captura con la cabeza girada (obligatorios si FACE_LIVENESS_ENABLED=true).\n\n"
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
    challenge_id: Annotated[str | None, Form(max_length=100)] = None,
    challenge_image: ChallengeImages = None,
    camera_label: CameraLabel = None,
) -> ApiResponse[VerificationResult]:
    frontal = read_image_uploads(images, max_files=3)
    turns = read_challenge_images(challenge_image)
    ip, user_agent = request_meta(request)
    result = VerificationService(db, ip=ip, user_agent=user_agent).verify_face(
        user, frontal, pipeline, challenge_id=challenge_id, challenge_images=turns, camera_label=camera_label
    )
    return _result(result)


@router.post("/qr", response_model=ApiResponse[VerificationResult], summary="Verificar identidad por QR")
def verify_qr(
    request: Request, payload: QrVerificationRequest, user: EmployeeUser, db: DbSession
) -> ApiResponse[VerificationResult]:
    ip, user_agent = request_meta(request)
    return _result(VerificationService(db, ip=ip, user_agent=user_agent).verify_qr(user, payload.qr_content))


def _result(result: VerificationResult) -> ApiResponse[VerificationResult]:
    # La solicitud se procesó correctamente (200) aunque la identidad no coincida:
    # `data.verified` indica el resultado y `code` permite distinguirlo sin leer `data`.
    return ok(result, result.message, code="IDENTITY_VERIFIED" if result.verified else "IDENTITY_NOT_VERIFIED")
