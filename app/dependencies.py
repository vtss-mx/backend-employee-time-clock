"""Dependencias compartidas: sesión de BD, usuario autenticado, autorización por rol."""

from collections.abc import Callable, Generator, Mapping
from typing import Annotated

from fastapi import Depends, File, Form, Query, Request, Security, UploadFile
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from pydantic import TypeAdapter, ValidationError
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
from app.core.request_context import note_actor, note_company
from app.core.row_security import clear_scope, use_company, use_platform
from app.core.tokens import decode_access_token
from app.facial_recognition import (
    FaceEngineUnavailable,
    FacePipeline,
    QueueFullError,
    QueueTimeoutError,
    lease_pipeline,
)
from app.facial_recognition.image_utils import ALLOWED_CONTENT_TYPES
from app.i18n import Megabytes
from app.middleware.rate_limit import enforce
from app.models import ApiScope, Company, Screen, User, UserRole
from app.schemas.auth import DeviceLocation
from app.schemas.capture import LocationSample
from app.schemas.common import PageParams
from app.services.api_key_service import ApiClient, authenticate, require_scope
from app.services.capture_protocol import max_burst_bytes
from app.services.catalog_service import get_catalogs
from app.services.client_evidence import ClientEvidence, DeviceProofInput, parse_telemetry
from app.services.employee_access import approved_employee
from app.services.liveness_service import MAX_STEPS, LivenessResponse
from app.services.navigation_service import IDENTITY_SCREENS
from app.services.request_signing import RequestProof
from app.services.session_service import SessionService
from app.services.validator_presence import IdentificationRequest
from app.services.validator_service import validators_module

_bearer = HTTPBearer(
    auto_error=False,
    bearerFormat="JWT",
    description=(
        "Access token JWT (ES256, 12 h) obtenido en `POST /api/auth/login` (campo "
        "`data.access_token`). Renovación: `POST /api/auth/refresh` con la cookie HttpOnly."
    ),
)

DbSession = Annotated[Session, Depends(get_db)]


def _platform_db(db: DbSession) -> Session:
    """La sesión de una ruta de la PLATAFORMA: cruza empresas a propósito (`row_security.use_platform`). Solo la
    identidad (iniciar, renovar y cerrar sesión, elegir empresa, cuenta recordada, contraseña) y el reporte de
    fallas de la app: leen la cuenta y sus empleos en todas sus empresas antes de saber en cuál opera. Declárala
    DESPUÉS del usuario autenticado (FastAPI resuelve en ese orden): la ruta queda de la plataforma."""
    use_platform(db)
    return db


#: Sesión de la plataforma para las rutas de identidad (ver `_platform_db`).
PlatformDb = Annotated[Session, Depends(_platform_db)]


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
        raise AuthenticationError()
    # Las mismas reglas que el canal WebSocket (SessionService.authenticate_access).
    user, request.state.session_id = authenticate_request(db, credentials.credentials, request.headers)
    # Quién la hizo, por si termina en error (el registro de errores guarda su correo y rol tal cual).
    # La empresa en que opera (la suya o la elegida en la sesión), ya cargada con la sesión: sin consultas.
    company = user.current_company
    note_actor(user.id, company.id if company else None, email=user.email, role=user.role.value)
    return user


def authenticate_request(db: Session, token: str, headers: Mapping[str, str]) -> tuple[User, str]:
    """Autentica el token (HTTP y canal WebSocket) y fija de quién son los datos de lo que sigue (seguridad por fila,
    `app/core/row_security.py`):

    - autenticar es de la PLATAFORMA: la sesión, la cuenta y los empleos de la persona en TODAS sus empresas;
    - después, la empresa en que opera (la suya o la elegida en la sesión): sus transacciones solo ven y escriben
      esa empresa; el ADMIN es de la plataforma (sus APIs cruzan empresas a propósito); un empleado que aún no
      elige empresa no ve ninguna."""
    use_platform(db)
    user, session_id = SessionService(db).authenticate_access(token, headers)
    # Fin de la transacción de lectura: la conexión vuelve al pool de inmediato (expire_on_commit=False conserva
    # los objetos). Así una petición que después espera trabajo largo (p. ej. la cola facial) no retiene una
    # conexión de BD ociosa, y su siguiente transacción ya empieza con el alcance de abajo.
    db.commit()
    scope_to(db, user)
    return user, session_id


