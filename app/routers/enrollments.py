"""Registro facial del empleado (auto-registro) y validación de identidad por COMPANY."""

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile

from app.core.config import settings
from app.core.responses import ApiResponse, ok
from app.dependencies import (
    CameraLabel,
    CompanyScope,
    CompanyUser,
    DbSession,
    EmployeeUser,
    Liveness,
    Pagination,
    Pipeline,
    company_of,
    face_check_rate_limit,
    read_image_uploads,
    read_video_upload,
    require_screen,
    verification_rate_limit,
    voice_answer_rate_limit,
)
from app.models import EnrollmentStatus, Screen
from app.schemas.common import ErrorResponse
from app.schemas.enrollment import (
    EnrollmentPhotoResponse,
    EnrollmentProgress,
    EnrollmentRejectRequest,
    EnrollmentSubmitResponse,
    FaceEnrollmentDetail,
    FaceEnrollmentList,
    VoiceAnswerResponse,
    VoiceChallengeResponse,
    VoiceClipRead,
)
from app.services.enrollment_service import EnrollmentService
from app.services.enrollment_steps import EnrollmentStepsService
from app.services.voice_verification import VoiceVerificationService

router = APIRouter(
    tags=["Registro facial y validación"],
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)


@router.get(
    "/enrollment/progress",
    response_model=ApiResponse[EnrollmentProgress],
    summary="EMPLOYEE: el estado de los tres pasos de su registro facial",
    description=(
        "Los tres pasos independientes del registro (decisión del dueño, 2026-10-07) para el índice de la app: `photo` "
        "(pending, done con `checked_at` y `expires_at`, expired), `capture` (locked mientras falte la foto vigente, "
        "pending, done con `submitted_at`) y `voice` (not_required si la política no la pide, locked, pending con "
        "`answered` de `total` y `attempts_left`, exhausted cuando se agotaron los intentos —se repiten los pasos 1 y "
        "2—, done). Una lectura acotada; nada se escribe."
    ),
    dependencies=[Depends(require_screen(Screen.EMPLOYEE_ENROLL))],
)
def enrollment_progress(user: EmployeeUser, db: DbSession) -> ApiResponse[EnrollmentProgress]:
    return ok(EnrollmentStepsService(db, company_of(user)).progress(user), code="ENROLLMENT_PROGRESS")


