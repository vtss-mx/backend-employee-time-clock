from fastapi import APIRouter, Depends, status

from app.core.responses import ApiResponse, ok
from app.dependencies import CurrentUser, DbSession, EmployeeUser, qr_rate_limit, require_screen
from app.models import Screen
from app.schemas.common import ErrorResponse
from app.schemas.qr import DynamicQrRead, QrStatusRead
from app.schemas.user import UserPreferences, UserPreferencesUpdate, UserRead
from app.services.navigation_service import user_read
from app.services.preferences_service import update_preferences
from app.services.qr_service import QrService

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
    return ok(update_preferences(db, user, changes), "Preferencias guardadas", code="PREFERENCES_UPDATED")


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
    dependencies=[Depends(require_screen(Screen.EMPLOYEE_QR)), Depends(qr_rate_limit)],
)
def issue_my_qr(user: EmployeeUser, db: DbSession) -> ApiResponse[DynamicQrRead]:
    return ok(QrService(db).issue_for(user), "Tu código QR", code="MY_QR", status_code=201)


@router.get(
    "/me/qr/{qr_id}",
    response_model=ApiResponse[QrStatusRead],
    summary="EMPLOYEE: estado de mi código QR (vigente, usado, vencido o reemplazado)",
    description="La webapp lo consulta para mostrar otro en cuanto un validador lo usa.",
    responses={404: {"model": ErrorResponse}},
    dependencies=[Depends(require_screen(Screen.EMPLOYEE_QR))],
)
def my_qr_status(qr_id: int, user: EmployeeUser, db: DbSession) -> ApiResponse[QrStatusRead]:
    return ok(QrService(db).status_for(user, qr_id), "Estado de tu código QR", code="MY_QR_STATUS")