def scope_to(db: Session, user: User) -> None:
    """Los datos que ve la sesión desde su siguiente transacción: la empresa en que opera el usuario; la
    plataforma si es el ADMIN (sus APIs cruzan empresas a propósito); ninguna si aún no elige empresa."""
    company = user.current_company
    if user.role == UserRole.ADMIN:
        use_platform(db)
    elif company is not None:
        use_company(db, company.id)
    else:
        clear_scope(db)


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


def require_screen(*screens: Screen) -> Callable[[User], User]:
    """Permiso del endpoint: el rol del usuario debe tener alguna de las pantallas que lo usan.

    Las pantallas de cada rol viven en la BD (catalog.role_screens): quitarle una pantalla a un rol
    le quita también los endpoints de esa pantalla, sin desplegar. Un endpoint que solo sirve a
    pantallas que exigen la identidad aprobada (`IDENTITY_SCREENS`) exige además el registro facial
    aprobado: mientras tanto el empleado solo usa su identidad y su cuenta.
    """

    def dependency(user: CurrentUser) -> User:
        catalogs = get_catalogs()
        granted = [screen for screen in screens if catalogs.grants(user.role.value, (screen,))]
        if not granted:
            raise PermissionDeniedError()
        if all(screen in IDENTITY_SCREENS for screen in granted):
            # Solo sirve a pantallas que exigen la identidad aprobada: 409 si aún no elige empresa,
            # 403 FACE_NOT_APPROVED con el motivo si su registro facial no está aprobado.
            company_of(user)
            approved_employee(user)
        return user

    return dependency


#: `?deleted=true`: la papelera («Eliminados») de un listado (borrado lógico, `app/core/soft_delete.py`).
Trash = Annotated[bool, Query(description="Solo los que están en «Eliminados» (el eliminado más reciente primero)")]


def trash_of(*screens: Screen) -> Callable[..., bool]:
    """La papelera de un listado que también usan otras pantallas (p. ej. elegir empleados): verla exige la pantalla
    que elimina y restaura (la misma regla que restaurar), no solo la que lista."""

    def dependency(user: CurrentUser, deleted: Trash = False) -> bool:
        if deleted:
            require_screen(*screens)(user)
        return deleted

    return dependency


AdminUser = Annotated[User, Depends(require_roles(UserRole.ADMIN))]
CompanyUser = Annotated[User, Depends(require_roles(UserRole.COMPANY))]
#: Validador de identidad de una empresa (tableta o teléfono).
ValidatorUser = Annotated[User, Depends(require_roles(UserRole.VALIDATOR))]
#: Empleado aunque todavía no elija empresa (p. ej. para elegirla).
EmployeeAccount = Annotated[User, Depends(require_roles(UserRole.EMPLOYEE))]
#: Cualquier persona con cuenta (los cuatro roles): lo que es de la persona y no de una empresa, p. ej. su foto de
#: perfil. Va junto con su pantalla (`require_screen`): quitarle la pantalla a un rol le quita también la API.
PersonUser = Annotated[
    User, Depends(require_roles(UserRole.ADMIN, UserRole.COMPANY, UserRole.VALIDATOR, UserRole.EMPLOYEE))
]


def company_of(user: User) -> int:
    """Empresa en la que opera el usuario: el alcance de TODOS sus datos (nunca viene del cliente)."""
    company = user.current_company
    if company is not None:
        return company.id
    if user.role == UserRole.EMPLOYEE:
        raise ConflictError(code="COMPANY_SELECTION_REQUIRED")
    raise PermissionDeniedError(code="COMPANY_REQUIRED")


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


def require_api_module(user: CompanyUser) -> User:
    """La empresa debe tener el módulo de Integraciones (API): lo decide el ADMIN de la plataforma."""
    if not (user.company and user.company.api_enabled):
        raise PermissionDeniedError(code="API_ACCESS_DISABLED")
    return user


