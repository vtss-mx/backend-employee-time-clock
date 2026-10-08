"""Consola de la plataforma: alta y administración de empresas. Solo rol ADMIN.

El ADMIN configura cada empresa: sus datos, su plan, sus módulos (Integraciones), su política de
verificación de identidad (con su historial, niveles predefinidos, regla de dos personas para relajarla y el
motor de riesgo con su simulación) y el aprendizaje del reconocimiento facial (la empresa no lo ve ni lo
configura). De sus empleados solo consulta la ficha de trabajo, de solo lectura; nunca datos fiscales
ni biométricos (privacidad por diseño).
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status

from app.core.responses import ApiResponse, ok
from app.dependencies import AdminUser, DbSession, Pagination, require_screen, trash_of
from app.models import PolicyChangeStatus, Screen
from app.schemas.common import ErrorResponse
from app.schemas.company import (
    CompanyAdminCreate,
    CompanyAdminList,
    CompanyAdminPasswordReset,
    CompanyAdminRead,
    CompanyCreate,
    CompanyDetail,
    CompanyEmployeeList,
    CompanyEmployeeRead,
    CompanyList,
    CompanyStatusUpdate,
    CompanyUpdate,
    PlatformStats,
)
from app.schemas.employee import EmployeeStatusUpdate
from app.schemas.face import FaceLearningSummary
from app.schemas.policy import (
    AdminPolicyRead,
    PolicyChangeDecision,
    PolicyChangeList,
    PolicyChangeRead,
    PolicyPresetApply,
    PolicyUpdateResult,
    RiskPolicyCandidate,
    RiskSimulationRead,
    VerificationPolicyUpdate,
)
from app.services.company_service import CompanyService
from app.services.policy_governance import PolicyGovernance

router = APIRouter(
    prefix="/admin",
    tags=["Plataforma (ADMIN)"],
    responses={
        401: {"model": ErrorResponse, "description": "No autenticado"},
        403: {"model": ErrorResponse, "description": "Solo el administrador de la plataforma"},
    },
)
NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": ErrorResponse, "description": "Empresa no encontrada"}}
CONFLICT: dict[int | str, dict[str, Any]] = {
    409: {"model": ErrorResponse, "description": "Identificador fiscal o correo ya registrados"}
}
#: La papelera de empresas la ve solo la pantalla que las elimina y restaura (no el tablero).
CompanyTrash = Annotated[bool, Depends(trash_of(Screen.ADMIN_COMPANIES))]


@router.get(
    "/stats",
    response_model=ApiResponse[PlatformStats],
    summary="Indicadores de la plataforma",
    dependencies=[Depends(require_screen(Screen.ADMIN_DASHBOARD))],
)
def platform_stats(_: AdminUser, db: DbSession) -> ApiResponse[PlatformStats]:
    return ok(CompanyService(db).stats(), code="PLATFORM_STATS")


@router.get(
    "/companies",
    response_model=ApiResponse[CompanyList],
    summary="Listar/buscar empresas",
    description="`deleted=true`: la papelera («Eliminados»), con quién y cuándo eliminó cada una.",
    dependencies=[Depends(require_screen(Screen.ADMIN_COMPANIES, Screen.ADMIN_DASHBOARD))],
)
def list_companies(
    _: AdminUser,
    db: DbSession,
    page: Pagination,
    deleted: CompanyTrash,
    search: Annotated[
        str | None, Query(max_length=100, description="Nombre, razón social o identificador fiscal")
    ] = None,
    active: Annotated[bool | None, Query(description="Filtrar por estado")] = None,
) -> ApiResponse[CompanyList]:
    result = CompanyService(db).list_companies(search=search, active=active, page=page, deleted=deleted)
    return ok(result, code="COMPANIES_LISTED", params={"count": result.total})


@router.post(
    "/companies",
    response_model=ApiResponse[CompanyDetail],
    status_code=status.HTTP_201_CREATED,
    summary="Dar de alta una empresa con su primer administrador y su plan de cobro",
    responses=CONFLICT,
    dependencies=[Depends(require_screen(Screen.ADMIN_COMPANIES))],
)
def create_company(payload: CompanyCreate, user: AdminUser, db: DbSession) -> ApiResponse[CompanyDetail]:
    detail = CompanyService(db).create(payload, user.email)
    return ok(detail, code="COMPANY_CREATED", status_code=201)


@router.get(
    "/companies/{company_id}",
    response_model=ApiResponse[CompanyDetail],
    summary="Detalle (también de una empresa en «Eliminados», con `deleted_at`)",
    responses=NOT_FOUND,
    dependencies=[Depends(require_screen(Screen.ADMIN_COMPANIES))],
)
def get_company(company_id: int, _: AdminUser, db: DbSession) -> ApiResponse[CompanyDetail]:
    return ok(CompanyService(db).detail(company_id, include_deleted=True), code="COMPANY_FOUND")


@router.get(
    "/companies/{company_id}/employees",
    response_model=ApiResponse[CompanyEmployeeList],
    summary="Empleados de una empresa (paginado, solo lectura)",
    description=(
        "Su ficha de trabajo: número, nombre, departamento, correo, teléfono, si está activo y el estado de su "
        "registro facial. Sin datos fiscales, fecha de nacimiento ni nada biométrico."
    ),
    responses=NOT_FOUND,
    dependencies=[Depends(require_screen(Screen.ADMIN_COMPANIES))],
)
def company_employees(
    company_id: int,
    _: AdminUser,
    db: DbSession,
    page: Pagination,
    search: Annotated[str | None, Query(max_length=100, description="Nombre, número o correo")] = None,
    active: Annotated[bool | None, Query(description="Solo activos o inactivos")] = None,
) -> ApiResponse[CompanyEmployeeList]:
    result = CompanyService(db).employees(company_id, search=search, active=active, page=page)
    return ok(result, code="COMPANY_EMPLOYEES", params={"count": result.total})


@router.get(
    "/companies/{company_id}/face-learning",
    response_model=ApiResponse[FaceLearningSummary],
    summary="Evolución del reconocimiento facial de una empresa",
    description=(
        "Lo que su galería aprendió de identificaciones seguras: empleados que ya aprenden, muestras "
        "aprendidas vigentes y cuántas identificaciones decidieron. Solo el ADMIN lo ve."
    ),
    responses=NOT_FOUND,
    dependencies=[Depends(require_screen(Screen.ADMIN_COMPANIES))],
)
def company_face_learning(company_id: int, _: AdminUser, db: DbSession) -> ApiResponse[FaceLearningSummary]:
    summary = CompanyService(db).face_learning(company_id)
    return ok(summary, code="FACE_LEARNING_SUMMARY")


@router.delete(
    "/companies/{company_id}/employees/{employee_id}/face/learned",
    response_model=ApiResponse[CompanyEmployeeRead],
    summary="Olvidar lo que el reconocimiento aprendió de un empleado",
    description=(
        "Borra las muestras aprendidas de sus identificaciones: vuelve a compararse solo con su registro "
        "aprobado, que no se toca. No cambia su estado facial."
    ),
    responses={404: {"model": ErrorResponse, "description": "Empresa o empleado no encontrado"}},
    dependencies=[Depends(require_screen(Screen.ADMIN_COMPANIES))],
)
def forget_learned_face(
    company_id: int, employee_id: int, _: AdminUser, db: DbSession
) -> ApiResponse[CompanyEmployeeRead]:
    employee, removed = CompanyService(db).forget_learned_face(company_id, employee_id)
    return ok(employee, code="FACE_LEARNING_FORGOTTEN", params={"count": removed})


@router.get(
    "/companies/{company_id}/verification-policy",
    response_model=ApiResponse[AdminPolicyRead],
    summary="Política de verificación de identidad de una empresa (con el motor de riesgo)",
    description=(
        "Lo que leen la empresa y su personal más lo que solo configura el ADMIN: el motor de riesgo (cortes, "
        "acción por nivel, respaldo y cada señal con su línea base de fraudes confirmados y falsos positivos), la "
        "sospecha de duplicado, el dispositivo del empleado, la evidencia y los cambios por aprobar."
    ),
    responses=NOT_FOUND,
    dependencies=[Depends(require_screen(Screen.ADMIN_COMPANIES))],
)
def company_policy(company_id: int, _: AdminUser, db: DbSession) -> ApiResponse[AdminPolicyRead]:
    CompanyService(db).get(company_id)  # 404 si la empresa no existe
    return ok(PolicyGovernance(db, company_id).read(), code="POLICY")


@router.put(
    "/companies/{company_id}/verification-policy",
    response_model=ApiResponse[PolicyUpdateResult],
    summary="Cambiar la política de verificación de una empresa",
    description=(
        "El ADMIN de la plataforma es el responsable: exigir retirar lentes (apagado por omisión), gorra o "
        "cubrebocas, prueba de vida, "
        "anti-spoofing, QR, nivel de confianza, candados contra engaños, aprendizaje continuo y el motor de riesgo. "
        "Solo se modifican los campos enviados. Cada cambio queda en el historial (antes → después). Lo que endurece "
        "aplica en segundos a todos los procesos; lo que RELAJA la seguridad queda por aprobar de otro ADMIN (regla de "
        "dos personas: `change.status = PENDING`, la política sigue igual) y, si toca el motor de riesgo, lleva la "
        "simulación de los últimos días."
    ),
    responses={**NOT_FOUND, 422: {"model": ErrorResponse}},
    dependencies=[Depends(require_screen(Screen.ADMIN_COMPANIES))],
)
def update_company_policy(
    company_id: int, payload: VerificationPolicyUpdate, user: AdminUser, db: DbSession
) -> ApiResponse[PolicyUpdateResult]:
    CompanyService(db).get(company_id)
    result = PolicyGovernance(db, company_id).request(payload, user)
    return _policy_result(result)


@router.post(
    "/companies/{company_id}/verification-policy/preset",
    response_model=ApiResponse[PolicyUpdateResult],
    summary="Aplicar un nivel predefinido (Estándar, Alto o Máximo)",
    description="Fija los controles del nivel por el mismo camino que un cambio (historial y regla de dos personas).",
    responses={**NOT_FOUND, 422: {"model": ErrorResponse}},
    dependencies=[Depends(require_screen(Screen.ADMIN_COMPANIES))],
)
def apply_policy_preset(
    company_id: int, payload: PolicyPresetApply, user: AdminUser, db: DbSession
) -> ApiResponse[PolicyUpdateResult]:
    CompanyService(db).get(company_id)
    result = PolicyGovernance(db, company_id).apply_preset(payload.preset, payload.reason, user)
    return _policy_result(result)


def _policy_result(result: PolicyUpdateResult) -> ApiResponse[PolicyUpdateResult]:
    if result.change is None:
        return ok(result, code="POLICY_UNCHANGED")
    if result.change.status == PolicyChangeStatus.PENDING:
        return ok(
            result,
            code="POLICY_CHANGE_PENDING",
        )
    return ok(result, code="POLICY_UPDATED")


@router.get(
    "/companies/{company_id}/verification-policy/changes",
    response_model=ApiResponse[PolicyChangeList],
    summary="Historial de la política (quién, cuándo, antes → después) y cambios por aprobar",
    responses=NOT_FOUND,
    dependencies=[Depends(require_screen(Screen.ADMIN_COMPANIES))],
)
def policy_changes(
    company_id: int,
    user: AdminUser,
    db: DbSession,
    page: Pagination,
    change_status: Annotated[PolicyChangeStatus | None, Query(alias="status")] = None,
) -> ApiResponse[PolicyChangeList]:
    CompanyService(db).get(company_id)
    result = PolicyGovernance(db, company_id).history(status=change_status, page=page, viewer=user)
    return ok(result, code="POLICY_CHANGES", params={"count": result.total})


@router.post(
    "/companies/{company_id}/verification-policy/changes/{change_id}/approve",
    response_model=ApiResponse[PolicyUpdateResult],
    summary="Aprobar un cambio que relaja la seguridad (otro ADMIN)",
    description=(
        "Solo un ADMIN distinto de quien lo pidió (409 `POLICY_SELF_APPROVAL`). 409 `POLICY_CHANGE_EXPIRED` si venció "
        "y `POLICY_CHANGED_SINCE` si la política cambió después de pedirlo (en ambos casos queda cancelado)."
    ),
    responses={**NOT_FOUND, 409: {"model": ErrorResponse}},
    dependencies=[Depends(require_screen(Screen.ADMIN_COMPANIES))],
)
def approve_policy_change(
    company_id: int, change_id: int, user: AdminUser, db: DbSession
) -> ApiResponse[PolicyUpdateResult]:
    CompanyService(db).get(company_id)
    result = PolicyGovernance(db, company_id).approve(change_id, user)
    return ok(result, code="POLICY_CHANGE_APPROVED")


@router.post(
    "/companies/{company_id}/verification-policy/changes/{change_id}/reject",
    response_model=ApiResponse[PolicyChangeRead],
    summary="Rechazar un cambio por aprobar (con su motivo)",
    responses={**NOT_FOUND, 409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
    dependencies=[Depends(require_screen(Screen.ADMIN_COMPANIES))],
)
def reject_policy_change(
    company_id: int, change_id: int, payload: PolicyChangeDecision, user: AdminUser, db: DbSession
) -> ApiResponse[PolicyChangeRead]:
    CompanyService(db).get(company_id)
    result = PolicyGovernance(db, company_id).reject(change_id, user, payload.note)
    return ok(result, code="POLICY_CHANGE_REJECTED")


@router.post(
    "/companies/{company_id}/verification-policy/changes/{change_id}/cancel",
    response_model=ApiResponse[PolicyChangeRead],
    summary="Retirar un cambio propio por aprobar",
    responses={**NOT_FOUND, 409: {"model": ErrorResponse}},
    dependencies=[Depends(require_screen(Screen.ADMIN_COMPANIES))],
)
def cancel_policy_change(
    company_id: int, change_id: int, user: AdminUser, db: DbSession
) -> ApiResponse[PolicyChangeRead]:
    CompanyService(db).get(company_id)
    result = PolicyGovernance(db, company_id).cancel(change_id, user)
    return ok(result, code="POLICY_CHANGE_CANCELLED")


@router.post(
    "/companies/{company_id}/verification-policy/simulate",
    response_model=ApiResponse[RiskSimulationRead],
    summary="Simular una configuración del motor de riesgo sobre los últimos días",
    description=(
        "Vuelve a decidir los intentos guardados de la empresa (con tope: RISK_SIMULATION_MAX_ATTEMPTS en "
        "RISK_SIMULATION_DAYS días) con la configuración vigente y con la candidata, con las mismas reglas del motor. "
        "No guarda nada. Solo cuenta lo que se midió en su momento."
    ),
    responses={**NOT_FOUND, 422: {"model": ErrorResponse}},
    dependencies=[Depends(require_screen(Screen.ADMIN_COMPANIES))],
)
def simulate_policy(
    company_id: int, payload: RiskPolicyCandidate, _: AdminUser, db: DbSession
) -> ApiResponse[RiskSimulationRead]:
    CompanyService(db).get(company_id)
    result = PolicyGovernance(db, company_id).simulate(payload)
    return ok(result, code="POLICY_SIMULATED", params={"count": result.evaluated})


@router.put(
    "/companies/{company_id}",
    response_model=ApiResponse[CompanyDetail],
    summary="Editar datos de la empresa (solo los campos enviados)",
    responses={**NOT_FOUND, **CONFLICT},
    dependencies=[Depends(require_screen(Screen.ADMIN_COMPANIES))],
)
def update_company(company_id: int, payload: CompanyUpdate, _: AdminUser, db: DbSession) -> ApiResponse[CompanyDetail]:
    return ok(CompanyService(db).update(company_id, payload), code="COMPANY_UPDATED")


@router.patch(
    "/companies/{company_id}/status",
    response_model=ApiResponse[CompanyDetail],
    summary="Activar o desactivar (desactivar cierra las sesiones de todo su personal)",
    responses=NOT_FOUND,
    dependencies=[Depends(require_screen(Screen.ADMIN_COMPANIES))],
)
def set_company_status(
    company_id: int, payload: CompanyStatusUpdate, _: AdminUser, db: DbSession
) -> ApiResponse[CompanyDetail]:
    detail = CompanyService(db).set_active(company_id, payload.active)
    key = "COMPANY_ACTIVATED" if payload.active else "COMPANY_DEACTIVATED"
    return ok(detail, code="COMPANY_STATUS_UPDATED", key=key)


@router.delete(
    "/companies/{company_id}",
    response_model=ApiResponse[None],
    summary="Eliminar una empresa sin empleados ni cobranza (a «Eliminados»; con empleados se desactiva)",
    description=(
        "Borrado lógico de la empresa con sus cuentas y validadores: sus sesiones se cierran y se puede restaurar "
        "durante `SOFT_DELETE_RETENTION_DAYS`. Las fotos de perfil de sus cuentas y la evidencia de sus casos de "
        "fraude se borran de verdad."
    ),
    responses={
        **NOT_FOUND,
        409: {
            "model": ErrorResponse,
            "description": "`COMPANY_HAS_EMPLOYEES`, `COMPANY_HAS_BILLING` o `ALREADY_DELETED`",
        },
    },
    dependencies=[Depends(require_screen(Screen.ADMIN_COMPANIES))],
)
def delete_company(company_id: int, user: AdminUser, db: DbSession) -> ApiResponse[None]:
    CompanyService(db).delete(company_id, user)
    return ok(None, code="COMPANY_DELETED")


@router.post(
    "/companies/{company_id}/restore",
    response_model=ApiResponse[CompanyDetail],
    summary="Restaurar una empresa de «Eliminados» (con las cuentas y validadores que se eliminaron con ella)",
    description="Revisa de nuevo que su identificador fiscal y los correos de sus cuentas sigan libres (409 "
    "`RESTORE_CONFLICT`).",
    responses={
        **NOT_FOUND,
        409: {"model": ErrorResponse, "description": "`NOT_DELETED` o `RESTORE_CONFLICT`"},
    },
    dependencies=[Depends(require_screen(Screen.ADMIN_COMPANIES))],
)
def restore_company(company_id: int, _: AdminUser, db: DbSession) -> ApiResponse[CompanyDetail]:
    return ok(CompanyService(db).restore(company_id), code="COMPANY_RESTORED")


@router.get(
    "/companies/{company_id}/admins",
    response_model=ApiResponse[CompanyAdminList],
    summary="Administradores de la empresa (paginados)",
    responses=NOT_FOUND,
    dependencies=[Depends(require_screen(Screen.ADMIN_COMPANIES))],
)
def list_company_admins(
    company_id: int, _: AdminUser, db: DbSession, page: Pagination
) -> ApiResponse[CompanyAdminList]:
    result = CompanyService(db).list_admins(company_id, page)
    return ok(result, code="COMPANY_ADMINS_LISTED", params={"count": result.total})


@router.get(
    "/companies/{company_id}/admins/{user_id}",
    response_model=ApiResponse[CompanyAdminRead],
    summary="Un administrador de la empresa",
    responses=NOT_FOUND,
    dependencies=[Depends(require_screen(Screen.ADMIN_COMPANIES))],
)
def get_company_admin(company_id: int, user_id: int, _: AdminUser, db: DbSession) -> ApiResponse[CompanyAdminRead]:
    return ok(CompanyService(db).admin(company_id, user_id), code="COMPANY_ADMIN_FOUND")


@router.post(
    "/companies/{company_id}/admins",
    response_model=ApiResponse[CompanyDetail],
    status_code=status.HTTP_201_CREATED,
    summary="Agregar un administrador a la empresa",
    responses={**NOT_FOUND, **CONFLICT},
    dependencies=[Depends(require_screen(Screen.ADMIN_COMPANIES))],
)
def add_company_admin(
    company_id: int, payload: CompanyAdminCreate, _: AdminUser, db: DbSession
) -> ApiResponse[CompanyDetail]:
    detail = CompanyService(db).add_admin(company_id, payload)
    return ok(detail, code="COMPANY_ADMIN_CREATED", status_code=201)


@router.patch(
    "/companies/{company_id}/admins/{user_id}/status",
    response_model=ApiResponse[CompanyDetail],
    summary="Activar o desactivar un administrador de la empresa",
    responses={**NOT_FOUND, 409: {"model": ErrorResponse, "description": "Último administrador activo"}},
    dependencies=[Depends(require_screen(Screen.ADMIN_COMPANIES))],
)
def set_company_admin_status(
    company_id: int, user_id: int, payload: EmployeeStatusUpdate, _: AdminUser, db: DbSession
) -> ApiResponse[CompanyDetail]:
    detail = CompanyService(db).set_admin_active(company_id, user_id, payload.active)
    return ok(detail, code="COMPANY_ADMIN_STATUS_UPDATED")


@router.put(
    "/companies/{company_id}/admins/{user_id}/password",
    response_model=ApiResponse[CompanyDetail],
    summary="Restablecer la contraseña de un administrador de la empresa (contraseña olvidada)",
    description="Asigna una contraseña nueva y cierra de inmediato las sesiones abiertas de ese administrador.",
    responses=NOT_FOUND,
    dependencies=[Depends(require_screen(Screen.ADMIN_COMPANIES))],
)
def reset_company_admin_password(
    company_id: int, user_id: int, payload: CompanyAdminPasswordReset, _: AdminUser, db: DbSession
) -> ApiResponse[CompanyDetail]:
    detail = CompanyService(db).reset_admin_password(company_id, user_id, payload)
    return ok(detail, code="COMPANY_ADMIN_PASSWORD_RESET")
