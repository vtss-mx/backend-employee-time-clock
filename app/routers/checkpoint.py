"""Punto de control (rol VALIDATOR, tableta o teléfono): identifica a los empleados de su empresa."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile

from app.core.responses import ApiResponse, ok
from app.dependencies import (
    CameraLabel,
    DbSession,
    Liveness,
    OperatorClient,
    Pagination,
    Pipeline,
    ValidatorRequest,
    ValidatorUser,
    checkpoint_rate_limit,
    read_image_uploads,
    request_meta,
    require_screen,
)
from app.models import Screen
from app.schemas.checkpoint import (
    CheckpointEmployee,
    CheckpointEventList,
    CheckpointProfile,
    CheckpointQrRequest,
    SignedRequest,
)
from app.schemas.common import ErrorResponse
from app.schemas.verification import VerificationResult
from app.services.checkpoint_service import CheckpointService
from app.services.request_signing import RequestProof
from app.services.validator_presence import IdentificationRequest

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
    # La sesión de la petición: su llave de dispositivo es la que debe firmar cada identificación (antifraude 2b).
    return CheckpointService(db, user, ip=ip, user_agent=user_agent, session_id=request.state.session_id)


Checkpoint = Annotated[CheckpointService, Depends(_service)]


def _signed(body: SignedRequest) -> IdentificationRequest:
    """La firma por petición y la ubicación que trae un cuerpo JSON (identificar o revisar un QR)."""
    proof = RequestProof(body.signature_key, body.signature_nonce, body.signature)
    return IdentificationRequest(proof, body.location, tuple(body.location_samples))


#: Respuestas de la firma por petición y de la ubicación de cada identificación (solo si la empresa las exige).
PRESENCE_ERRORS: dict[int | str, dict[str, Any]] = {
    403: {
        "model": ErrorResponse,
        "description": (
            "Con la firma obligatoria: `SIGNATURE_REQUIRED`, `SIGNATURE_STALE`, `SIGNATURE_INVALID` o "
            "`SIGNATURE_KEY_MISMATCH` (con un reto nuevo en `details.device_nonce`). Con la ubicación obligatoria: "
            "`LOCATION_REQUIRED`, `LOCATION_INACCURATE` o `LOCATION_OUT_OF_RANGE`."
        ),
    }
}


def _identified(result: VerificationResult) -> ApiResponse[VerificationResult]:
    if result.verified:
        return ok(result, result.text, code="EMPLOYEE_IDENTIFIED")
    return ok(result, result.text, code="EMPLOYEE_NOT_IDENTIFIED")


@router.get("/me", response_model=ApiResponse[CheckpointProfile], summary="Configuración del validador")
def checkpoint_profile(checkpoint: Checkpoint) -> ApiResponse[CheckpointProfile]:
    return ok(checkpoint.profile(), code="CHECKPOINT_PROFILE")


@router.get(
    "/recent",
    response_model=ApiResponse[CheckpointEventList],
    summary="Identificaciones de este validador (paginadas, la más reciente primero)",
)
def checkpoint_recent(checkpoint: Checkpoint, page: Pagination) -> ApiResponse[CheckpointEventList]:
    return ok(checkpoint.recent(page), code="CHECKPOINT_RECENT")


@router.post(
    "/qr/inspect",
    response_model=ApiResponse[CheckpointEmployee],
    summary="QR + rostro: de quién es el código QR (paso 1; no registra)",
    description=(
        "JSON: `qr_content` y, por petición (antifraude 2b), la firma del dispositivo (`signature_key`, "
        "`signature_nonce`, `signature` de `{nonce}.inspect.{SHA-256 del QR}`) y la ubicación (`location`, "
        "`location_samples`). La respuesta trae el reto de la siguiente firma (`device_nonce`)."
    ),
    responses={**PRESENCE_ERRORS, 422: {"model": ErrorResponse}},
    dependencies=[Depends(checkpoint_rate_limit)],
)
def inspect_qr(payload: CheckpointQrRequest, checkpoint: Checkpoint) -> ApiResponse[CheckpointEmployee]:
    employee = checkpoint.inspect_qr(payload.qr_content, _signed(payload))
    return ok(employee, code="QR_HOLDER_FOUND", params={"name": employee.name})


@router.post(
    "/identify/qr",
    response_model=ApiResponse[VerificationResult],
    summary="Identificar a un empleado por su código QR",
    description=(
        "El QR debe ser de un empleado activo de la empresa del validador. Siempre 200: ver `verified`. Por petición "
        "(antifraude 2b): la firma del dispositivo (`{nonce}.qr.{SHA-256 del QR}`) y la ubicación, como en "
        "`/qr/inspect`; la respuesta trae el reto de la siguiente firma (`device_nonce`)."
    ),
    responses=PRESENCE_ERRORS,
    dependencies=[Depends(checkpoint_rate_limit)],
)
def identify_by_qr(payload: CheckpointQrRequest, checkpoint: Checkpoint) -> ApiResponse[VerificationResult]:
    return _identified(checkpoint.identify_qr(payload.qr_content, _signed(payload)))


@router.post(
    "/identify/face",
    response_model=ApiResponse[VerificationResult],
    summary="Identificar a un empleado por su rostro (1:N) o confirmar el rostro del dueño de un QR",
    description=(
        "Multipart: `images` (1 a 3 capturas frontales), `challenge_id` + `challenge_image` + `flash_image` (reto de "
        "`/api/face/challenge`, si la empresa exige prueba de vida) y, en el modo QR_AND_FACE, "
        "`qr_content`. Sin QR se busca a la persona entre los empleados de la empresa con identidad "
        "validada: todas las capturas deben señalar a la misma persona, alcanzar el nivel de "
        "confianza de la empresa y superar a la segunda más parecida por FACE_IDENTIFY_MARGIN. Por petición "
        "(antifraude 2b): la firma del dispositivo (`signature_key`, `signature_nonce`, `signature` de "
        "`{nonce}.face.{SHA-256 de la primera captura}`) y la ubicación (`latitude`, `longitude`, `accuracy`, "
        "`location_samples`); la respuesta trae el reto de la siguiente firma (`device_nonce`)."
    ),
    responses={**PRESENCE_ERRORS, 413: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
    dependencies=[Depends(checkpoint_rate_limit)],
)
def identify_by_face(
    checkpoint: Checkpoint,
    pipeline: Pipeline,
    images: Annotated[list[UploadFile], File(description="Capturas frontales (JPEG/PNG/WEBP)")],
    liveness: Liveness,
    client: OperatorClient,
    signed: ValidatorRequest,
    qr_content: Annotated[str | None, Form(max_length=512)] = None,
    camera_label: CameraLabel = None,
) -> ApiResponse[VerificationResult]:
    result = checkpoint.identify_face(
        pipeline,
        read_image_uploads(images, max_files=3),
        liveness=liveness,
        qr_content=qr_content or None,
        camera_label=camera_label,
        client=client,
        request=signed,
    )
    return _identified(result)
