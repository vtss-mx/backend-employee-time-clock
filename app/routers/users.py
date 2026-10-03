from fastapi import APIRouter, Depends, status

from app.core.config import settings
from app.core.responses import ApiResponse, ok
from app.dependencies import CurrentUser, DbSession, EmployeeUser, require_screen
from app.middleware.rate_limit import enforce
from app.models import Screen
from app.schemas.common import ErrorResponse
from app.schemas.qr import DynamicQrRead, QrStatusRead
from app.schemas.user import UserPreferences, UserPreferencesUpdate, UserRead
from app.services.navigation_service import user_read
from app.services.policy_service import PolicyService
from app.services.qr_service import QrService
from app.services.verification_service import VerificationService

router = APIRouter(prefix="/users", tags=["Usuarios"])


@router.get("/me", response_model=ApiResponse[UserRead], summary="Información del usuario autenticado")
def me(user: CurrentUser) -> ApiResponse[UserRead]:
    return ok(user_read(user), "Usuario autenticado", code="USER_PROFILE")


@router.patch(
    "/me/preferences",
    response_model=ApiResponse[UserPreferences],
    summary="Guardar mis preferencias de la interfaz (p. ej. menú contraído)",
    description="Cambio parcial. Las preferencias viven en la BD y siguen al usuario en cualquier dispositivo.",
)
def update_my_preferences(
    changes: UserPreferencesUpdate, user: CurrentUser, db: DbSession
) -> ApiResponse[UserPreferences]:
    current = UserPreferences.model_validate(user.preferences or {})
    updated = current.model_copy(update=changes.model_dump(exclude_unset=True, exclude_none=True))
    user.preferences = updated.model_dump()  # nuevo dict: SQLAlchemy detecta el cambio del JSON
    db.commit()
    return ok(updated, "Preferencias guardadas", code="PREFERENCES_UPDATED")


@router.post(
    "/me/qr",
    response_model=ApiResponse[DynamicQrRead],
    status_code=status.HTTP_201_CREATED,
    summary="EMPLOYEE: generar mi código QR dinámico",
    description=(
        "Emite un QR nuevo y reemplaza el anterior. Vive `lifetime_seconds` (política de la empresa) y "
        "sirve UNA sola vez: al usarse, vencer o pedir otro ya no vuelve a servir. Solo con la identidad "
        "aprobada (`face_status=APPROVED`; si no, 403 `FACE_NOT_APPROVED`) y el QR habilitado por la "
        "empresa (si no, 403 `QR_DISABLED`). El QR no contiene datos personales: solo un token aleatorio "
        "del que la BD guarda su hash."
    ),
    responses={403: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
    dependencies=[Depends(require_screen(Screen.EMPLOYEE_QR))],
)
def issue_my_qr(user: EmployeeUser, db: DbSession) -> ApiResponse[DynamicQrRead]:
    employee = VerificationService._employee_of(user)  # activo + identidad aprobada
    enforce(f"qr:user:{user.id}", settings.RATE_LIMIT_QR_PER_MINUTE)
    policies = PolicyService(db, employee.company_id)
    policies.ensure_qr_enabled()
    lifetime = policies.current().qr_lifetime_seconds
    qr_service = QrService(db)
    qr, content = qr_service.issue(employee, lifetime)
    db.commit()
    db.refresh(qr)
    return ok(qr_service.to_read(employee, qr, content, lifetime), "Tu código QR", code="MY_QR", status_code=201)


@router.get(
    "/me/qr/{qr_id}",
    response_model=ApiResponse[QrStatusRead],
    summary="EMPLOYEE: estado de mi código QR (vigente, usado, vencido o reemplazado)",
    description="La webapp lo consulta para mostrar otro en cuanto un validador lo usa.",
    responses={404: {"model": ErrorResponse}},
    dependencies=[Depends(require_screen(Screen.EMPLOYEE_QR))],
)
def my_qr_status(qr_id: int, user: EmployeeUser, db: DbSession) -> ApiResponse[QrStatusRead]:
    employee = VerificationService._employee_of(user)
    return ok(QrService(db).status(employee, qr_id), "Estado de tu código QR", code="MY_QR_STATUS")
