"""Autenticación: login, renovación, cierre de sesión, sesiones activas y JWKS.

- Access token: JWT ES256 de 12 h en `data.access_token` → cabecera `Authorization: Bearer`.
- Refresh token: opaco, en cookie `HttpOnly; SameSite=Strict; Path=/api/auth` (inaccesible para
  JavaScript, no viaja a otros endpoints). Rota en cada uso con detección de reutilización.
"""

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Cookie, Depends, Request, Response
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.opaque_tokens import split_token
from app.core.responses import ApiResponse, ok
from app.core.tokens import jwks
from app.dependencies import (
    CurrentUser,
    DbSession,
    EmployeeAccount,
    OptionalTokenPayload,
    Pagination,
    PlatformDb,
    request_meta,
    require_screen,
)
from app.i18n import Text
from app.middleware.rate_limit import enforce, ip_rate_limit
from app.models import Screen, SessionRevocationReason, User
from app.schemas.auth import (
    ChangePasswordRequest,
    CompanySelection,
    JwksResponse,
    LoginRequest,
    RememberedAccountRead,
    SessionList,
    SessionRead,
    SessionStatus,
    TokenResponse,
)
from app.schemas.common import ErrorResponse
from app.schemas.passkey import (
    ChallengeOptions,
    PasskeyList,
    PasskeyLogin,
    PasskeyRead,
    PasskeyRegistration,
    PasskeyRename,
)
from app.schemas.user import UserRead
from app.services.auth_service import AuthService, default_company_id, ensure_account_usable
from app.services.device_service import ensure_device_authorized
from app.services.location_service import ensure_location_allowed
from app.services.navigation_service import user_read
from app.services.passkey_service import PasskeyService
from app.services.policy_service import ensure_device_allowed
from app.services.remembered_account_service import RememberedAccountService
from app.services.session_service import IssuedSession, SessionService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Autenticación"])

COOKIE_PATH = f"{settings.API_PREFIX}/auth"
RefreshCookie = Annotated[str | None, Cookie(alias=settings.REFRESH_COOKIE_NAME, include_in_schema=False)]
RememberCookie = Annotated[str | None, Cookie(alias=settings.REMEMBER_COOKIE_NAME, include_in_schema=False)]


def _secure(request: Request) -> bool:
    if settings.COOKIE_SECURE == "auto":
        return request.url.scheme == "https"
    return settings.COOKIE_SECURE == "true"


def _set_cookie(response: Response, request: Request, name: str, value: str, max_age: int | None) -> None:
    """Cookie HttpOnly, SameSite=Strict y solo para /api/auth (JavaScript no puede leerla)."""
    response.set_cookie(
        name, value, max_age=max_age, path=COOKIE_PATH, httponly=True, secure=_secure(request), samesite="strict"
    )


def _clear_cookie(response: Response, request: Request, name: str) -> None:
    response.delete_cookie(name, path=COOKIE_PATH, httponly=True, secure=_secure(request), samesite="strict")


def _set_refresh_cookie(response: Response, request: Request, issued: IssuedSession, value: str) -> None:
    # "Recordar mi cuenta": persiste lo que le queda a la sesión. Si no, cookie de sesión del
    # navegador: al cerrarlo hay que volver a iniciar sesión.
    max_age = issued.access.expires_in if issued.session.persistent else None
    _set_cookie(response, request, settings.REFRESH_COOKIE_NAME, value, max_age)


def _token_response(issued: IssuedSession) -> TokenResponse:
    issued.session.user.use_company(issued.session.company_id)
    return TokenResponse(
        access_token=issued.access.token,
        expires_in=issued.access.expires_in,
        expires_at=issued.access.expires_at,
        session_id=issued.session.id,
        user=user_read(issued.session.user),
    )


