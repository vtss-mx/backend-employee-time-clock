from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile, status

from app.core.config import settings
from app.core.exceptions import PayloadTooLargeError, UnprocessableError
from app.core.responses import ApiResponse, ok
from app.dependencies import (
    CurrentUser,
    DbSession,
    EmployeeUser,
    Pagination,
    PersonUser,
    avatar_rate_limit,
    qr_rate_limit,
    require_screen,
)
from app.i18n import Megabytes
from app.models import Screen
from app.schemas.avatar import AVATAR_SIZES, AvatarImage, AvatarRead
from app.schemas.common import ErrorResponse
from app.schemas.employee_device import EmployeeDeviceList
from app.schemas.qr import DynamicQrRead, QrStatusRead
from app.schemas.user import UserPreferences, UserPreferencesUpdate, UserRead
from app.services.avatar_image import CropBox
from app.services.avatar_service import AvatarService
from app.services.employee_access import active_employee
from app.services.employee_devices import EmployeeDeviceService
from app.services.navigation_service import user_read
from app.services.preferences_service import update_preferences
from app.services.qr_service import QrService

router = APIRouter(prefix="/users", tags=["Usuarios"])
#: La foto de perfil es de "Mi perfil" (los cuatro roles la tienen en el catálogo; la BD decide).
PROFILE = [Depends(require_screen(Screen.PROFILE))]
_ERRORS: dict[int | str, dict[str, object]] = {
    code: {"model": ErrorResponse} for code in (404, 413, 422, 429, status.HTTP_503_SERVICE_UNAVAILABLE)
}


@router.get("/me", response_model=ApiResponse[UserRead], summary="Información del usuario autenticado")
def me(user: CurrentUser) -> ApiResponse[UserRead]:
    return ok(user_read(user), code="USER_PROFILE")


@router.patch(
    "/me/preferences",
    response_model=ApiResponse[UserPreferences],
    summary="Guardar mis preferencias de la interfaz (p. ej. menú contraído)",
    description="Cambio parcial. Las preferencias viven en la BD y siguen al usuario en cualquier dispositivo.",
)
def update_my_preferences(
    changes: UserPreferencesUpdate, user: CurrentUser, db: DbSession
) -> ApiResponse[UserPreferences]:
    return ok(update_preferences(db, user, changes), code="PREFERENCES_UPDATED")


def read_avatar_upload(file: UploadFile) -> bytes:
    """La foto que se sube, sin leer más de `AVATAR_MAX_MB` (413 `AVATAR_TOO_LARGE`)."""
    limit = int(settings.AVATAR_MAX_MB * 1024 * 1024)
    data = file.file.read(limit + 1)
    if len(data) > limit:
        raise PayloadTooLargeError(code="AVATAR_TOO_LARGE", params={"size": Megabytes(limit)})
    return data


def crop_box(crop_x: int | None, crop_y: int | None, crop_size: int | None) -> CropBox | None:
    """El recorte que eligió la persona (los tres valores o ninguno: sin recorte se toma el centro)."""
    if crop_x is None and crop_y is None and crop_size is None:
        return None
    if crop_x is None or crop_y is None or crop_size is None:
        raise UnprocessableError(code="AVATAR_CROP_INVALID", field="crop")
    return CropBox(crop_x, crop_y, crop_size)


def avatar_cache_control(current: bool) -> str:
    """La URL con la versión vigente no cambia nunca de contenido: el navegador la conserva sin preguntar
    (`AVATAR_CACHE_SECONDS`); sin versión o con una vieja, la vuelve a validar con su ETag. 0 = no se guarda."""
    seconds = settings.AVATAR_CACHE_SECONDS
    if seconds == 0:
        return "no-store"
    return f"private, max-age={seconds}, immutable" if current else "private, no-cache"


_PIXELS = "en píxeles de la imagen ya orientada (como la muestra el navegador)"


@router.put(
    "/me/avatar",
    response_model=ApiResponse[AvatarRead],
    summary="Subir o cambiar mi foto de perfil",
    description=(
        "Multipart: `file` (JPG, PNG o WEBP de hasta `AVATAR_MAX_MB`, reconocido por su contenido) y, opcional, el "
        "recorte cuadrado `crop_x`, `crop_y`, `crop_size` en píxeles de la imagen ya orientada (sin recorte, el "
        "centro). El servidor aplica la orientación, QUITA todos los metadatos (EXIF con GPS incluido), guarda 512 "
        "y 96 px en WebP cifrados en el bucket (nunca en la BD) y devuelve la ruta versionada (`user.avatar`). La "
        "foto anterior sale del bucket. 413 `AVATAR_TOO_LARGE`, 422 `AVATAR_INVALID` / `AVATAR_CROP_INVALID`, "
        "429 (`RATE_LIMIT_AVATAR_PER_MINUTE`), 503 `STORAGE_UNAVAILABLE` (nada cambia)."
    ),
    responses=_ERRORS,
    dependencies=[*PROFILE, Depends(avatar_rate_limit)],
)
def upload_my_avatar(
    user: PersonUser,
    db: DbSession,
    file: Annotated[UploadFile, File(description="La foto: JPG, PNG o WEBP")],
    crop_x: Annotated[int | None, Form(ge=0, le=100_000, description=f"Esquina izquierda, {_PIXELS}")] = None,
    crop_y: Annotated[int | None, Form(ge=0, le=100_000, description=f"Esquina superior, {_PIXELS}")] = None,
    crop_size: Annotated[int | None, Form(ge=1, le=100_000, description=f"Lado del cuadrado, {_PIXELS}")] = None,
) -> ApiResponse[AvatarRead]:
    result = AvatarService(db).replace(user, read_avatar_upload(file), crop_box(crop_x, crop_y, crop_size))
    return ok(result, code="AVATAR_UPDATED")


