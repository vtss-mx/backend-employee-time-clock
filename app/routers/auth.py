"""Autenticación: login, renovación, cierre de sesión, sesiones activas y JWKS.

- Access token: JWT ES256 de 12 h en `data.access_token` → cabecera `Authorization: Bearer`.
- Refresh token: opaco, en cookie `HttpOnly; SameSite=Strict; Path=/api/auth` (inaccesible para
  JavaScript, no viaja a otros endpoints). Rota en cada uso con detección de reutilización.
"""

from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Request, Response

from app.core.config import settings
from app.core.responses import ApiResponse, ok
from app.core.tokens import jwks
from app.dependencies import CurrentUser, DbSession, EmployeeAccount, OptionalTokenPayload, request_meta
from app.middleware.rate_limit import enforce, ip_rate_limit
from app.models import SessionRevocationReason
from app.schemas.auth import (
    ChangePasswordRequest,
    CompanySelection,
    JwksResponse,
    LoginRequest,
    RememberedAccountRead,
    SessionRead,
    TokenResponse,
)
from app.schemas.common import ErrorResponse
from app.schemas.user import UserRead
from app.services.auth_service import AuthService, default_company_id
from app.services.navigation_service import user_read
from app.services.policy_service import ensure_device_allowed
from app.services.remembered_account_service import RememberedAccountService
from app.services.session_service import IssuedSession, SessionService

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
    payload: LoginRequest, request: Request, response: Response, db: DbSession, remembered: RememberCookie = None
) -> ApiResponse[TokenResponse]:
    # Límite adicional por cuenta para frenar fuerza bruta sobre un mismo correo.
    enforce(f"login:email:{payload.email}", settings.RATE_LIMIT_LOGIN_PER_MINUTE)
    user = AuthService(db).authenticate(payload.email, payload.password)
    # Con un solo empleo entra directo a su empresa; con varios, la elige después (POST /auth/company).
    user.use_company(default_company_id(user))
    # Antes de crear la sesión: desde un dispositivo no permitido no se emite ningún token.
    ensure_device_allowed(db, user, request.headers)
    ip, user_agent = request_meta(request)
    issued = SessionService(db).create(user, ip=ip, user_agent=user_agent, persistent=payload.remember)
    _set_refresh_cookie(response, request, issued, issued.refresh_token or "")
    accounts = RememberedAccountService(db)
    if payload.remember:
        days = settings.REMEMBER_ACCOUNT_DAYS
        _set_cookie(response, request, settings.REMEMBER_COOKIE_NAME, accounts.remember(user, remembered), days * 86400)
    elif remembered:
        accounts.forget(remembered)
        _clear_cookie(response, request, settings.REMEMBER_COOKIE_NAME)
    return ok(_token_response(issued), "Sesión iniciada correctamente", code="LOGIN_SUCCESS")


@router.post(
    "/refresh",
    response_model=ApiResponse[TokenResponse],
    summary="Renovar el access token (rota el refresh token de la cookie)",
    description=(
        "Usa la cookie HttpOnly. Un refresh token ya utilizado (fuera de la ventana de gracia) "
        "revoca la sesión: 401 `REFRESH_TOKEN_REUSED`."
    ),
    responses={401: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
    dependencies=[Depends(ip_rate_limit("refresh", lambda: settings.RATE_LIMIT_REFRESH_PER_MINUTE))],
)
def refresh(
    request: Request, response: Response, db: DbSession, token: RefreshCookie = None
) -> ApiResponse[TokenResponse]:
    issued = SessionService(db).refresh(token)
    if issued.refresh_token:
        _set_refresh_cookie(response, request, issued, issued.refresh_token)
    return ok(_token_response(issued), "Sesión renovada", code="TOKEN_REFRESHED")


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
)
def select_company(
    payload: CompanySelection, request: Request, user: EmployeeAccount, db: DbSession
) -> ApiResponse[UserRead]:
    SessionService(db).select_company(request.state.session_id, user, payload.company_id, request.headers)
    company = user.current_company
    return ok(
        user_read(user),
        f"Entraste a {company.name if company else 'tu empresa'}",
        code="COMPANY_SELECTED",
    )


