from fastapi import APIRouter

from app.core.exceptions import NotFoundError
from app.core.responses import ApiResponse, ok
from app.dependencies import CurrentUser, DbSession, EmployeeUser
from app.schemas.common import ErrorResponse
from app.schemas.qr import EmployeeQrRead
from app.schemas.user import UserPreferences, UserPreferencesUpdate, UserRead
from app.services.qr_service import QrService
from app.services.verification_service import VerificationService

router = APIRouter(prefix="/users", tags=["Usuarios"])


@router.get("/me", response_model=ApiResponse[UserRead], summary="Información del usuario autenticado")
def me(user: CurrentUser) -> ApiResponse[UserRead]:
    return ok(UserRead.model_validate(user), "Usuario autenticado", code="USER_PROFILE")


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


@router.get(
    "/me/qr",
    response_model=ApiResponse[EmployeeQrRead],
    summary="EMPLOYEE: mi código QR de identidad",
    description=(
        "Disponible solo cuando COMPANY ya aprobó la identidad del empleado "
        "(`face_status=APPROVED`); si no, 403 `FACE_NOT_APPROVED`. El QR no contiene datos "
        "personales: solo un token aleatorio cuyo hash se guarda en la BD."
    ),
    responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
def my_qr(user: EmployeeUser, db: DbSession) -> ApiResponse[EmployeeQrRead]:
    employee = VerificationService._employee_of(user)  # activo + identidad aprobada
    qr_service = QrService(db)
    qr = qr_service.repo.get_active_for_employee(employee.id)
    if qr is None:
        raise NotFoundError(
            "Tu código QR no está disponible. Solicita a tu empresa que lo genere de nuevo.", code="QR_NOT_FOUND"
        )
    return ok(qr_service.to_read(employee, qr), "Tu código QR de identidad", code="MY_QR")