def require_validators_module(user: CompanyUser) -> Company:
    """La empresa debe tener el módulo de validadores (un límite mayor que cero, lo fija el ADMIN de la plataforma);
    sin él, 403 VALIDATORS_DISABLED. Devuelve la empresa (su límite, para el "N de M" del listado)."""
    return validators_module(user)


#: Empresa del COMPANY con el módulo de validadores.
ValidatorsModule = Annotated[Company, Depends(require_validators_module)]


#: Empresa del usuario autenticado, sea COMPANY, VALIDATOR o EMPLOYEE.
MemberCompany = Annotated[int, Depends(_member_company)]


def get_pipeline(db: DbSession) -> Generator[FacePipeline]:
    """Reserva un worker facial durante la petición (1 por núcleo, cola FIFO acotada).

    - Cola llena  → 503 inmediato (backpressure): nunca se acumulan peticiones sin límite.
    - Espera larga → 503 reintentable.
    - Modelos caídos → 503 y el resto de la API sigue operando (circuit breaker).

    Antes de esperar turno se cierra la transacción de lo leído hasta aquí (usuario, validador...):
    la conexión vuelve al pool y la fila facial nunca deja sin conexiones a login, QR o administración.
    """
    db.commit()
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


def qr_rate_limit(user: CurrentUser) -> None:
    """QR dinámico: rotación automática + "Generar otro" (un tope por persona y minuto)."""
    enforce(f"qr:user:{user.id}", settings.RATE_LIMIT_QR_PER_MINUTE)


def avatar_rate_limit(user: CurrentUser) -> None:
    """Foto de perfil: subir, cambiar o quitar (procesar una imagen cuesta CPU y dos subidas al bucket)."""
    enforce(f"avatar:user:{user.id}", settings.RATE_LIMIT_AVATAR_PER_MINUTE)


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
        raise _image_error("INVALID_IMAGE_FORMAT")
    max_bytes = settings.max_image_bytes
    data = file.file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise PayloadTooLargeError(key="IMAGE_FILE_TOO_LARGE", params={"size": Megabytes(max_bytes)})
    if not data:
        raise _image_error("EMPTY_IMAGE")
    return data


def _image_error(code: str) -> UnprocessableError:
    """Un archivo de imagen que no sirve: el mensaje es el del error de captura del catálogo (`face_errors`)."""
    return UnprocessableError(get_catalogs().face_error_message(code), code=code)


def read_image_uploads(files: list[UploadFile], *, max_files: int) -> list[bytes]:
    if not files:
        raise _image_error("EMPTY_IMAGE")
    if len(files) > max_files:
        raise UnprocessableError(code="TOO_MANY_IMAGES", params={"count": max_files})
    return [read_image_upload(f) for f in files]


def liveness_response(
    challenge_id: Annotated[str | None, Form(max_length=100, description="Reto de `/api/face/challenge`")] = None,
    challenge_image: Annotated[
        list[UploadFile] | None,
        File(description="Una captura por cada movimiento del reto, en orden (`challenge_image` repetido)"),
    ] = None,
    flash_image: Annotated[
        list[UploadFile] | None,
        File(description="Una captura por cada color del destello, en orden (`flash_image` repetido)"),
    ] = None,
    burst: Annotated[
        UploadFile | None, File(description="Hoja JPEG con la ráfaga de recortes del rostro (`burst` del reto)")
    ] = None,
    burst_meta: Annotated[str | None, Form(description="Descripción de la hoja (JSON: v, tile, cols, t, s)")] = None,
    flash_receipt: Annotated[
        str | None, Form(description="Comprobante del destello dictado por el servidor (canal en vivo)")
    ] = None,
) -> LivenessResponse:
    """La respuesta al reto de prueba de vida que viene en el formulario (vacía si no se envió). La ráfaga y el
    comprobante del destello (antifraude 2a) se leen acotados y SIN validar aquí: lo que no cumpla es una señal del
    motor de riesgo, nunca un 413 ni un 422 (`capture_protocol`)."""
    sheet, oversize = _bounded_read(burst, max_burst_bytes()) if burst is not None else (None, False)
    limit = settings.WS_MAX_MESSAGE_BYTES
    return LivenessResponse(
        challenge_id=challenge_id,
        steps=tuple(read_image_uploads(challenge_image, max_files=MAX_STEPS)) if challenge_image else (),
        flash=tuple(read_image_uploads(flash_image, max_files=MAX_FLASH_COLORS)) if flash_image else (),
        burst=sheet,
        burst_meta=burst_meta,
        burst_oversize=oversize,
        # Un comprobante más largo de lo que cabe en un mensaje del canal no lo emitió el servidor: es uno alterado.
        flash_receipt=flash_receipt if flash_receipt is None or len(flash_receipt) <= limit else "-",
    )


