"""API pública de verificación facial (SDK móviles): la aplicación de una empresa verifica o identifica a SUS empleados.

Contrato: `docs/sdk/contrato-verificacion.md` (raíz). Se autentica SOLO con la llave de la empresa (cabecera
`X-API-Key`) con el permiso `VERIFICATION` y su propio límite por llave (`VerificationClient`); la empresa sale de la
llave. Cada envío trae además la prueba del dispositivo (obligatoria) y se revisa ANTES de reservar un worker facial
(`_take`): el orden de los parámetros es el orden en que FastAPI resuelve las dependencias.
"""

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile

from app.core.responses import ApiResponse, ok
from app.dependencies import (
    CameraLabel,
    DbSession,
    Liveness,
    OperatorClient,
    Pipeline,
    VerificationClient,
    read_image_uploads,
    request_meta,
)
from app.schemas.auth import DeviceLocation
from app.schemas.common import ErrorResponse
from app.schemas.verification_api import ApiChallenge, ApiChallengeIn, ApiVerificationResult
from app.services.api_verification_service import (
    ACTION_IDENTIFY,
    ACTION_VERIFY,
    ApiTake,
    ApiVerificationService,
    EmployeeReference,
    prove_device,
)
from app.services.catalog_service import instruction_text
from app.services.client_evidence import DeviceProofInput
from app.services.identity_core import MAX_FRONTAL_FRAMES

router = APIRouter(
    prefix="/integrations/v1/verification",
    tags=["API pública de verificación (SDK móviles)"],
    responses={
        401: {"model": ErrorResponse, "description": "Falta la llave, no es válida, venció o se revocó"},
        403: {"model": ErrorResponse, "description": "Sin el permiso VERIFICATION, empresa desactivada o sin prueba"},
        429: {"model": ErrorResponse, "description": "Demasiadas peticiones con esta llave o este dispositivo"},
    },
)
ATTEMPT_ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "El empleado no existe en tu empresa"},
    409: {"model": ErrorResponse, "description": "Empleado desactivado o sin registro facial aprobado"},
    413: {"model": ErrorResponse, "description": "Una imagen pasa del tamaño máximo"},
    422: {"model": ErrorResponse, "description": "Captura no válida, reto vencido o datos mal formados"},
    503: {"model": ErrorResponse, "description": "Motor facial ocupado o no disponible (reintentable)"},
}


def _take(action: str) -> Callable[..., ApiTake]:
    """Las frontales y la prueba del dispositivo (que firma `"{reto}.{acción}.{SHA-256 de la primera frontal}"`), con
    el límite por dispositivo: todo antes del motor facial."""

    def dependency(
        client: VerificationClient,
        images: Annotated[list[UploadFile], File(description="Capturas frontales JPEG (1 a 3; se recomiendan 2)")],
        device_key: Annotated[str | None, Form(description="Llave pública P-256 (SPKI, base64)")] = None,
        device_nonce: Annotated[str | None, Form(description="`device_nonce` del reto")] = None,
        device_signature: Annotated[str | None, Form(description="Firma ECDSA P-256 (r||s, base64)")] = None,
    ) -> ApiTake:
        frontal = read_image_uploads(images, max_files=MAX_FRONTAL_FRAMES)
        proof = DeviceProofInput(public_key=device_key, nonce=device_nonce, signature=device_signature)
        return ApiTake(prove_device(client, proof, action, frontal[0]), frontal)

    return dependency


def _service(request: Request, db: DbSession, client: VerificationClient) -> ApiVerificationService:
    ip, user_agent = request_meta(request)
    return ApiVerificationService(db, client, ip=ip, user_agent=user_agent)


def _location(
    latitude: Annotated[
        float | None, Form(ge=-90, le=90, description="Ubicación de la verificación (si la hay)")
    ] = None,
    longitude: Annotated[float | None, Form(ge=-180, le=180)] = None,
    accuracy: Annotated[float | None, Form(ge=0, le=100_000, description="Precisión (m)")] = None,
) -> DeviceLocation | None:
    """La ubicación del intento (opcional): la empresa decide si la exige (`verification_location`). `location_samples`
    del multipart no se usa aquí (FastAPI ignora los campos de más)."""
    if latitude is None or longitude is None:
        return None
    return DeviceLocation(latitude=latitude, longitude=longitude, accuracy=accuracy)


