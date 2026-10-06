"""Administración de empleados. Todos los endpoints requieren rol COMPANY.

Incluye el rostro en persona: la empresa registra o verifica el rostro del empleado presente con
su propia cámara (registro asistido y verificación 1:1).
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile, status

from app.core.config import settings
from app.core.responses import ApiResponse, ok
from app.dependencies import (
    CameraLabel,
    CompanyScope,
    CompanyUser,
    DbSession,
    Liveness,
    OperatorClient,
    Pagination,
    Pipeline,
    read_image_uploads,
    request_meta,
    require_screen,
    trash_of,
    verification_rate_limit,
)
from app.models import DeviceStatus, Screen
from app.schemas.common import ErrorResponse
from app.schemas.employee import (
    EmployeeCreate,
    EmployeeIdList,
    EmployeeList,
    EmployeeRead,
    EmployeeStatusUpdate,
    EmployeeUpdate,
    IdentityReverifyRequest,
    IdentityReverifySummary,
)
from app.schemas.employee_device import EmployeeDeviceList, EmployeeDeviceRead, EmployeeDeviceStatusUpdate
from app.schemas.enrollment import EnrollmentSubmitResponse
from app.schemas.qr import EmployeeQrSummary
from app.schemas.verification import VerificationLogList, VerificationResult
from app.services.employee_devices import EmployeeDeviceService
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
#: Eliminar y restaurar (borrado lógico): 409 si ya está (o no está) en «Eliminados» o si otro tomó sus datos.
TRASH: dict[int | str, dict[str, Any]] = {
    **NOT_FOUND,
    409: {"model": ErrorResponse, "description": "`ALREADY_DELETED`, `NOT_DELETED` o `RESTORE_CONFLICT`"},
}


#: Pantallas que eligen empleados de la lista: la propia, el resumen, departamentos y las operaciones
#: para varios empleados (asignar un turno, registrar vacaciones colectivas).
PICKER_SCREENS = (
    Screen.COMPANY_EMPLOYEES,
    Screen.COMPANY_DASHBOARD,
    Screen.COMPANY_DEPARTMENTS,
    Screen.COMPANY_SHIFTS,
    Screen.COMPANY_CALENDAR,
)
SearchFilter = Annotated[str | None, Query(max_length=100, description="Nombre, número o correo")]
ActiveFilter = Annotated[bool | None, Query(description="Filtrar por estado")]
DepartmentFilter = Annotated[int | None, Query(gt=0, description="Solo los asignados a ese departamento")]
#: La papelera de empleados la ve solo la pantalla que los elimina y restaura (no las que eligen empleados).
EmployeeTrash = Annotated[bool, Depends(trash_of(Screen.COMPANY_EMPLOYEES))]


@router.get(
    "",
    response_model=ApiResponse[EmployeeList],
    summary="Listar/buscar empleados",
    description=(
        "También la usan Departamentos (sus empleados con `department_id`), Turnos y Calendario (a quién "
        "asignar un turno o registrar una ausencia). `deleted=true`: la papelera («Eliminados», solo con la pantalla "
        "de empleados), con quién y cuándo eliminó a cada uno."
    ),
    dependencies=[Depends(require_screen(*PICKER_SCREENS))],
)
def list_employees(
    company: CompanyScope,
    db: DbSession,
    page: Pagination,
    deleted: EmployeeTrash,
    search: SearchFilter = None,
    active: ActiveFilter = None,
    department_id: DepartmentFilter = None,
) -> ApiResponse[EmployeeList]:
    result = EmployeeService(db, company).list_employees(
        search=search, active=active, page=page, department_id=department_id, deleted=deleted
    )
    return ok(result, code="EMPLOYEES_LISTED", params={"count": result.total})


@router.get(
    "/ids",
    response_model=ApiResponse[EmployeeIdList],
    summary="Ids de los empleados de un filtro (seleccionar todos para una operación masiva)",
    description="Los mismos filtros del listado; a lo más el tope de una operación masiva (`limit`).",
    dependencies=[Depends(require_screen(Screen.COMPANY_SHIFTS, Screen.COMPANY_CALENDAR))],
)
def list_employee_ids(
    company: CompanyScope,
    db: DbSession,
    search: SearchFilter = None,
    active: ActiveFilter = None,
    department_id: DepartmentFilter = None,
) -> ApiResponse[EmployeeIdList]:
    result = EmployeeService(db, company).ids(search=search, active=active, department_id=department_id)
    return ok(result, code="EMPLOYEE_IDS", params={"count": result.total})


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
        code="EMPLOYEE_CREATED",
        status_code=status.HTTP_201_CREATED,
    )


@router.get(
    "/{employee_id}",
    response_model=ApiResponse[EmployeeRead],
    summary="Detalle de empleado (también uno en «Eliminados», con `deleted_at`)",
    responses=NOT_FOUND,
    dependencies=[Depends(require_screen(Screen.COMPANY_EMPLOYEES))],
)
def get_employee(employee_id: int, company: CompanyScope, db: DbSession) -> ApiResponse[EmployeeRead]:
    service = EmployeeService(db, company)
    return ok(service.read(service.get(employee_id, include_deleted=True)), code="EMPLOYEE_FOUND")


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
    return ok(service.read(service.update(employee_id, payload)), code="EMPLOYEE_UPDATED")


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
    return ok(employee, code="EMPLOYEE_ACTIVATED" if payload.active else "EMPLOYEE_DEACTIVATED")


@router.delete(
    "/{employee_id}",
    response_model=ApiResponse[None],
    summary="Eliminar empleado (a «Eliminados»; sus datos biométricos y fotos se borran para siempre)",
    description=(
        "Borrado lógico: deja de aparecer, de contar y de cobrarse desde hoy; su historial se conserva y se puede "
        "restaurar durante `SOFT_DELETE_RETENTION_DAYS`. Sus datos faciales y fotos se borran de verdad (no vuelven "
        "al restaurarlo), sus solicitudes pendientes se cancelan y su QR deja de servir."
    ),
    responses=TRASH,
    dependencies=[Depends(require_screen(Screen.COMPANY_EMPLOYEES))],
)
def delete_employee(employee_id: int, company: CompanyScope, user: CompanyUser, db: DbSession) -> ApiResponse[None]:
    EmployeeService(db, company).delete(employee_id, user)
    return ok(None, code="EMPLOYEE_DELETED")


@router.post(
    "/{employee_id}/restore",
    response_model=ApiResponse[EmployeeRead],
    summary="Restaurar un empleado de «Eliminados» (sin sus datos biométricos: registra su rostro de nuevo)",
    description=(
        "Revisa de nuevo el límite de empleados (409 `EMPLOYEE_LIMIT_REACHED`) y que nadie vigente haya tomado su "
        "número, RFC, CURP, NSS, correo o teléfono (409 `RESTORE_CONFLICT` con el campo)."
    ),
    responses=TRASH,
    dependencies=[Depends(require_screen(Screen.COMPANY_EMPLOYEES))],
)
def restore_employee(employee_id: int, company: CompanyScope, db: DbSession) -> ApiResponse[EmployeeRead]:
    service = EmployeeService(db, company)
    return ok(service.read(service.restore(employee_id)), code="EMPLOYEE_RESTORED")


# ---------------- Rostro ----------------


@router.post(
    "/face/reset",
    response_model=ApiResponse[IdentityReverifySummary],
    summary="Solicitar nueva verificación de identidad a TODOS los empleados",
    description=(
        "Para toda la empresa (p. ej. tras un incidente de seguridad o un cambio de cámaras): elimina los "
        "datos faciales de cada empleado con registro (aprobado, en validación o rechazado) y los regresa a "
        "`NOT_ENROLLED`. En su próximo acceso cada uno registra su rostro con prueba de vida y COMPANY lo "
        "valida otra vez. `reason` (opcional) se les muestra. Los que aún no tenían registro no cambian."
    ),
    dependencies=[Depends(require_screen(Screen.COMPANY_EMPLOYEES))],
)
def reset_all_faces(
    company: CompanyScope, db: DbSession, payload: IdentityReverifyRequest | None = None
) -> ApiResponse[IdentityReverifySummary]:
    changed = EmployeeService(db, company).reset_all_faces(payload.reason if payload else None)
    return ok(
        IdentityReverifySummary(employees=changed), code="IDENTITY_REVERIFY_REQUESTED_ALL", params={"count": changed}
    )


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
        code="IDENTITY_REVERIFY_REQUESTED",
    )


# ---------------- Rostro en persona ----------------

IN_PERSON = [Depends(require_screen(Screen.COMPANY_EMPLOYEES)), Depends(verification_rate_limit)]
FrontalImages = Annotated[list[UploadFile], File(description="Capturas frontales del empleado")]


@router.post(
    "/{employee_id}/face/enroll",
    response_model=ApiResponse[EnrollmentSubmitResponse],
    status_code=status.HTTP_201_CREATED,
    summary="Registrar el rostro del empleado en persona (queda aprobado)",
    description=(
        "Registro asistido: la empresa captura el rostro del empleado presente con su cámara. "
        "Multipart con `images` (hasta `FACE_ENROLL_MAX_PHOTOS` = 36 fotos frontales; el servidor elige las mejores) "
        "y, si la prueba de vida está activa, "
        "`challenge_id` (de `/api/face/challenge`, pedido por la empresa) + `challenge_image` + `flash_image`. "
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
    liveness: Liveness,
    camera_label: CameraLabel = None,
) -> ApiResponse[EnrollmentSubmitResponse]:
    employee = EmployeeService(db, company).get(employee_id)
    result = EnrollmentService(db, company).enroll_in_person(
        employee,
        operator,
        read_image_uploads(images, max_files=settings.FACE_ENROLL_MAX_PHOTOS),
        pipeline,
        liveness=liveness,
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
    liveness: Liveness,
    client: OperatorClient,
    camera_label: CameraLabel = None,
) -> ApiResponse[VerificationResult]:
    employee = EmployeeService(db, company).get(employee_id)
    ip, user_agent = request_meta(request)
    result = VerificationService(db, ip=ip, user_agent=user_agent).verify_in_person(
        employee,
        operator,
        read_image_uploads(images, max_files=3),
        pipeline,
        liveness=liveness,
        camera_label=camera_label,
        client=client,
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
    return ok(QrService(db).summary(employee), code="QR_SUMMARY")


@router.delete(
    "/{employee_id}/qr",
    response_model=ApiResponse[EmployeeQrSummary],
    summary="Invalidar el QR vigente (el teléfono del empleado muestra otro)",
    responses=NOT_FOUND,
    dependencies=[Depends(require_screen(Screen.COMPANY_EMPLOYEES))],
)
def revoke_qr(employee_id: int, company: CompanyScope, db: DbSession) -> ApiResponse[EmployeeQrSummary]:
    employee = EmployeeService(db, company).get(employee_id)
    return ok(QrService(db).revoke(employee), code="QR_REVOKED")


# ---------------- Dispositivos (antifraude 1b, decisión D2) ----------------

DEVICE_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "Empleado o dispositivo no encontrado"}
}


@router.get(
    "/{employee_id}/devices",
    response_model=ApiResponse[EmployeeDeviceList],
    summary="Dispositivos desde los que el empleado checa (el más reciente primero)",
    description=(
        "Cada navegador o teléfono desde el que el empleado registró asistencia o verificó su identidad, por la llave "
        "que la app genera en él (solo su hash): nombre, estado (por decidir, aprobado o revocado), primer y último "
        "uso. Con el modo «Aprobación de la empresa», los registros desde uno sin aprobar quedan en revisión."
    ),
    responses=DEVICE_NOT_FOUND,
    dependencies=[Depends(require_screen(Screen.COMPANY_EMPLOYEES))],
)
def list_employee_devices(
    employee_id: int, _: CompanyUser, company: CompanyScope, db: DbSession, page: Pagination
) -> ApiResponse[EmployeeDeviceList]:
    employee = EmployeeService(db, company).get(employee_id)
    result = EmployeeDeviceService(db, company).list(employee, page)
    return ok(result, code="EMPLOYEE_DEVICES_LISTED", key="DEVICES_LISTED", params={"count": result.total})


@router.patch(
    "/{employee_id}/devices/{device_id}/status",
    response_model=ApiResponse[EmployeeDeviceRead],
    summary="Aprobar o revocar un dispositivo del empleado",
    description=(
        "`APPROVED` lo aprueba (desde uno por decidir o revocado): sus registros dejan de quedar en revisión por el "
        "dispositivo. `REVOKED` lo revoca (desde uno por decidir o aprobado): vuelve a ser desconocido. Otro cambio: "
        "409 `DEVICE_INVALID_TRANSITION`."
    ),
    responses={**DEVICE_NOT_FOUND, 409: {"model": ErrorResponse, "description": "Cambio no permitido"}},
    dependencies=[Depends(require_screen(Screen.COMPANY_EMPLOYEES))],
)
def set_employee_device_status(
    employee_id: int,
    device_id: int,
    payload: EmployeeDeviceStatusUpdate,
    operator: CompanyUser,
    company: CompanyScope,
    db: DbSession,
) -> ApiResponse[EmployeeDeviceRead]:
    employee = EmployeeService(db, company).get(employee_id)
    device = EmployeeDeviceService(db, company).set_status(employee, device_id, payload.status, operator)
    key = "EMPLOYEE_DEVICE_APPROVED" if payload.status == DeviceStatus.APPROVED else "EMPLOYEE_DEVICE_REVOKED"
    return ok(device, code="EMPLOYEE_DEVICE_UPDATED", key=key)


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
    return ok(logs, code="VERIFICATIONS_LISTED", params={"count": logs.total})
