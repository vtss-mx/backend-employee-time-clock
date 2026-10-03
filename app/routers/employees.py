"""Administración de empleados. Todos los endpoints requieren rol COMPANY.

Incluye el rostro en persona: la empresa registra o verifica el rostro del empleado presente con
su propia cámara (registro asistido y verificación 1:1).
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile, status

from app.core.responses import ApiResponse, ok
from app.dependencies import (
    CameraLabel,
    ChallengeImages,
    CompanyScope,
    CompanyUser,
    DbSession,
    Pagination,
    Pipeline,
    read_challenge_images,
    read_image_uploads,
    request_meta,
    require_screen,
    verification_rate_limit,
)
from app.models import Screen
from app.schemas.common import ErrorResponse
from app.schemas.employee import (
    EmployeeCreate,
    EmployeeList,
    EmployeeRead,
    EmployeeStatusUpdate,
    EmployeeUpdate,
    IdentityReverifyRequest,
)
from app.schemas.enrollment import EnrollmentSubmitResponse
from app.schemas.qr import EmployeeQrSummary
from app.schemas.verification import VerificationLogList, VerificationResult
from app.services.employee_service import EmployeeService
from app.services.enrollment_service import EnrollmentService
from app.services.qr_service import QrService
from app.services.verification_service import VerificationService

router = APIRouter(
    prefix="/employees",
    tags=["Empleados (COMPANY)"],
    responses={
        401: {"model": ErrorResponse, "description": "No autenticado"},
        403: {"model": ErrorResponse, "description": "Rol sin permisos"},
    },
)

NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": ErrorResponse, "description": "Empleado no encontrado"}}


@router.get(
    "",
    response_model=ApiResponse[EmployeeList],
    summary="Listar/buscar empleados",
    dependencies=[Depends(require_screen(Screen.COMPANY_EMPLOYEES, Screen.COMPANY_DASHBOARD))],
)
def list_employees(
    company: CompanyScope,
    db: DbSession,
    page: Pagination,
    search: Annotated[str | None, Query(max_length=100, description="Nombre, número o correo")] = None,
    active: Annotated[bool | None, Query(description="Filtrar por estado")] = None,
) -> ApiResponse[EmployeeList]:
    result = EmployeeService(db, company).list_employees(search=search, active=active, page=page)
    return ok(result, f"{result.total} empleado(s) encontrado(s)", code="EMPLOYEES_LISTED")


@router.post(
    "",
    response_model=ApiResponse[EmployeeRead],
    status_code=status.HTTP_201_CREATED,
    summary="Registrar empleado (solo datos)",
    description=(
        "Crea el usuario EMPLOYEE, el empleado y genera automáticamente su QR. "
        "El rostro NO se registra aquí: el empleado lo registra en su primer inicio de "
        "sesión (`face_status=NOT_ENROLLED`) y después COMPANY valida su identidad en "
        "`/api/enrollments`."
    ),
    responses={409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
    dependencies=[Depends(require_screen(Screen.COMPANY_EMPLOYEES))],
)
def create_employee(payload: EmployeeCreate, company: CompanyScope, db: DbSession) -> ApiResponse[EmployeeRead]:
    service = EmployeeService(db, company)
    return ok(
        service.read(service.create(payload)),
        "Empleado registrado correctamente",
        code="EMPLOYEE_CREATED",
        status_code=status.HTTP_201_CREATED,
    )


@router.get(
    "/{employee_id}",
    response_model=ApiResponse[EmployeeRead],
    summary="Detalle de empleado",
    responses=NOT_FOUND,
    dependencies=[Depends(require_screen(Screen.COMPANY_EMPLOYEES))],
)
def get_employee(employee_id: int, company: CompanyScope, db: DbSession) -> ApiResponse[EmployeeRead]:
    service = EmployeeService(db, company)
    return ok(service.read(service.get(employee_id)), "Empleado encontrado", code="EMPLOYEE_FOUND")


@router.put(
    "/{employee_id}",
    response_model=ApiResponse[EmployeeRead],
    summary="Editar empleado (solo campos enviados)",
    responses={**NOT_FOUND, 409: {"model": ErrorResponse}},
    dependencies=[Depends(require_screen(Screen.COMPANY_EMPLOYEES))],
)
def update_employee(
    employee_id: int, payload: EmployeeUpdate, company: CompanyScope, db: DbSession
) -> ApiResponse[EmployeeRead]:
    service = EmployeeService(db, company)
    return ok(service.read(service.update(employee_id, payload)), "Empleado actualizado", code="EMPLOYEE_UPDATED")


@router.patch(
    "/{employee_id}/status",
    response_model=ApiResponse[EmployeeRead],
    summary="Activar / desactivar empleado",
    responses=NOT_FOUND,
    dependencies=[Depends(require_screen(Screen.COMPANY_EMPLOYEES))],
)
def set_employee_status(
    employee_id: int, payload: EmployeeStatusUpdate, company: CompanyScope, db: DbSession
) -> ApiResponse[EmployeeRead]:
    service = EmployeeService(db, company)
    employee = service.read(service.set_active(employee_id, payload.active))
    message = "Empleado activado" if payload.active else "Empleado desactivado"
    return ok(employee, message, code="EMPLOYEE_ACTIVATED" if payload.active else "EMPLOYEE_DEACTIVATED")


@router.delete(
    "/{employee_id}",
    response_model=ApiResponse[None],
    summary="Eliminar empleado definitivamente (incluye datos biométricos y QR)",
    responses=NOT_FOUND,
    dependencies=[Depends(require_screen(Screen.COMPANY_EMPLOYEES))],
)
def delete_employee(employee_id: int, company: CompanyScope, db: DbSession) -> ApiResponse[None]:
    EmployeeService(db, company).delete(employee_id)
    return ok(None, "Empleado eliminado definitivamente", code="EMPLOYEE_DELETED")


# ---------------- Rostro ----------------


@router.post(
    "/{employee_id}/face/reset",
    response_model=ApiResponse[EmployeeRead],
    summary="Solicitar nueva verificación de identidad",
    description=(
        "Elimina los datos faciales del empleado y lo regresa a `NOT_ENROLLED`: en su próximo "
        "acceso deberá registrar su rostro con prueba de vida y COMPANY validarlo otra vez. "
        "`reason` (opcional) se le muestra al empleado."
    ),
    responses=NOT_FOUND,
    dependencies=[Depends(require_screen(Screen.COMPANY_EMPLOYEES))],
)
def reset_face(
    employee_id: int, company: CompanyScope, db: DbSession, payload: IdentityReverifyRequest | None = None
) -> ApiResponse[EmployeeRead]:
    service = EmployeeService(db, company)
    employee = service.read(service.reset_face(employee_id, payload.reason if payload else None))
    return ok(
        employee,
        "Se solicitó al empleado verificar nuevamente su identidad.",
        code="IDENTITY_REVERIFY_REQUESTED",
    )


# ---------------- Rostro en persona ----------------

IN_PERSON = [Depends(require_screen(Screen.COMPANY_EMPLOYEES)), Depends(verification_rate_limit)]
FrontalImages = Annotated[list[UploadFile], File(description="Capturas frontales del empleado")]
ChallengeId = Annotated[str | None, Form(max_length=100, description="Reto de `/api/face/challenge`")]


@router.post(
    "/{employee_id}/face/enroll",
    response_model=ApiResponse[EnrollmentSubmitResponse],
    status_code=status.HTTP_201_CREATED,
    summary="Registrar el rostro del empleado en persona (queda aprobado)",
    description=(
        "Registro asistido: la empresa captura el rostro del empleado presente con su cámara. "
        "Multipart con `images` (1 a 5 capturas frontales) y, si la prueba de vida está activa, "
        "`challenge_id` (de `/api/face/challenge`, pedido por la empresa) + `challenge_image`. "
        "Mismas validaciones que el autoregistro; la sospecha de foto o pantalla bloquea. Queda "
        "aprobado al momento y reemplaza cualquier registro anterior."
    ),
    responses={**NOT_FOUND, 409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
    dependencies=IN_PERSON,
)
def enroll_face_in_person(
    employee_id: int,
    operator: CompanyUser,
    company: CompanyScope,
    db: DbSession,
    pipeline: Pipeline,
    images: FrontalImages,
    challenge_id: ChallengeId = None,
    challenge_image: ChallengeImages = None,
    camera_label: CameraLabel = None,
) -> ApiResponse[EnrollmentSubmitResponse]:
    employee = EmployeeService(db, company).get(employee_id)
    result = EnrollmentService(db, company).enroll_in_person(
        employee,
        operator,
        read_image_uploads(images, max_files=5),
        pipeline,
        challenge_id=challenge_id,
        challenge_images=read_challenge_images(challenge_image),
        camera_label=camera_label,
    )
    return ok(result, result.message, code="FACE_ENROLLED_IN_PERSON", status_code=201)


@router.post(
    "/{employee_id}/face/verify",
    response_model=ApiResponse[VerificationResult],
    summary="Verificar en persona la identidad del empleado (rostro 1:1)",
    description=(
        "La empresa captura el rostro del empleado presente y se compara con su registro aprobado, "
        "con la confianza que exige la empresa y prueba de vida. `verified` indica el resultado; "
        "el intento queda en la bitácora del empleado a nombre de quien lo verificó."
    ),
    responses={**NOT_FOUND, 409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
    dependencies=IN_PERSON,
)
def verify_face_in_person(
    employee_id: int,
    request: Request,
    operator: CompanyUser,
    company: CompanyScope,
    db: DbSession,
    pipeline: Pipeline,
    images: FrontalImages,
    challenge_id: ChallengeId = None,
    challenge_image: ChallengeImages = None,
    camera_label: CameraLabel = None,
) -> ApiResponse[VerificationResult]:
    employee = EmployeeService(db, company).get(employee_id)
    ip, user_agent = request_meta(request)
    result = VerificationService(db, ip=ip, user_agent=user_agent).verify_in_person(
        employee,
        operator,
        read_image_uploads(images, max_files=3),
        pipeline,
        challenge_id=challenge_id,
        challenge_images=read_challenge_images(challenge_image),
        camera_label=camera_label,
    )
    code = "IDENTITY_VERIFIED" if result.verified else "IDENTITY_NOT_VERIFIED"
    return ok(result, result.message, code=code)


# ---------------- QR ----------------


@router.get(
    "/{employee_id}/qr",
    response_model=ApiResponse[EmployeeQrSummary],
    summary="Actividad del QR dinámico del empleado",
    description=(
        "El QR es dinámico: lo genera el empleado en su teléfono, vive unos segundos y sirve una sola vez, "
        "así que la empresa no lo ve ni lo descarga. Aquí: si tiene uno vigente y cuándo lo generó y usó."
    ),
    responses=NOT_FOUND,
    dependencies=[Depends(require_screen(Screen.COMPANY_EMPLOYEES))],
)
def qr_summary(employee_id: int, company: CompanyScope, db: DbSession) -> ApiResponse[EmployeeQrSummary]:
    employee = EmployeeService(db, company).get(employee_id)
    return ok(QrService(db).summary(employee), "Actividad del código QR", code="QR_SUMMARY")


@router.delete(
    "/{employee_id}/qr",
    response_model=ApiResponse[EmployeeQrSummary],
    summary="Invalidar el QR vigente (el teléfono del empleado muestra otro)",
    responses=NOT_FOUND,
    dependencies=[Depends(require_screen(Screen.COMPANY_EMPLOYEES))],
)
def revoke_qr(employee_id: int, company: CompanyScope, db: DbSession) -> ApiResponse[EmployeeQrSummary]:
    employee = EmployeeService(db, company).get(employee_id)
    qr_service = QrService(db)
    qr_service.revoke(employee)
    db.commit()
    return ok(qr_service.summary(employee), "Código QR invalidado", code="QR_REVOKED")


# ---------------- Historial ----------------


@router.get(
    "/{employee_id}/verifications",
    response_model=ApiResponse[VerificationLogList],
    summary="Bitácora de verificaciones (paginada, la más reciente primero)",
    responses=NOT_FOUND,
    dependencies=[Depends(require_screen(Screen.COMPANY_EMPLOYEES))],
)
def verification_history(
    employee_id: int, company: CompanyScope, db: DbSession, page: Pagination
) -> ApiResponse[VerificationLogList]:
    logs = EmployeeService(db, company).history(employee_id, page)
    return ok(logs, f"{logs.total} intento(s) de verificación", code="VERIFICATIONS_LISTED")
