"""Validación previa de imágenes faciales (sin comparar identidad ni registrar intentos).

Permite al cliente dar retroalimentación inmediata (la foto está borrosa, oscura, sin el rostro completo o con un
accesorio que la empresa bloquea) antes de pasar a la prueba de vida o de enviar el registro, e informa TODOS los
accesorios detectados (`accessories`) para que la app los muestre como insignias sobre el rostro (decisión del dueño,
2026-10-07: la insignia es el único aviso; ningún texto pide retirar nada). El backend vuelve a validar todo en los
endpoints definitivos.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile

from app.core.clock import epoch_ms
from app.core.exceptions import UnprocessableError
from app.core.responses import ApiResponse, ok
from app.dependencies import (
    CurrentUser,
    DbSession,
    Pipeline,
    company_of,
    face_check_rate_limit,
    read_image_uploads,
    require_roles,
    require_screen,
)
from app.models import UserRole
from app.schemas.common import ErrorResponse
from app.schemas.face import FaceCheckResponse
from app.schemas.verification import ChallengePurpose, FaceChallengeResponse, FlashColors, FlashTokenIn
from app.services import flash_pacing
from app.services.catalog_service import instruction_text
from app.services.face_capture_service import FACE_CAPTURE_SCREENS, FaceCaptureService

router = APIRouter(
    prefix="/face",
    tags=["Rostro"],
    # Primero el permiso: un rol sin estas pantallas recibe 403 sin gastar su límite ni un worker facial.
    dependencies=[
        Depends(require_roles(UserRole.EMPLOYEE, UserRole.VALIDATOR, UserRole.COMPANY)),
        Depends(require_screen(*FACE_CAPTURE_SCREENS)),
        Depends(face_check_rate_limit),
    ],
)


@router.post(
    "/check",
    response_model=ApiResponse[FaceCheckResponse],
    summary="Validar una captura facial (calidad, pose y accesorios)",
    description=(
        "Devuelve 200 si la imagen es apta (con `accessories`: los detectados por mayoría aunque la empresa no los "
        "bloquee, para las insignias de la app), o 422 con `code` (NO_FACE, MULTIPLE_FACES, "
        "POSE_NOT_FRONTAL, TOO_DARK, TOO_BLURRY, ACCESSORIES_DETECTED, ...) y `details` "
        '(p. ej. `{"accessories": ["GLASSES", "HEADWEAR"]}`: los que la política bloquea). Envía `images` (1 a 3 '
        "capturas consecutivas, recomendado 3) o `image` (una). Los accesorios se deciden por mayoría entre "
        "las capturas, así un falso positivo aislado no bloquea. `allow_headwear` solo se respeta "
        "para COMPANY; para EMPLOYEE se usa su excepción registrada y, para VALIDATOR, la prenda de "
        "cabeza se decide al identificar a la persona."
    ),
    responses={422: {"model": ErrorResponse}},
)
def check_face(
    user: CurrentUser,
    db: DbSession,
    pipeline: Pipeline,
    images: Annotated[list[UploadFile] | None, File(description="1 a 3 capturas consecutivas")] = None,
    image: Annotated[UploadFile | None, File(description="Una captura (compatibilidad)")] = None,
    allow_headwear: Annotated[bool, Form()] = False,
) -> ApiResponse[FaceCheckResponse]:
    uploads = [*(images or []), *([image] if image is not None else [])]
    if not uploads:
        raise UnprocessableError(code="IMAGE_REQUIRED")
    data = read_image_uploads(uploads, max_files=3)
    result = FaceCaptureService(db, user, company_of(user)).precheck(pipeline, data, allow_headwear=allow_headwear)
    return ok(result, code="FACE_CHECK_PASSED")


@router.post(
    "/challenge",
    response_model=ApiResponse[FaceChallengeResponse],
    summary="Obtener reto de prueba de vida (movimientos)",
    description=(
        "Reto aleatorio de uso único. `purpose=VERIFICATION` (por omisión): de uno a tres movimientos (TURN_LEFT, "
        "TURN_RIGHT, LOOK_UP, LOOK_DOWN, MOVE_CLOSER; nunca el mismo dos veces seguidas) según la política de la "
        "empresa. `purpose=ENROLLMENT` (el registro facial, propio o en persona): SIEMPRE los cuatro movimientos de la "
        "cabeza (TURN_RIGHT, TURN_LEFT, LOOK_UP, LOOK_DOWN) en orden al azar; el registro rechaza cualquier otro reto. "
        "Si la empresa aún usa el destello, sus colores (`flash`). Vence en `expires_in` segundos (política de la "
        "empresa). Las direcciones son desde el punto de vista del empleado."
    ),
)
def face_challenge(
    user: CurrentUser,
    db: DbSession,
    purpose: Annotated[
        ChallengePurpose, Query(description="VERIFICATION (1 a 3 movimientos) o ENROLLMENT (los cuatro del registro)")
    ] = "VERIFICATION",
) -> ApiResponse[FaceChallengeResponse]:
    # company_of: 409 si un empleado de varias empresas aún no elige; 403 sin empresa (ADMIN).
    challenge = FaceCaptureService(db, user, company_of(user)).challenge(purpose)
    if not challenge.liveness_required:
        return ok(challenge, code="LIVENESS_NOT_REQUIRED")
    # El mensaje es la instrucción del primer movimiento (del catálogo; el sobre la arma en cada idioma).
    return ok(challenge, instruction_text(challenge.actions[0]), code="CHALLENGE_ISSUED")


@router.post(
    "/challenge/flash",
    response_model=ApiResponse[FlashColors],
    summary="Destello de respaldo: los colores del reto cuando no hay canal en vivo",
    description=(
        "Con el destello dictado por el servidor (`flash_pace` del reto), los colores se piden uno por uno por el "
        "canal en vivo (`/api/ws/validation`, mensaje `flash`). Si el canal no está disponible (un proxy que bloquea "
        "WebSocket, una red inestable), la app manda aquí el token inicial y recibe los colores de siempre: el intento "
        "sigue, con la señal FLASH_UNPACED (medida, nunca un rechazo). 422 FLASH_TOKEN_INVALID si el token venció, se "
        "alteró o es de otra cuenta. Sin consultas a la base: el token va sellado."
    ),
    responses={422: {"model": ErrorResponse}},
)
def flash_fallback(body: FlashTokenIn, user: CurrentUser) -> ApiResponse[FlashColors]:
    colors = flash_pacing.fallback_colors(body.token, user.id, epoch_ms())
    return ok(FlashColors(flash=colors), code="FLASH_COLORS")