@router.delete(
    "/me/avatar",
    response_model=ApiResponse[AvatarRead],
    summary="Quitar mi foto de perfil",
    description="Idempotente. Sus objetos salen del bucket por la cola de borrado; se muestran las iniciales.",
    responses=_ERRORS,
    dependencies=[*PROFILE, Depends(avatar_rate_limit)],
)
def remove_my_avatar(user: PersonUser, db: DbSession) -> ApiResponse[AvatarRead]:
    return ok(AvatarService(db).remove(user), code="AVATAR_REMOVED")


@router.get(
    "/{user_id}/avatar",
    response_model=ApiResponse[AvatarImage],
    summary="Foto de perfil de una persona (si puedes verla)",
    description=(
        "La ruta sale de `user.avatar` / `employee.avatar` / `avatar` de cada persona (lleva la versión); se agrega "
        "`size=96|512`. La propia; la empresa (administrador o validador), la de SU gente: empleados activos o "
        "inactivos, validadores y administradores; el ADMIN, la de cualquier cuenta vigente; lo eliminado o de otra "
        "empresa: 404 `AVATAR_NOT_FOUND` (igual que sin foto). "
        "Cabeceras `ETag` y `Cache-Control: private` (inmutable con la versión vigente); `If-None-Match` igual → 304 "
        "sin leer el bucket. Bucket caído: 503 `STORAGE_UNAVAILABLE` (la app muestra las iniciales)."
    ),
    responses=_ERRORS,
    dependencies=PROFILE,
)
def user_avatar(
    user_id: int,
    request: Request,
    response: Response,
    viewer: PersonUser,
    db: DbSession,
    size: Annotated[int, Query(description="Lado del cuadrado en px: 96 o 512")] = 96,
    v: Annotated[str | None, Query(max_length=24, description="Versión (la de la ruta de `avatar`)")] = None,
) -> ApiResponse[AvatarImage] | Response:
    if size not in AVATAR_SIZES:
        raise UnprocessableError(code="AVATAR_SIZE_INVALID", field="size")
    download = AvatarService(db).read(viewer, user_id, size, request.headers.get("If-None-Match"))
    headers = {
        "ETag": download.etag,
        "Cache-Control": avatar_cache_control(v == download.version),
        # La caché del navegador se separa por sesión: otra cuenta en el mismo equipo nunca reusa esta respuesta.
        "Vary": "Authorization",
    }
    if download.image is None:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    response.headers.update(headers)
    return ok(download.image, code="AVATAR")


@router.get(
    "/me/devices",
    response_model=ApiResponse[EmployeeDeviceList],
    summary="EMPLOYEE: mis dispositivos (desde dónde checo)",
    description=(
        "Los navegadores o teléfonos desde los que registré asistencia o verifiqué mi identidad en mi empresa, con su "
        "estado (por decidir, aprobado o revocado por la empresa) y su primer y último uso. Solo lectura."
    ),
    dependencies=PROFILE,
)
def my_devices(user: EmployeeUser, db: DbSession, page: Pagination) -> ApiResponse[EmployeeDeviceList]:
    employee = active_employee(user)
    result = EmployeeDeviceService(db, employee.company_id).list(employee, page, reviewers=False)
    return ok(result, code="MY_DEVICES", key="DEVICES_LISTED", params={"count": result.total})


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
    return ok(QrService(db).issue_for(user), code="MY_QR", status_code=201)


@router.get(
    "/me/qr/{qr_id}",
    response_model=ApiResponse[QrStatusRead],
    summary="EMPLOYEE: estado de mi código QR (vigente, usado, vencido o reemplazado)",
    description="La webapp lo consulta para mostrar otro en cuanto un validador lo usa.",
    responses={404: {"model": ErrorResponse}},
    dependencies=[Depends(require_screen(Screen.EMPLOYEE_QR))],
)
def my_qr_status(qr_id: int, user: EmployeeUser, db: DbSession) -> ApiResponse[QrStatusRead]:
    return ok(QrService(db).status_for(user, qr_id), code="MY_QR_STATUS")