@router.post(
    "/login",
    response_model=ApiResponse[TokenResponse],
    summary="Iniciar sesión con correo y contraseña",
    description=(
        "Devuelve el access token (JWT ES256, 12 h) en `data.access_token` y fija la cookie "
        "HttpOnly del refresh token. En Swagger: copia `data.access_token` y pulsa **Authorize**."
    ),
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
    dependencies=[Depends(ip_rate_limit("login", lambda: settings.RATE_LIMIT_LOGIN_IP_PER_MINUTE))],
)
def login(
    payload: LoginRequest, request: Request, response: Response, db: PlatformDb, remembered: RememberCookie = None
) -> ApiResponse[TokenResponse]:
    # Límite adicional por cuenta para frenar fuerza bruta sobre un mismo correo.
    enforce(f"login:email:{payload.email}", settings.RATE_LIMIT_LOGIN_PER_MINUTE)
    user = AuthService(db).authenticate(payload.email, payload.password)
    return _start_session(user, payload, request, response, db, remembered)


def _start_session(
    user: User,
    payload: LoginRequest | PasskeyLogin,
    request: Request,
    response: Response,
    db: Session,
    remembered: str | None,
) -> ApiResponse[TokenResponse]:
    """Lo que sigue a autenticar a la persona (con su contraseña o con una llave de acceso), igual para los dos: las
    reglas del dispositivo y la ubicación de un validador, UNA sesión y recordar la cuenta en el dispositivo."""
    # Con un solo empleo entra directo a su empresa; con varios, la elige después (POST /auth/company).
    user.use_company(default_company_id(user))
    # Antes de crear la sesión: desde un dispositivo no permitido no se emite ningún token.
    ensure_device_allowed(db, user, request.headers)
    ip, user_agent = request_meta(request)
    # Validador: solo desde un dispositivo que su empresa autorizó (firma el reto con su llave).
    # La sesión queda ligada a esa llave: cada identificación la vuelve a firmar (antifraude 2b, `request_signing`).
    device_key = ensure_device_authorized(db, user, payload.device, ip=ip, user_agent=user_agent)
    # Validador que requiere ubicación: solo dentro del radio de su domicilio.
    ensure_location_allowed(user, payload.location)
    issued = SessionService(db).create(
        user, ip=ip, user_agent=user_agent, persistent=payload.remember, device_key_hash=device_key
    )
    _set_refresh_cookie(response, request, issued, issued.refresh_token or "")
    try:  # recordar la cuenta es un extra: si falla, la sesión (ya creada) no se pierde
        accounts = RememberedAccountService(db)
        if payload.remember:
            days = settings.REMEMBER_ACCOUNT_DAYS
            token = accounts.remember(user, remembered)
            _set_cookie(response, request, settings.REMEMBER_COOKIE_NAME, token, days * 86400)
        elif remembered:
            accounts.forget(remembered)
            _clear_cookie(response, request, settings.REMEMBER_COOKIE_NAME)
    except SQLAlchemyError:
        db.rollback()
        logger.warning("No se pudo recordar la cuenta en este dispositivo; la sesión sí se inició", exc_info=True)
    return ok(_token_response(issued), code="LOGIN_SUCCESS")


# ---------------------------------------------------------------- llaves de acceso (WebAuthn / passkeys)

PASSKEY_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse},
    404: {"model": ErrorResponse, "description": "La llave no es de esta cuenta"},
    409: {"model": ErrorResponse, "description": "Reto ya usado, llave ya registrada o tope de llaves"},
    422: {"model": ErrorResponse, "description": "Reto vencido o credencial que no verifica"},
}


@router.post(
    "/login/passkey/options",
    response_model=ApiResponse[ChallengeOptions],
    summary="Entrar con una llave de acceso: el reto (sellado, de un solo uso) y las opciones del navegador",
    description=(
        "Sin sesión. Devuelve `options` para `navigator.credentials.get` (sin lista de llaves: la persona elige en su "
        "dispositivo, con verificación obligatoria) y `token`, el reto sellado que debe volver con la firma."
    ),
    responses={429: {"model": ErrorResponse}},
    dependencies=[Depends(ip_rate_limit("login", lambda: settings.RATE_LIMIT_LOGIN_IP_PER_MINUTE))],
)
def passkey_login_options(db: PlatformDb) -> ApiResponse[ChallengeOptions]:
    return ok(PasskeyService(db).login_options(), code="PASSKEY_LOGIN_OPTIONS")


