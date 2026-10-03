"""Dependencias compartidas: sesión de BD, usuario autenticado, autorización por rol."""

from collections.abc import Callable, Generator
from typing import Annotated

from fastapi import Depends, File, Form, Query, Request, UploadFile
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.exceptions import (
    AuthenticationError,
    ConflictError,
    PayloadTooLargeError,
    PermissionDeniedError,
    ServiceUnavailableError,
    UnprocessableError,
)
from app.core.tokens import decode_access_token
from app.facial_recognition import (
    FaceEngineUnavailable,
    FacePipeline,
    QueueFullError,
    QueueTimeoutError,
    lease_pipeline,
)
from app.facial_recognition.image_utils import ALLOWED_CONTENT_TYPES
from app.middleware.rate_limit import enforce
from app.models import Screen, User, UserRole
from app.schemas.common import PageParams
from app.services.auth_service import ensure_account_usable
from app.services.catalog_service import get_catalogs
from app.services.policy_service import ensure_device_allowed
from app.services.session_service import SessionService

_bearer = HTTPBearer(
    auto_error=False,
    bearerFormat="JWT",
    description=(
        "Access token JWT (ES256, 12 h) obtenido en `POST /api/auth/login` (campo "
        "`data.access_token`). Renovación: `POST /api/auth/refresh` con la cookie HttpOnly."
    ),
)

DbSession = Annotated[Session, Depends(get_db)]


def _page_params(
    page: Annotated[int, Query(ge=1, description="Página (desde 1)")] = 1,
    size: Annotated[
        int, Query(ge=1, le=settings.PAGE_SIZE_MAX, description="Elementos por página")
    ] = settings.PAGE_SIZE_DEFAULT,
) -> PageParams:
    return PageParams(page=page, size=size)


#: Nombre de la cámara con que se capturó (el `label` de la pista de video): las cámaras virtuales
#: (programas que inyectan video) se rechazan con 422 VIRTUAL_CAMERA.
CameraLabel = Annotated[
    str | None, Form(max_length=200, description="Nombre de la cámara usada (las cámaras virtuales se rechazan)")
]

#: `page` y `size` de todo listado paginado: mismos límites y valor por omisión en toda la API.
Pagination = Annotated[PageParams, Depends(_page_params)]


