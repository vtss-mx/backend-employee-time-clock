from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.exceptions import AuthenticationError, PermissionDeniedError, UnprocessableError
from app.core.passwords import hash_password, password_needs_rehash, verify_password
from app.models import Company, User, UserRole
from app.repositories.user_repository import UserRepository


def company_suspended(key: str = "COMPANY_SUSPENDED") -> PermissionDeniedError:
    """403 COMPANY_SUSPENDED: la empresa está suspendida (falta de pago o decisión del ADMIN). Es un 403 y
    no un 401: la sesión no es el problema; la app muestra la pantalla de empresa suspendida. `key` elige el mensaje
    (el de la API de integración habla de "la empresa de esta llave")."""
    return PermissionDeniedError(code="COMPANY_SUSPENDED", key=key)


def default_company_id(user: User) -> int | None:
    """Empresa con la que entra un empleado sin elegir: solo si tiene un único empleo utilizable."""
    usable = user.usable_employees if user.role == UserRole.EMPLOYEE else []
    return usable[0].company_id if len(usable) == 1 else None


def ensure_account_usable(user: User) -> None:
    """Cuenta activa y con dónde operar (al iniciar sesión, al renovar y en cada petición).

    - COMPANY: su empresa debe estar activa.
    - EMPLOYEE: el empleo de la empresa elegida en la sesión debe seguir activo y su empresa
      también; si aún no elige, debe tener al menos un empleo utilizable.
    - ADMIN no pertenece a una empresa.
    """
    if not user.active or user.deleted:  # una cuenta en «Eliminados» ya no entra (sus sesiones se cerraron)
        raise AuthenticationError(code="USER_INACTIVE")
    if user.role in (UserRole.COMPANY, UserRole.VALIDATOR):
        _ensure_company_usable(user.company)
    elif user.role == UserRole.EMPLOYEE:
        _ensure_employment_usable(user)


def _ensure_company_usable(company: Company | None) -> None:
    """La empresa donde opera debe estar activa (401: la sesión ya no sirve) y no suspendida (403)."""
    if company is None or not company.active or company.deleted:
        raise AuthenticationError(code="COMPANY_INACTIVE")
    if company.suspended:
        raise company_suspended()


def _ensure_employment_usable(user: User) -> None:
    """Empleado: el empleo de la empresa elegida (y esa empresa) siguen utilizables; si aún no elige,
    debe tener alguno. Una empresa suspendida no bloquea los empleos en otras empresas."""
    if user.session_company_id is not None:
        employee = user.employee
        if employee is None or not employee.active:
            raise AuthenticationError(code="USER_INACTIVE", key="NO_LONGER_IN_COMPANY")
        _ensure_company_usable(employee.company)
    elif not user.usable_employees:
        active = [e for e in user.employees if e.active]
        if any(e.company.active and e.company.suspended for e in active):
            raise company_suspended()
        raise AuthenticationError(code="COMPANY_INACTIVE" if active else "USER_INACTIVE")


class AuthService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.users = UserRepository(db)

    def authenticate(self, email: str, password: str) -> User:
        user = self.users.get_by_email(email)
        password_hash = user.password_hash if user else None
        # Termina la lectura antes de Argon2: la conexión vuelve al pool mientras se calcula el hash
        # (decenas de ms y, en una ráfaga de inicios de sesión, la espera por un turno). Los datos
        # leídos siguen disponibles (expire_on_commit=False); la escritura abre otra transacción.
        self.db.commit()
        # verify_password se ejecuta siempre (hash ficticio si no existe) para no filtrar
        # por tiempo de respuesta qué correos están registrados.
        valid = verify_password(password, password_hash)
        if not user or not valid:
            raise AuthenticationError(code="INVALID_CREDENTIALS")
        user.use_company(None)  # aún no elige empresa (si trabaja en varias)
        ensure_account_usable(user)

        if password_needs_rehash(user.password_hash):
            user.password_hash = hash_password(password)
        user.last_login_at = datetime.now(UTC)
        self.db.commit()
        self.db.refresh(user)
        return user

    def change_password(self, user: User, current_password: str, new_password: str) -> None:
        if not verify_password(current_password, user.password_hash):
            raise UnprocessableError(
                code="CURRENT_PASSWORD_INVALID",
                details={"field": "current_password"},
            )
        if current_password == new_password:
            raise UnprocessableError(code="PASSWORD_REUSED")
        user.password_hash = hash_password(new_password)