@router.post(
    "/login/passkey",
    response_model=ApiResponse[TokenResponse],
    summary="Iniciar sesión con una llave de acceso (WebAuthn)",
    description=(
        "La misma sesión que `/auth/login` y las mismas reglas (empresa suspendida, estado de la cuenta, dispositivo y "
        "ubicación de un validador, `remember`). Una firma que no verifica, un reto vencido o ya usado: 401 "
        "`PASSKEY_LOGIN_FAILED`. Una llave copiada (su contador no avanzó): 401 `PASSKEY_CLONED` y la llave se revoca."
    ),
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
    dependencies=[Depends(ip_rate_limit("login", lambda: settings.RATE_LIMIT_LOGIN_IP_PER_MINUTE))],
)
def passkey_login(
    payload: PasskeyLogin, request: Request, response: Response, db: PlatformDb, remembered: RememberCookie = None
) -> ApiResponse[TokenResponse]:
    # Límite por credencial (como el de la contraseña por correo): frena a quien prueba firmas contra una misma llave.
    enforce(f"login:passkey:{str(payload.credential.get('id', ''))[:200]}", settings.RATE_LIMIT_LOGIN_PER_MINUTE)
    user = PasskeyService(db).authenticate(payload.token, payload.credential)
    ensure_account_usable(user)
    return _start_session(user, payload, request, response, db, remembered)


@router.post(
    "/passkeys/options",
    response_model=ApiResponse[ChallengeOptions],
    summary="Registrar una llave de acceso: el reto (sellado, de un solo uso) y las opciones del navegador",
    responses=PASSKEY_ERRORS,
)
def passkey_registration_options(user: CurrentUser, db: DbSession) -> ApiResponse[ChallengeOptions]:
    return ok(PasskeyService(db).registration_options(user), code="PASSKEY_OPTIONS")


@router.post(
    "/passkeys",
    response_model=ApiResponse[PasskeyRead],
    status_code=201,
    summary="Registrar una llave de acceso en esta cuenta",
    responses=PASSKEY_ERRORS,
)
def register_passkey(payload: PasskeyRegistration, user: CurrentUser, db: DbSession) -> ApiResponse[PasskeyRead]:
    passkey = PasskeyService(db).register(user, payload.token, payload.name, payload.credential)
    return ok(PasskeyRead.model_validate(passkey), code="PASSKEY_REGISTERED", status_code=201)


@router.get("/passkeys", response_model=ApiResponse[PasskeyList], summary="Mis llaves de acceso (paginado)")
def list_passkeys(user: CurrentUser, db: DbSession, page: Pagination) -> ApiResponse[PasskeyList]:
    passkeys, total = PasskeyService(db).page(user, page)
    items = [PasskeyRead.model_validate(p) for p in passkeys]
    return ok(PasskeyList.of(items, total, page), code="PASSKEYS_LISTED", params={"count": total})


@router.patch(
    "/passkeys/{passkey_id}",
    response_model=ApiResponse[PasskeyRead],
    summary="Renombrar una de mis llaves de acceso",
    responses=PASSKEY_ERRORS,
)
def rename_passkey(
    passkey_id: int, payload: PasskeyRename, user: CurrentUser, db: DbSession
) -> ApiResponse[PasskeyRead]:
    passkey = PasskeyService(db).rename(user, passkey_id, payload.name)
    return ok(PasskeyRead.model_validate(passkey), code="PASSKEY_RENAMED")


@router.delete(
    "/passkeys/{passkey_id}",
    response_model=ApiResponse[None],
    summary="Revocar una de mis llaves de acceso (deja de servir al instante)",
    responses=PASSKEY_ERRORS,
)
def revoke_passkey(passkey_id: int, user: CurrentUser, db: DbSession) -> ApiResponse[None]:
    PasskeyService(db).revoke(user, passkey_id)
    return ok(None, code="PASSKEY_REVOKED")


