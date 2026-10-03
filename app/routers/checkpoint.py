"""Punto de control (rol VALIDATOR, tableta o teléfono): identifica a los empleados de su empresa."""

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile

from app.core.responses import ApiResponse, ok
from app.dependencies import (
    CameraLabel,
    ChallengeImages,
    DbSession,
    Pipeline,
    ValidatorUser,
    checkpoint_rate_limit,
    read_challenge_images,
    read_image_uploads,
    request_meta,
    require_screen,
)
from app.models import Screen
from app.schemas.checkpoint import CheckpointEmployee, CheckpointEvent, CheckpointProfile, CheckpointQrRequest
from app.schemas.common import ErrorResponse
from app.schemas.verification import VerificationResult
from app.services.checkpoint_service import CheckpointService

router = APIRouter(
    dependencies=[Depends(require_screen(Screen.VALIDATOR_CHECKPOINT))],
    prefix="/checkpoint",
    tags=["Punto de control (VALIDATOR)"],
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse, "description": "Rol sin permisos o dispositivo no permitido"},
        409: {"model": ErrorResponse, "description": "Método no permitido para este validador"},
        429: {"model": ErrorResponse},
    },
)


def _service(request: Request, db: DbSession, user: ValidatorUser) -> CheckpointService:
    ip, user_agent = request_meta(request)
    return CheckpointService(db, user, ip=ip, user_agent=user_agent)


Checkpoint = Annotated[CheckpointService, Depends(_service)]


def _identified(result: VerificationResult) -> ApiResponse[VerificationResult]:
    if result.verified:
        return ok(result, result.message, code="EMPLOYEE_IDENTIFIED")
    return ok(result, result.message, code="EMPLOYEE_NOT_IDENTIFIED")


@router.get("/me", response_model=ApiResponse[CheckpointProfile], summary="Configuración del validador")
def checkpoint_profile(checkpoint: Checkpoint) -> ApiResponse[CheckpointProfile]:
    return ok(checkpoint.profile(), "Validador", code="CHECKPOINT_PROFILE")


@router.get(
    "/recent",
    response_model=ApiResponse[list[CheckpointEvent]],
    summary="Últimas identificaciones de este validador",
)
def checkpoint_recent(
    checkpoint: Checkpoint, limit: Annotated[int, Query(ge=1, le=50)] = 10
) -> ApiResponse[list[CheckpointEvent]]:
    return ok(checkpoint.recent(limit), "Identificaciones recientes", code="CHECKPOINT_RECENT")


@router.post(
    "/qr/inspect",
    response_model=ApiResponse[CheckpointEmployee],
    summary="QR + rostro: de quién es el código QR (paso 1; no registra)",
    responses={422: {"model": ErrorResponse}},
    dependencies=[Depends(checkpoint_rate_limit)],
)
def inspect_qr(payload: CheckpointQrRequest, checkpoint: Checkpoint) -> ApiResponse[CheckpointEmployee]:
    employee = checkpoint.inspect_qr(payload.qr_content)
    return ok(employee, f"Ahora valida el rostro de {employee.name}", code="QR_HOLDER_FOUND")


@router.post(
    "/identify/qr",
    response_model=ApiResponse[VerificationResult],
    summary="Identificar a un empleado por su código QR",
    description="El QR debe ser de un empleado activo de la empresa del validador. Siempre 200: ver `verified`.",
    dependencies=[Depends(checkpoint_rate_limit)],
)
def identify_by_qr(payload: CheckpointQrRequest, checkpoint: Checkpoint) -> ApiResponse[VerificationResult]:
    return _identified(checkpoint.identify_qr(payload.qr_content))


@router.post(
    "/identify/face",
    response_model=ApiResponse[VerificationResult],
    summary="Identificar a un empleado por su rostro (1:N) o confirmar el rostro del dueño de un QR",
    description=(
        "Multipart: `images` (1 a 3 capturas frontales), `challenge_id` + `challenge_image` (reto de "
        "`/api/face/challenge`, si la empresa exige prueba de vida) y, en el modo QR_AND_FACE, "
        "`qr_content`. Sin QR se busca a la persona entre los empleados de la empresa con identidad "
        "validada: todas las capturas deben señalar a la misma persona, alcanzar el nivel de "
        "confianza de la empresa y superar a la segunda más parecida por FACE_IDENTIFY_MARGIN."
    ),
    responses={413: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
    dependencies=[Depends(checkpoint_rate_limit)],
)
def identify_by_face(
    checkpoint: Checkpoint,
    pipeline: Pipeline,
    images: Annotated[list[UploadFile], File(description="Capturas frontales (JPEG/PNG/WEBP)")],
    challenge_id: Annotated[str | None, Form(max_length=100)] = None,
    challenge_image: ChallengeImages = None,
    qr_content: Annotated[str | None, Form(max_length=512)] = None,
    camera_label: CameraLabel = None,
) -> ApiResponse[VerificationResult]:
    result = checkpoint.identify_face(
        pipeline,
        read_image_uploads(images, max_files=3),
        challenge_id=challenge_id,
        challenge_images=read_challenge_images(challenge_image),
        qr_content=qr_content or None,
        camera_label=camera_label,
    )
    return _identified(result)
