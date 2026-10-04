"""Registro facial del empleado (auto-registro) y validación de identidad por COMPANY."""

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile

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
    read_image_uploads,
    require_screen,
    verification_rate_limit,
)
from app.models import EnrollmentStatus, Screen
from app.schemas.common import ErrorResponse
from app.schemas.enrollment import (
    EnrollmentRejectRequest,
    EnrollmentSubmitResponse,
    FaceEnrollmentDetail,
    FaceEnrollmentList,
)
from app.services.enrollment_service import EnrollmentService

router = APIRouter(
    tags=["Registro facial y validación"],
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)


@router.post(
    "/enrollment/face",
    response_model=ApiResponse[EnrollmentSubmitResponse],
    status_code=201,
    summary="EMPLOYEE: registrar su rostro (queda en validación)",
    description=(
        "Disponible cuando `face_status` es NOT_ENROLLED o REJECTED. Multipart con `images` "
        "(1 a 5 capturas frontales; se recomiendan 3) y, si la prueba de vida está activa, "
        "`challenge_id` (de `/api/face/challenge`) + `challenge_image` + `flash_image`. Se validan calidad, pose, "
        "accesorios, consistencia y prueba de vida; los embeddings quedan inactivos y el estado "
        "pasa a PENDING_REVIEW hasta que COMPANY lo apruebe."
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
    frontal = read_image_uploads(images, max_files=5)
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
        "Registro facial enviado. Tu identidad está en validación.",
        code="ENROLLMENT_SUBMITTED",
        status_code=201,
    )


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
    return ok(result, f"{result.total} registro(s) facial(es)", code="ENROLLMENTS_LISTED")


@router.get(
    "/enrollments/{enrollment_id}",
    response_model=ApiResponse[FaceEnrollmentDetail],
    summary="COMPANY: detalle con fotografía de referencia",
    responses={404: {"model": ErrorResponse}},
    dependencies=[Depends(require_screen(Screen.COMPANY_VALIDATIONS))],
)
def get_enrollment(enrollment_id: int, company: CompanyScope, db: DbSession) -> ApiResponse[FaceEnrollmentDetail]:
    return ok(
        EnrollmentService(db, company).detail(enrollment_id), "Registro facial encontrado", code="ENROLLMENT_FOUND"
    )


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
    return ok(detail, "Usuario aceptado. Ya puede verificar su identidad.", code="ENROLLMENT_APPROVED")


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
    return ok(detail, "Usuario rechazado. Deberá registrar su rostro de nuevo.", code="ENROLLMENT_REJECTED")
