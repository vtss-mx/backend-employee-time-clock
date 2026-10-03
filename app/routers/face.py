"""Validación previa de imágenes faciales (sin comparar identidad ni registrar intentos).

Permite al cliente dar retroalimentación inmediata ("Quítate los lentes para continuar")
antes de pasar a la prueba de vida o de enviar el registro. El backend vuelve a validar
todo en los endpoints definitivos.
"""

from dataclasses import replace
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, UploadFile

from app.core.exceptions import PermissionDeniedError, UnprocessableError
from app.core.responses import ApiResponse, ok
from app.dependencies import (
    CurrentUser,
    DbSession,
    Pipeline,
    company_of,
    face_check_rate_limit,
    read_image_uploads,
)
from app.models import UserRole
from app.schemas.common import ErrorResponse
from app.schemas.face import FaceCheckResponse
from app.schemas.verification import FaceChallengeResponse
from app.services.checkpoint_service import CheckpointService
from app.services.face_service import analyze_frames
from app.services.identity_core import issue_challenge
from app.services.policy_service import PolicyService
from app.services.verification_service import VerificationService

router = APIRouter(prefix="/face", tags=["Rostro"], dependencies=[Depends(face_check_rate_limit)])


@router.post(
    "/check",
    response_model=ApiResponse[FaceCheckResponse],
    summary="Validar una captura facial (calidad, pose y accesorios)",
    description=(
        "Devuelve 200 si la imagen es apta, o 422 con `code` (NO_FACE, MULTIPLE_FACES, "
        "POSE_NOT_FRONTAL, TOO_DARK, TOO_BLURRY, ACCESSORIES_DETECTED, ...) y `details` "
        '(p. ej. `{"accessories": ["GLASSES", "HEADWEAR"]}`). Envía `images` (1 a 3 capturas '
        "consecutivas, recomendado 3) o `image` (una). Los accesorios se deciden por mayoría entre "
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
        raise UnprocessableError("Envía al menos una captura", code="IMAGE_REQUIRED")
    data = read_image_uploads(uploads, max_files=3)
    policy = PolicyService(db, company_of(user)).current().face_policy(user.employee)
    # COMPANY puede omitirla a petición; el validador aún no sabe quién es (puede tener excepción).
    if user.role == UserRole.VALIDATOR or (user.role == UserRole.COMPANY and allow_headwear):
        policy = replace(policy, block_headwear=False)
    # La suplantación se decide en el envío definitivo (verificación) o la revisa COMPANY (registro).
    analyses, _ = analyze_frames(pipeline, data, policy=policy, check_spoof=False)
    analysis = min(analyses, key=lambda a: a.quality_score)
    result = FaceCheckResponse(
        detection_score=round(analysis.detection_score, 4),
        quality_score=analysis.quality_score,
        yaw_ratio=analysis.pose.yaw_ratio if analysis.pose else None,
    )
    return ok(result, "La captura es válida", code="FACE_CHECK_PASSED")


@router.post(
    "/challenge",
    response_model=ApiResponse[FaceChallengeResponse],
    summary="Obtener reto de prueba de vida (girar la cabeza)",
    description=(
        "Reto aleatorio de uso único (TURN_LEFT / TURN_RIGHT) que expira en "
        "FACE_CHALLENGE_TTL_SECONDS. Se usa en el registro facial y en la verificación (del "
        "empleado, del validador o de la empresa con el empleado presente). La dirección es desde "
        "el punto de vista del empleado."
    ),
)
def face_challenge(user: CurrentUser, db: DbSession) -> ApiResponse[FaceChallengeResponse]:
    if user.role == UserRole.VALIDATOR:
        challenge = CheckpointService(db, user).issue_challenge()
    elif user.role == UserRole.EMPLOYEE:
        company_of(user)  # 409 si trabaja en varias empresas y aún no elige
        challenge = VerificationService(db).issue_face_challenge(user)
    elif user.role == UserRole.COMPANY:
        # La empresa opera la cámara con el empleado presente (registro asistido o verificación).
        challenge = issue_challenge(db, user.id, PolicyService(db, company_of(user)).current())
    else:
        raise PermissionDeniedError()
    if not challenge.liveness_required:
        return ok(challenge, "Prueba de vida no requerida", code="LIVENESS_NOT_REQUIRED")
    return ok(challenge, challenge.instruction or "Reto de prueba de vida emitido", code="CHALLENGE_ISSUED")