Service = Annotated[ApiVerificationService, Depends(_service)]
VerifyTake = Annotated[ApiTake, Depends(_take(ACTION_VERIFY))]
IdentifyTake = Annotated[ApiTake, Depends(_take(ACTION_IDENTIFY))]
Location = Annotated[DeviceLocation | None, Depends(_location)]


def _result(result: ApiVerificationResult, matched: str, other: str) -> ApiResponse[ApiVerificationResult]:
    # Siempre 200 (el intento se procesó): `data.decision` dice qué pasó y `code` lo distingue sin leer `data`.
    if result.decision == "STEP_UP":
        return ok(result, result.text, code="VERIFICATION_STEP_UP")
    return ok(result, result.text, code=matched if result.matched else other)


@router.post(
    "/challenge",
    response_model=ApiResponse[ApiChallenge],
    summary="Reto de prueba de vida para un intento (por dispositivo)",
    description=(
        "JSON `{device_key}`. Reto de uso único con los movimientos de la política de la empresa, la ráfaga si la usa "
        "(`burst`), el reto que firma la llave del dispositivo (`device_nonce`) y cómo capturar (`capture`). Un reto "
        "nuevo del mismo dispositivo invalida el anterior. Sin destello (retirado de la experiencia)."
    ),
    responses={422: {"model": ErrorResponse, "description": "La llave del dispositivo no es una llave P-256"}},
)
def challenge(service: Service, body: ApiChallengeIn) -> ApiResponse[ApiChallenge]:
    issued = service.challenge(body)
    if not issued.liveness_required:
        return ok(issued, code="LIVENESS_NOT_REQUIRED")
    return ok(issued, instruction_text(issued.actions[0]), code="CHALLENGE_ISSUED")


@router.post(
    "/verify",
    response_model=ApiResponse[ApiVerificationResult],
    summary="Verificar (1:1) que la persona es el empleado indicado",
    description=(
        "Multipart: `employee_id` o `employee_number` (exactamente uno), `images` (frontales), `challenge_id` + "
        "`challenge_image` (una por movimiento, en orden), `burst` + `burst_meta` (si el reto la pide), "
        "`camera_label`, `telemetry` (con `native`) y la prueba del dispositivo (`device_key`, `device_nonce`, "
        "`device_signature` de `{device_nonce}.verify.{SHA-256 de images[0]}`). Siempre 200 con `decision` "
        "(ALLOW, REVIEW, STEP_UP con el reto nuevo, DENY con `reason`)."
    ),
    responses=ATTEMPT_ERRORS,
)
def verify(
    take: VerifyTake,
    service: Service,
    liveness: Liveness,
    evidence: OperatorClient,
    pipeline: Pipeline,
    location: Location,
    employee_id: Annotated[int | None, Form(ge=1, description="Id del empleado")] = None,
    employee_number: Annotated[str | None, Form(min_length=1, max_length=30, description="Número")] = None,
    camera_label: CameraLabel = None,
) -> ApiResponse[ApiVerificationResult]:
    reference = EmployeeReference(employee_id=employee_id, employee_number=employee_number)
    result = service.verify(
        take, reference, pipeline, liveness=liveness, camera_label=camera_label, client=evidence, location=location
    )
    return _result(result, "IDENTITY_VERIFIED", "IDENTITY_NOT_VERIFIED")


@router.post(
    "/identify",
    response_model=ApiResponse[ApiVerificationResult],
    summary="Identificar (1:N) a la persona entre los empleados de la empresa",
    description=(
        "Los mismos campos de `/verify` sin el empleado; la firma es de `{device_nonce}.identify.{SHA-256 de "
        "images[0]}`. Busca entre los empleados activos con registro facial aprobado. Siempre 200 con `decision`; "
        "sin coincidencia, DENY con `reason` NO_MATCH, AMBIGUOUS_MATCH, INCONSISTENT_MATCH o EMPTY_GALLERY."
    ),
    responses={**ATTEMPT_ERRORS, 429: {"model": ErrorResponse, "description": "Bloqueo del dispositivo o límite"}},
)
def identify(
    take: IdentifyTake,
    service: Service,
    liveness: Liveness,
    evidence: OperatorClient,
    pipeline: Pipeline,
    location: Location,
    camera_label: CameraLabel = None,
) -> ApiResponse[ApiVerificationResult]:
    result = service.identify(
        take, pipeline, liveness=liveness, camera_label=camera_label, client=evidence, location=location
    )
    return _result(result, "EMPLOYEE_IDENTIFIED", "EMPLOYEE_NOT_IDENTIFIED")
