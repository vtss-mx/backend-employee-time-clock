from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.exceptions import AuthenticationError, UnprocessableError
from app.core.passwords import hash_password, password_needs_rehash, verify_password
from app.models import User, UserRole
from app.repositories.user_repository import UserRepository

INVALID_CREDENTIALS = "Correo o contraseña incorrectos"


COMPANY_INACTIVE = "Tu empresa está desactivada en la plataforma. Contacta al administrador."


ACCOUNT_INACTIVE = "La cuenta está desactivada"
NO_LONGER_IN_COMPANY = "Ya no tienes acceso a esta empresa. Inicia sesión de nuevo."


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
    if not user.active:
        raise AuthenticationError(ACCOUNT_INACTIVE, code="USER_INACTIVE")
    if user.role in (UserRole.COMPANY, UserRole.VALIDATOR):
        if user.company is None or not user.company.active:
            raise AuthenticationError(COMPANY_INACTIVE, code="COMPANY_INACTIVE")
    elif user.role == UserRole.EMPLOYEE:
        if user.session_company_id is not None:
            employee = user.employee
            if employee is None or not employee.active:
                raise AuthenticationError(NO_LONGER_IN_COMPANY, code="USER_INACTIVE")
            if not employee.company.active:
                raise AuthenticationError(COMPANY_INACTIVE, code="COMPANY_INACTIVE")
        elif not user.usable_employees:
            inactive_company = any(e.active for e in user.employees)
            raise AuthenticationError(
                COMPANY_INACTIVE if inactive_company else ACCOUNT_INACTIVE,
                code="COMPANY_INACTIVE" if inactive_company else "USER_INACTIVE",
            )


class AuthService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.users = UserRepository(db)

    def authenticate(self, email: str, password: str) -> User:
        user = self.users.get_by_email(email)
        # verify_password se ejecuta siempre (hash ficticio si no existe) para no filtrar
        # por tiempo de respuesta qué correos están registrados.
        valid = verify_password(password, user.password_hash if user else None)
        if not user or not valid:
            raise AuthenticationError(INVALID_CREDENTIALS, code="INVALID_CREDENTIALS")
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
                "La contraseña actual no es correcta",
                code="CURRENT_PASSWORD_INVALID",
                details={"field": "current_password"},
            )
        if current_password == new_password:
            raise UnprocessableError("La nueva contraseña debe ser distinta de la actual", code="PASSWORD_REUSED")
        user.password_hash = hash_password(new_password)