def get_current_user(
    request: Request,
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthenticationError("No autenticado")
    payload = decode_access_token(credentials.credentials)
    try:
        user_id = int(payload["sub"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AuthenticationError("Token inválido", code="TOKEN_INVALID") from exc

    # La sesión (y su usuario, en la misma consulta) se valida en cada petición: cerrar
    # sesión, revocarla, desactivar al usuario o cambiar su rol surte efecto de inmediato
    # aunque el JWT siga vigente.
    session = SessionService(db).validate(str(payload["sid"]), user_id)
    user = session.user
    # Empresa en la que opera (EMPLOYEE en varias empresas): la fija la sesión del servidor.
    user.use_company(session.company_id)
    request.state.session_id = payload["sid"]
    if payload.get("role") != user.role.value:
        raise AuthenticationError("La sesión ya no es válida", code="TOKEN_INVALID")
    # Cuenta, empleo o empresa desactivados: el acceso se pierde de inmediato.
    ensure_account_usable(user)
    # En cada petición (no solo al iniciar sesión): un token obtenido en el teléfono tampoco
    # sirve desde una computadora o tableta.
    ensure_device_allowed(db, user, request.headers)
    # Fin de la transacción de lectura: la conexión vuelve al pool de inmediato
    # (expire_on_commit=False conserva los objetos). Así una petición que después espera
    # trabajo largo (p. ej. la cola facial) no retiene una conexión de BD ociosa.
    db.commit()
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def optional_token_payload(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> dict | None:
    """Claims del access token si viene y es válido; None en cualquier otro caso (sin error)."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        return None
    try:
        return decode_access_token(credentials.credentials)
    except AuthenticationError:
        return None


OptionalTokenPayload = Annotated[dict | None, Depends(optional_token_payload)]


def require_roles(*roles: UserRole) -> Callable[[User], User]:
    def dependency(user: CurrentUser) -> User:
        if user.role not in roles:
            raise PermissionDeniedError()
        return user

    return dependency


def require_screen(*screens: Screen) -> Callable[[User, Session], User]:
    """Permiso del endpoint: el rol del usuario debe tener alguna de las pantallas que lo usan.

    Las pantallas de cada rol viven en la BD (catalog.role_screens): quitarle una pantalla a un rol
    le quita también los endpoints de esa pantalla, sin desplegar.
    """

    def dependency(user: CurrentUser, db: DbSession) -> User:
        if not get_catalogs(db).grants(user.role.value, screens):
            raise PermissionDeniedError()
        return user

    return dependency


AdminUser = Annotated[User, Depends(require_roles(UserRole.ADMIN))]
CompanyUser = Annotated[User, Depends(require_roles(UserRole.COMPANY))]
#: Validador de identidad de una empresa (tableta o teléfono).
ValidatorUser = Annotated[User, Depends(require_roles(UserRole.VALIDATOR))]
#: Empleado aunque todavía no elija empresa (p. ej. para elegirla).
EmployeeAccount = Annotated[User, Depends(require_roles(UserRole.EMPLOYEE))]


def company_of(user: User) -> int:
    """Empresa en la que opera el usuario: el alcance de TODOS sus datos (nunca viene del cliente)."""
    company = user.current_company
    if company is not None:
        return company.id
    if user.role == UserRole.EMPLOYEE:
        raise ConflictError("Elige la empresa a la que quieres entrar", code="COMPANY_SELECTION_REQUIRED")
    raise PermissionDeniedError("Esta acción corresponde a una empresa", code="COMPANY_REQUIRED")


def _employee_in_company(user: EmployeeAccount) -> User:
    company_of(user)  # 409 si trabaja en varias empresas y aún no elige
    return user


#: Empleado operando en una empresa (ya elegida en su sesión).
EmployeeUser = Annotated[User, Depends(_employee_in_company)]


def _company_scope(user: CompanyUser) -> int:
    return company_of(user)


def _member_company(user: CurrentUser) -> int:
    return company_of(user)


#: Empresa del administrador COMPANY autenticado (rutas de administración de la empresa).
CompanyScope = Annotated[int, Depends(_company_scope)]
#: Empresa del usuario autenticado, sea COMPANY, VALIDATOR o EMPLOYEE.
MemberCompany = Annotated[int, Depends(_member_company)]


def get_pipeline() -> Generator[FacePipeline, None, None]:
    """Reserva un worker facial durante la petición (1 por núcleo, cola FIFO acotada).

    - Cola llena  → 503 inmediato (backpressure): nunca se acumulan peticiones sin límite.
    - Espera larga → 503 reintentable.
    - Modelos caídos → 503 y el resto de la API sigue operando (circuit breaker).
    """
    try:
        lease = lease_pipeline()
        pipeline = lease.__enter__()
    except FaceEngineUnavailable as exc:
        raise ServiceUnavailableError(
            get_catalogs().face_error_message("FACE_SERVICE_UNAVAILABLE"),
            code="FACE_SERVICE_UNAVAILABLE",
            retry_after=settings.FACE_ENGINE_RETRY_SECONDS,
        ) from exc
    except (QueueFullError, QueueTimeoutError) as exc:
        raise ServiceUnavailableError(
            get_catalogs().face_error_message("FACE_SERVICE_BUSY"),
            code="FACE_SERVICE_BUSY",
            retry_after=3,
        ) from exc
    try:
        yield pipeline
    finally:
        lease.__exit__(None, None, None)


Pipeline = Annotated[FacePipeline, Depends(get_pipeline)]


def checkpoint_rate_limit(user: CurrentUser) -> None:
    """Validador: atiende a muchos empleados seguidos desde el mismo dispositivo (límite propio)."""
    enforce(f"checkpoint:user:{user.id}", settings.RATE_LIMIT_CHECKPOINT_PER_MINUTE)


def verification_rate_limit(user: CurrentUser) -> None:
    """Limita intentos de verificación por usuario (no por IP: varios empleados
    pueden compartir la IP de la oficina)."""
    enforce(f"verify:user:{user.id}", settings.RATE_LIMIT_VERIFY_PER_MINUTE)


def read_image_upload(file: UploadFile) -> bytes:
    """Lee un UploadFile validando tipo declarado y tamaño máximo sin cargar más de lo permitido."""
    if file.content_type and file.content_type.lower() not in ALLOWED_CONTENT_TYPES:
        raise UnprocessableError("Formato de imagen no permitido (usa JPEG, PNG o WEBP)", code="INVALID_IMAGE_FORMAT")
    max_bytes = settings.max_image_bytes
    data = file.file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise PayloadTooLargeError(f"La imagen excede el tamaño máximo de {settings.MAX_IMAGE_SIZE_MB} MB")
    if not data:
        raise UnprocessableError("No se recibió ninguna imagen", code="EMPTY_IMAGE")
    return data


def read_image_uploads(files: list[UploadFile], *, max_files: int) -> list[bytes]:
    if not files:
        raise UnprocessableError("No se recibió ninguna imagen", code="EMPTY_IMAGE")
    if len(files) > max_files:
        raise UnprocessableError(f"Máximo {max_files} imágenes por solicitud", code="TOO_MANY_IMAGES")
    return [read_image_upload(f) for f in files]


#: Una captura con la cabeza girada por cada giro del reto, en orden (el campo se repite).
ChallengeImages = Annotated[
    list[UploadFile] | None,
    File(description="Captura con la cabeza girada por cada giro del reto, en orden (`challenge_image` repetido)"),
]
#: Giros que puede pedir un reto (verification_policy.liveness_steps).
MAX_CHALLENGE_STEPS = 2


def read_challenge_images(files: list[UploadFile] | None) -> list[bytes]:
    """Capturas de los giros del reto (ninguna si no se envió el reto)."""
    return read_image_uploads(files, max_files=MAX_CHALLENGE_STEPS) if files else []


def face_check_rate_limit(user: CurrentUser) -> None:
    enforce(f"face-check:user:{user.id}", settings.RATE_LIMIT_FACE_CHECK_PER_MINUTE)


def request_meta(request: Request) -> tuple[str | None, str | None]:
    ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")
    return ip, (user_agent[:255] if user_agent else None)