def _bounded_read(upload: UploadFile, limit: int) -> tuple[bytes | None, bool]:
    """Lee hasta `limit` bytes; (None, True) si el archivo es más grande (no se sigue leyendo)."""
    data = upload.file.read(limit + 1)
    return (None, True) if len(data) > limit else (data or None, False)


#: Respuesta al reto: `challenge_id`, `challenge_image` (movimientos) y `flash_image` (destello).
Liveness = Annotated[LivenessResponse, Depends(liveness_response)]
#: Colores que puede tener un destello (el máximo de FACE_FLASH_COLORS).
MAX_FLASH_COLORS = 6


def face_check_rate_limit(user: CurrentUser) -> None:
    enforce(f"face-check:user:{user.id}", settings.RATE_LIMIT_FACE_CHECK_PER_MINUTE)


def request_meta(request: Request) -> tuple[str | None, str | None]:
    ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")
    return ip, (user_agent[:255] if user_agent else None)


# ---------------- Lo que informa la app con las capturas (antifraude 1b) ----------------

#: Telemetría de la toma (JSON, `app/schemas/capture.py`): grande, mal formada o con campos de más cuenta como ausente
#: (señal TELEMETRY_MISSING); nunca es un rechazo.
TelemetryField = Annotated[str | None, Form(description="Telemetría de la toma (JSON, versión 1)")]


def operator_client(request: Request, telemetry: TelemetryField = None) -> ClientEvidence:
    """Lo que informa la app de un validador o de la empresa en persona (sin la prueba del dispositivo: es de la
    empresa, no del empleado)."""
    ip, user_agent = request_meta(request)
    return ClientEvidence(ip=ip, user_agent=user_agent, telemetry=parse_telemetry(telemetry))


def employee_client(
    request: Request,
    telemetry: TelemetryField = None,
    device_key: Annotated[str | None, Form(description="Llave pública del dispositivo (SPKI DER, base64)")] = None,
    device_nonce: Annotated[str | None, Form(description="`device_nonce` del reto de `/api/face/challenge`")] = None,
    device_signature: Annotated[str | None, Form(description="Firma ECDSA P-256 del reto (r||s, base64)")] = None,
) -> ClientEvidence:
    """Lo que informa la app del empleado: la telemetría y la prueba de su dispositivo (decisión D2). Una prueba que
    falta o no verifica es una señal (DEVICE_KEY_MISSING), nunca un 422."""
    ip, user_agent = request_meta(request)
    proof = DeviceProofInput(public_key=device_key, nonce=device_nonce, signature=device_signature)
    return ClientEvidence(ip=ip, user_agent=user_agent, telemetry=parse_telemetry(telemetry), device=proof)


#: Validador o empresa en persona / el propio empleado.
OperatorClient = Annotated[ClientEvidence, Depends(operator_client)]
EmployeeClient = Annotated[ClientEvidence, Depends(employee_client)]
_SAMPLES = TypeAdapter(list[LocationSample])