@router.get(
    "/session",
    response_model=ApiResponse[SessionStatus],
    summary="¿Hay una sesión vigente? (sin renovarla)",
    description=(
        "Lo consulta la aplicación al cargar para decidir si restaura la sesión con `/auth/refresh`. "
        "Lee la cookie HttpOnly sin rotarla ni cerrarla; sin sesión responde `signed_in: false` (no es "
        "un error). El navegador no guarda nada propio para saberlo."
    ),
    responses={429: {"model": ErrorResponse}},
    dependencies=[Depends(ip_rate_limit("session", lambda: settings.RATE_LIMIT_LOGIN_IP_PER_MINUTE))],
)
def session_status(db: PlatformDb, token: RefreshCookie = None) -> ApiResponse[SessionStatus]:
    signed_in = SessionService(db).has_session(token)
    key = "SESSION_ACTIVE" if signed_in else "SESSION_NONE"
    return ok(SessionStatus(signed_in=signed_in), code="SESSION_STATUS", key=key)


@router.post(
    "/refresh",
    response_model=ApiResponse[TokenResponse],
    summary="Renovar el access token (rota el refresh token de la cookie)",
    description=(
        "Usa la cookie HttpOnly. Un refresh token ya utilizado (fuera de la ventana de gracia) "
        "revoca la sesión: 401 `REFRESH_TOKEN_REUSED`."
    ),
    responses={401: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
    dependencies=[Depends(ip_rate_limit("refresh", lambda: settings.RATE_LIMIT_LOGIN_IP_PER_MINUTE))],
)
def refresh(
    request: Request, response: Response, db: PlatformDb, token: RefreshCookie = None
) -> ApiResponse[TokenResponse]:
    # Por sesión (la cookie): una oficina entera detrás de una sola IP no se bloquea entre sí.
    sid, _ = split_token(token)
    if sid:
        enforce(f"refresh:sid:{sid}", settings.RATE_LIMIT_REFRESH_PER_MINUTE)
    issued = SessionService(db).refresh(token)
    if issued.refresh_token:
        _set_refresh_cookie(response, request, issued, issued.refresh_token)
    return ok(_token_response(issued), code="TOKEN_REFRESHED")


@router.post(
    "/company",
    response_model=ApiResponse[UserRead],
    summary="EMPLOYEE: entrar a una de sus empresas (si trabaja en varias)",
    description=(
        "Fija en la sesión la empresa en la que opera el empleado. Solo una empresa donde trabaje, "
        "con su empleo y la empresa activos, y desde un dispositivo que esa empresa permita. Se puede "
        "cambiar en cualquier momento sin volver a iniciar sesión."
    ),
    responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    dependencies=[Depends(require_screen(Screen.EMPLOYEE_SELECT_COMPANY))],
)
def select_company(
    payload: CompanySelection, request: Request, user: EmployeeAccount, db: PlatformDb
) -> ApiResponse[UserRead]:
    SessionService(db).select_company(request.state.session_id, user, payload.company_id, request.headers)
    company = user.current_company  # la que acaba de elegir (sin ella, "tu empresa")
    name = company.name if company else Text("YOUR_COMPANY")
    return ok(user_read(user), code="COMPANY_SELECTED", params={"company": name})


@router.get(
    "/remembered",
    response_model=ApiResponse[RememberedAccountRead | None],
    summary='Cuenta recordada en este dispositivo ("Recordar mi cuenta")',
    description=(
        "Lee la cookie HttpOnly del dispositivo y devuelve el correo recordado (vive en la BD), o "
        "`null` si no hay ninguno o ya venció."
    ),
)
def remembered_account(db: PlatformDb, remembered: RememberCookie = None) -> ApiResponse[RememberedAccountRead | None]:
    account = RememberedAccountService(db).lookup(remembered)
    data = RememberedAccountRead(email=account.user.email) if account else None
    return ok(data, code="REMEMBERED_ACCOUNT", key="REMEMBERED_ACCOUNT" if data else "REMEMBERED_ACCOUNT_NONE")


@router.delete(
    "/remembered",
    response_model=ApiResponse[None],
    summary='Olvidar la cuenta recordada en este dispositivo ("Usar otra cuenta")',
)
def forget_remembered_account(
    request: Request, response: Response, db: PlatformDb, remembered: RememberCookie = None
) -> ApiResponse[None]:
    RememberedAccountService(db).forget(remembered)
    _clear_cookie(response, request, settings.REMEMBER_COOKIE_NAME)
    return ok(None, code="REMEMBERED_ACCOUNT_FORGOTTEN")


@router.post(
    "/logout",
    response_model=ApiResponse[None],
    summary="Cerrar la sesión actual (el access token deja de funcionar de inmediato)",
    description=(
        "Revoca la sesión identificada por el access token (Bearer) y/o por la cookie del refresh "
        "token: el JWT activo queda inválido al instante aunque aún no haya expirado. Nunca falla "
        "(responde 200 aunque el token ya no sea válido)."
    ),
)
def logout(
    request: Request,
    response: Response,
    db: PlatformDb,
    payload: OptionalTokenPayload,
    token: RefreshCookie = None,
) -> ApiResponse[None]:
    sessions = SessionService(db)
    if payload is not None:
        sessions.revoke_quietly(str(payload["sid"]), int(payload["sub"]))
    sessions.revoke_by_refresh_token(token)
    _clear_cookie(response, request, settings.REFRESH_COOKIE_NAME)
    return ok(None, code="LOGGED_OUT")


@router.post(
    "/change-password",
    response_model=ApiResponse[dict],
    summary="Cambiar mi contraseña",
    description=(
        "Requiere la contraseña actual. Cierra todas las demás sesiones del usuario (otros "
        "dispositivos); la sesión actual sigue activa."
    ),
    responses={401: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
)
def change_password(
    payload: ChangePasswordRequest, request: Request, user: CurrentUser, db: PlatformDb
) -> ApiResponse[dict]:
    enforce(f"change-password:user:{user.id}", settings.RATE_LIMIT_LOGIN_PER_MINUTE)
    current = getattr(request.state, "session_id", None)
    revoked = SessionService(db).change_password(user, payload.current_password, payload.new_password, keep=current)
    return ok({"revoked_sessions": revoked}, code="PASSWORD_CHANGED", params={"count": revoked})


@router.post(
    "/logout-all",
    response_model=ApiResponse[dict],
    summary="Cerrar sesión en todos los dispositivos",
    responses={401: {"model": ErrorResponse}},
)
def logout_all(request: Request, response: Response, user: CurrentUser, db: PlatformDb) -> ApiResponse[dict]:
    revoked = SessionService(db).revoke_all(user.id, SessionRevocationReason.LOGOUT_ALL)
    _clear_cookie(response, request, settings.REFRESH_COOKIE_NAME)
    return ok({"revoked": revoked}, code="LOGGED_OUT_ALL", params={"count": revoked})


@router.get(
    "/sessions",
    response_model=ApiResponse[SessionList],
    summary="Mis sesiones activas (dispositivos)",
    responses={401: {"model": ErrorResponse}},
)
def list_sessions(request: Request, user: CurrentUser, db: PlatformDb, page: Pagination) -> ApiResponse[SessionList]:
    current = getattr(request.state, "session_id", None)
    sessions, total = SessionService(db).page_active(user.id, page)
    items = [SessionRead.model_validate(s).model_copy(update={"current": s.id == current}) for s in sessions]
    return ok(SessionList.of(items, total, page), code="SESSIONS_LISTED", params={"count": total})


@router.delete(
    "/sessions/{session_id}",
    response_model=ApiResponse[None],
    summary="Revocar una de mis sesiones",
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
def revoke_session(session_id: str, user: CurrentUser, db: PlatformDb) -> ApiResponse[None]:
    SessionService(db).revoke(session_id, user.id, SessionRevocationReason.REVOKED_BY_USER)
    return ok(None, code="SESSION_REVOKED")


@router.get(
    "/jwks",
    response_model=ApiResponse[JwksResponse],
    summary="Llaves públicas para validar los JWT (JWKS)",
    description="Las llaves están en `data.keys`, con el formato RFC 7517. Con HS256 la lista está vacía.",
)
def get_jwks() -> ApiResponse[JwksResponse]:
    return ok(JwksResponse.model_validate(jwks()), code="JWKS")