@router.post(
    "/enrollment/photo",
    response_model=ApiResponse[EnrollmentPhotoResponse],
    status_code=201,
    summary="EMPLOYEE: paso 1, la foto inicial de su registro facial",
    description=(
        "Multipart con `images` (1 a 3 capturas de frente, recomendado 1). La misma validación que `POST /face/check` "
        "(422 con el código del catálogo `face_errors`: NO_FACE, TOO_BLURRY, TOO_DARK, ACCESSORIES_DETECTED con los "
        "bloqueados en `details`...) y, aceptada, la mejor queda guardada CIFRADA en el bucket como borrador del "
        "registro con su plantilla facial, hasta `expires_at` (`FACE_ENROLLMENT_DRAFT_HOURS`). Repetirla reemplaza la "
        "anterior. Responde `accessories` (todos los detectados, para las insignias), `checked_at` y `expires_at`. "
        "409 ENROLLMENT_PENDING o ENROLLMENT_APPROVED si el registro ya no admite cambios."
    ),
    dependencies=[Depends(require_screen(Screen.EMPLOYEE_ENROLL)), Depends(face_check_rate_limit)],
    responses={409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
def take_enrollment_photo(
    user: EmployeeUser,
    db: DbSession,
    pipeline: Pipeline,
    images: Annotated[list[UploadFile], File(description="1 a 3 capturas de frente")],
) -> ApiResponse[EnrollmentPhotoResponse]:
    data = read_image_uploads(images, max_files=3)
    result = EnrollmentStepsService(db, company_of(user)).photograph(user, data, pipeline)
    return ok(result, code="ENROLLMENT_PHOTO_SAVED", status_code=201)


@router.post(
    "/enrollment/face",
    response_model=ApiResponse[EnrollmentSubmitResponse],
    status_code=201,
    summary="EMPLOYEE: paso 2, las capturas con prueba de vida (queda en validación)",
    description=(
        "Disponible cuando `face_status` es NOT_ENROLLED o REJECTED y hay una foto inicial VIGENTE (paso 1, "
        "`POST /enrollment/photo`; si no la hay o venció, 409 ENROLLMENT_PHOTO_REQUIRED). Multipart con `images` "
        "(las `FACE_ENROLL_VALID_PHOTOS` = 32 fotos frontales VÁLIDAS en orden de la toma, hasta "
        "`FACE_ENROLL_MAX_PHOTOS`; el servidor las vuelve a validar, elige las mejores como referencias y exige "
        "`FACE_ENROLL_MIN_USABLE` útiles) y, si la prueba de vida está activa, `challenge_id` (de "
        "`/api/face/challenge?purpose=ENROLLMENT`) + `challenge_image` por movimiento. Se validan calidad, pose, "
        "accesorios, consistencia, que las capturas sean la MISMA persona que la foto inicial (422 "
        "ENROLLMENT_PHOTO_MISMATCH; la foto se conserva) y la prueba de vida; los embeddings quedan inactivos y la "
        "foto inicial se consume. Con `voice_verification` en la política, la respuesta trae `voice` (compatibilidad "
        "con la app de un solo flujo; la app actual pide su sesión en `POST /enrollment/voice/start`) y el estado NO "
        "cambia hasta responder las preguntas; si no, pasa a PENDING_REVIEW hasta que COMPANY lo apruebe."
    ),
    dependencies=[Depends(require_screen(Screen.EMPLOYEE_ENROLL)), Depends(verification_rate_limit)],
    responses={409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
def submit_enrollment(
    user: EmployeeUser,
    db: DbSession,
    pipeline: Pipeline,
    images: Annotated[list[UploadFile], File(description="Capturas frontales")],
    liveness: Liveness,
    accessory_review: Annotated[
        bool,
        Form(
            description=(
                "true = el empleado indica que NO usa el accesorio detectado: el registro se envía "
                "marcado (`flagged_accessories`) para que COMPANY lo confirme en la foto."
            )
        ),
    ] = False,
    camera_label: CameraLabel = None,
) -> ApiResponse[EnrollmentSubmitResponse]:
    frontal = read_image_uploads(images, max_files=settings.FACE_ENROLL_MAX_PHOTOS)
    result = EnrollmentService(db, company_of(user)).submit(
        user,
        frontal,
        pipeline,
        liveness=liveness,
        accessory_review=accessory_review,
        camera_label=camera_label,
    )
    return ok(
        result,
        code="ENROLLMENT_PHOTOS_ACCEPTED" if result.voice is not None else "ENROLLMENT_SUBMITTED",
        status_code=201,
    )


@router.post(
    "/enrollment/voice/start",
    response_model=ApiResponse[VoiceChallengeResponse],
    summary="EMPLOYEE: paso 3, la sesión de preguntas en video de su registro pendiente",
    description=(
        "Emite una sesión de voz NUEVA (token sellado, preguntas al azar sobre los datos del empleado) para el "
        "registro que espera su video, cuantas veces haga falta (la persona cierra la app y vuelve otro día): las "
        "respuestas ya aceptadas se conservan y `questions` trae solo las que faltan, con `answered` de `total`. 409 "
        "VOICE_NOT_PENDING sin registro pendiente (primero los pasos 1 y 2); 422 VOICE_RETRIES_EXHAUSTED con los "
        "intentos agotados (el tope vive en la base: se repiten los pasos 1 y 2)."
    ),
    dependencies=[Depends(require_screen(Screen.EMPLOYEE_ENROLL)), Depends(verification_rate_limit)],
    responses={409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
def start_voice_session(user: EmployeeUser, db: DbSession) -> ApiResponse[VoiceChallengeResponse]:
    return ok(EnrollmentStepsService(db, company_of(user)).start_voice(user), code="VOICE_SESSION_STARTED")


@router.post(
    "/enrollment/voice/answer",
    response_model=ApiResponse[VoiceAnswerResponse],
    summary="EMPLOYEE: responder en video una pregunta de la verificación por voz",
    description=(
        "Multipart con `token` (la sesión sellada de `POST /enrollment/voice/start`, o la renovada de la respuesta "
        "anterior), `position` (la pregunta, 0 = la primera) y `clip` (el video con audio de la respuesta: WebM o MP4, "
        "hasta `FACE_VIDEO_MAX_MB`). El servidor decodifica el clip, transcribe la voz en el idioma de la petición, la "
        "compara con el dato registrado y comprueba que el rostro del video sea el de las fotos. 422 con el motivo "
        "(VIDEO_UNSUPPORTED_FORMAT, ANSWER_TOO_SHORT, ANSWER_TOO_LONG, ANSWER_INAUDIBLE, ANSWER_UNCLEAR, "
        "ANSWER_MISMATCH, VIDEO_FACE_MISMATCH) y el token en `details.token`: se repite la misma pregunta; "
        "VOICE_SESSION_EXPIRED o VOICE_SESSION_INVALID: se pide otra sesión (las respuestas aceptadas se conservan); "
        "VOICE_RETRIES_EXHAUSTED: se repiten los pasos 1 y 2. Con la última respuesta aceptada (`done`), el registro "
        "queda en validación de la empresa."
    ),
    dependencies=[Depends(require_screen(Screen.EMPLOYEE_ENROLL)), Depends(voice_answer_rate_limit)],
    responses={
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        413: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
def answer_voice_question(
    user: EmployeeUser,
    db: DbSession,
    pipeline: Pipeline,
    token: Annotated[str, Form(max_length=4000, description="Sesión sellada de la verificación por voz")],
    position: Annotated[int, Form(ge=0, le=10, description="Pregunta que se responde (0 = la primera)")],
    clip: Annotated[UploadFile, File(description="Video con audio de la respuesta (WebM o MP4)")],
) -> ApiResponse[VoiceAnswerResponse]:
    data = read_video_upload(clip)
    result = VoiceVerificationService(db, company_of(user)).answer(user, token, position, data, pipeline)
    return ok(result, code="ENROLLMENT_VOICE_DONE" if result.done else "ANSWER_ACCEPTED")


@router.get(
    "/enrollments",
    response_model=ApiResponse[FaceEnrollmentList],
    summary="COMPANY: registros faciales por validar / historial",
    dependencies=[Depends(require_screen(Screen.COMPANY_VALIDATIONS, Screen.COMPANY_DASHBOARD))],
)
def list_enrollments(
    company: CompanyScope,
    db: DbSession,
    page: Pagination,
    status: Annotated[EnrollmentStatus | None, Query(description="Por defecto: PENDING")] = EnrollmentStatus.PENDING,
) -> ApiResponse[FaceEnrollmentList]:
    result = EnrollmentService(db, company).list_enrollments(status=status, page=page)
    return ok(result, code="ENROLLMENTS_LISTED", params={"count": result.total})


@router.get(
    "/enrollments/{enrollment_id}",
    response_model=ApiResponse[FaceEnrollmentDetail],
    summary="COMPANY: detalle con fotografía de referencia",
    responses={404: {"model": ErrorResponse}},
    dependencies=[Depends(require_screen(Screen.COMPANY_VALIDATIONS))],
)
def get_enrollment(enrollment_id: int, company: CompanyScope, db: DbSession) -> ApiResponse[FaceEnrollmentDetail]:
    return ok(EnrollmentService(db, company).detail(enrollment_id), code="ENROLLMENT_FOUND")


@router.get(
    "/enrollments/{enrollment_id}/voice/{answer_id}/clip",
    response_model=ApiResponse[VoiceClipRead],
    summary="COMPANY: el video de una respuesta de la verificación por voz (base64)",
    description=(
        "El clip descifrado en memoria, en base64 dentro del contrato (nunca una URL del bucket). Solo de un registro "
        "de la empresa que ya terminó su verificación por voz; sale del bucket a los `FACE_VIDEO_RETENTION_DAYS`."
    ),
    responses={404: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
    dependencies=[Depends(require_screen(Screen.COMPANY_VALIDATIONS))],
)
def get_voice_clip(
    enrollment_id: int, answer_id: int, company: CompanyScope, db: DbSession
) -> ApiResponse[VoiceClipRead]:
    return ok(VoiceVerificationService(db, company).clip(enrollment_id, answer_id), code="VOICE_CLIP_FOUND")


@router.post(
    "/enrollments/{enrollment_id}/approve",
    response_model=ApiResponse[FaceEnrollmentDetail],
    summary="COMPANY: aceptar usuario (identidad validada)",
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    dependencies=[Depends(require_screen(Screen.COMPANY_VALIDATIONS))],
)
def approve_enrollment(
    enrollment_id: int, reviewer: CompanyUser, company: CompanyScope, db: DbSession
) -> ApiResponse[FaceEnrollmentDetail]:
    detail = EnrollmentService(db, company).approve(enrollment_id, reviewer)
    return ok(detail, code="ENROLLMENT_APPROVED", key="ENROLLMENT_APPROVED_DONE")


@router.post(
    "/enrollments/{enrollment_id}/reject",
    response_model=ApiResponse[FaceEnrollmentDetail],
    summary="COMPANY: rechazar usuario (debe registrarse de nuevo)",
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    dependencies=[Depends(require_screen(Screen.COMPANY_VALIDATIONS))],
)
def reject_enrollment(
    enrollment_id: int, payload: EnrollmentRejectRequest, reviewer: CompanyUser, company: CompanyScope, db: DbSession
) -> ApiResponse[FaceEnrollmentDetail]:
    detail = EnrollmentService(db, company).reject(enrollment_id, reviewer, payload.reason)
    return ok(detail, code="ENROLLMENT_REJECTED")