def location_samples(
    location_samples: Annotated[
        str | None, Form(description="Lecturas de la ubicación de la toma (JSON: latitude, longitude, accuracy)")
    ] = None,
) -> tuple[LocationSample, ...]:
    """Las lecturas de la ubicación que tomó la app en su ventana corta (señales del lugar). Mal formadas o más de
    `LOCATION_MAX_SAMPLES`: 422 `LOCATION_SAMPLES_INVALID` (son parte del contrato del registro)."""
    if not location_samples:
        return ()
    try:
        samples = _SAMPLES.validate_json(location_samples)
    except ValidationError:
        samples = None
    if samples is None or len(samples) > settings.LOCATION_MAX_SAMPLES:
        raise UnprocessableError(
            code="LOCATION_SAMPLES_INVALID", field="location_samples", params={"max": settings.LOCATION_MAX_SAMPLES}
        )
    return tuple(samples)


LocationSamples = Annotated[tuple[LocationSample, ...], Depends(location_samples)]


def validator_request(
    samples: LocationSamples,
    signature_key: Annotated[str | None, Form(description="Llave pública del dispositivo (SPKI DER, base64)")] = None,
    signature_nonce: Annotated[str | None, Form(description="El `device_nonce` más reciente del servidor")] = None,
    signature: Annotated[
        str | None, Form(description="Firma ECDSA P-256 de `{nonce}.face.{SHA-256 de la primera captura}` (r||s)")
    ] = None,
    latitude: Annotated[float | None, Form(ge=-90, le=90)] = None,
    longitude: Annotated[float | None, Form(ge=-180, le=180)] = None,
    accuracy: Annotated[float | None, Form(ge=0, le=100_000, description="Precisión (m) del navegador")] = None,
) -> IdentificationRequest:
    """La firma por petición y la ubicación de una identificación facial del validador (antifraude 2b): la firma que
    falta o no verifica es una señal o, si la empresa la exige, un 403; nunca un 422."""
    location = (
        DeviceLocation(latitude=latitude, longitude=longitude, accuracy=accuracy)
        if latitude is not None and longitude is not None
        else None
    )
    return IdentificationRequest(RequestProof(signature_key, signature_nonce, signature), location, samples)


#: La firma y la ubicación de una identificación facial en un validador (multipart).
ValidatorRequest = Annotated[IdentificationRequest, Depends(validator_request)]


# ---------------- API de integración (llave de la empresa) ----------------

#: Solo la API de integración la lee; el resto de la API no la acepta (y aquí no sirve una sesión).
_api_key_header = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
    scheme_name="ApiKey",
    description="Llave de la API de integración de tu empresa (Integraciones › Crear llave)",
)


def get_api_client(
    request: Request, db: DbSession, api_key: Annotated[str | None, Security(_api_key_header)]
) -> ApiClient:
    """La llave de la cabecera: a qué empresa (y con qué permisos) da acceso esta petición. El consumo
    de la petición se le cuenta a esa empresa."""
    ip, _ = request_meta(request)
    # La llave se busca por su hash antes de saber de qué empresa es (plataforma); todo lo demás de la petición
    # es solo de la empresa de la llave (seguridad por fila).
    use_platform(db)
    client = authenticate(db, api_key, ip)
    use_company(db, client.company_id)
    note_company(client.company_id)
    return client


ApiClientDep = Annotated[ApiClient, Depends(get_api_client)]


def require_api_scope(scope: ApiScope) -> Callable[[ApiClient], ApiClient]:
    """La llave debe tener este permiso (catalog.api_scopes); si no, 403 API_SCOPE_REQUIRED."""

    def dependency(client: ApiClientDep) -> ApiClient:
        require_scope(client, scope.value)
        return client

    return dependency


def require_validators_api(
    client: Annotated[ApiClient, Depends(require_api_scope(ApiScope.VALIDATORS_READ))],
) -> ApiClient:
    """Los validadores por la API de integración: el permiso de lectura y, como en la app, el módulo de validadores
    de la empresa de la llave (un límite mayor que cero); sin él, 403 VALIDATORS_DISABLED."""
    if client.max_validators <= 0:
        raise PermissionDeniedError(code="VALIDATORS_DISABLED", key="API_KEY_VALIDATORS_DISABLED")
    return client