@router.get(
    "/remembered",
    response_model=ApiResponse[RememberedAccountRead | None],
    summary='Cuenta recordada en este dispositivo ("Recordar mi cuenta")',
    description=(
        "Lee la cookie HttpOnly del dispositivo y devuelve el correo recordado (vive en la BD), o "
        "`null` si no hay ninguno o ya venció."
    ),
)
def remembered_account(db: DbSession, remembered: RememberCookie = None) -> ApiResponse[RememberedAccountRead | None]:
    account = RememberedAccountService(db).lookup(remembered)
    data = RememberedAccountRead(email=account.user.email) if account else None
    return ok(data, "Cuenta recordada" if data else "Sin cuenta recordada", code="REMEMBERED_ACCOUNT")


@router.delete(
    "/remembered",
    response_model=ApiResponse[None],
    summary='Olvidar la cuenta recordada en este dispositivo ("Usar otra cuenta")',
)
def forget_remembered_account(
    request: Request, response: Response, db: DbSession, remembered: RememberCookie = None
) -> ApiResponse[None]:
    RememberedAccountService(db).forget(remembered)
    _clear_cookie(response, request, settings.REMEMBER_COOKIE_NAME)
    return ok(None, "Este dispositivo ya no recuerda la cuenta", code="REMEMBERED_ACCOUNT_FORGOTTEN")


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
    db: DbSession,
    payload: OptionalTokenPayload,
    token: RefreshCookie = None,
) -> ApiResponse[None]:
    sessions = SessionService(db)
    if payload is not None:
        sessions.revoke_quietly(str(payload["sid"]), int(payload["sub"]))
    sessions.revoke_by_refresh_token(token)
    _clear_cookie(response, request, settings.REFRESH_COOKIE_NAME)
    return ok(None, "Sesión cerrada", code="LOGGED_OUT")


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
    payload: ChangePasswordRequest, request: Request, user: CurrentUser, db: DbSession
) -> ApiResponse[dict]:
    enforce(f"change-password:user:{user.id}", settings.RATE_LIMIT_LOGIN_PER_MINUTE)
    AuthService(db).change_password(user, payload.current_password, payload.new_password)
    current = getattr(request.state, "session_id", None)
    revoked = SessionService(db).revoke_all(user.id, SessionRevocationReason.PASSWORD_CHANGED, except_id=current)
    message = "Contraseña actualizada." + (
        f" Se cerraron {revoked} sesión(es) en otros dispositivos." if revoked else ""
    )
    return ok({"revoked_sessions": revoked}, message, code="PASSWORD_CHANGED")


@router.post(
    "/logout-all",
    response_model=ApiResponse[dict],
    summary="Cerrar sesión en todos los dispositivos",
    responses={401: {"model": ErrorResponse}},
)
def logout_all(request: Request, response: Response, user: CurrentUser, db: DbSession) -> ApiResponse[dict]:
    revoked = SessionService(db).revoke_all(user.id, SessionRevocationReason.LOGOUT_ALL)
    _clear_cookie(response, request, settings.REFRESH_COOKIE_NAME)
    return ok({"revoked": revoked}, f"Se cerraron {revoked} sesión(es)", code="LOGGED_OUT_ALL")


@router.get(
    "/sessions",
    response_model=ApiResponse[list[SessionRead]],
    summary="Mis sesiones activas (dispositivos)",
    responses={401: {"model": ErrorResponse}},
)
def list_sessions(request: Request, user: CurrentUser, db: DbSession) -> ApiResponse[list[SessionRead]]:
    current = getattr(request.state, "session_id", None)
    items = [
        SessionRead.model_validate(s).model_copy(update={"current": s.id == current})
        for s in SessionService(db).list_active(user.id)
    ]
    return ok(items, f"{len(items)} sesión(es) activa(s)", code="SESSIONS_LISTED")


@router.delete(
    "/sessions/{session_id}",
    response_model=ApiResponse[None],
    summary="Revocar una de mis sesiones",
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
def revoke_session(session_id: str, user: CurrentUser, db: DbSession) -> ApiResponse[None]:
    SessionService(db).revoke(session_id, user.id, SessionRevocationReason.REVOKED_BY_USER)
    return ok(None, "Sesión revocada", code="SESSION_REVOKED")


@router.get(
    "/jwks",
    response_model=ApiResponse[JwksResponse],
    summary="Llaves públicas para validar los JWT (JWKS)",
    description="Las llaves están en `data.keys`, con el formato RFC 7517. Con HS256 la lista está vacía.",
)
def get_jwks() -> ApiResponse[JwksResponse]:
    return ok(JwksResponse.model_validate(jwks()), "Llaves públicas de firma", code="JWKS")
