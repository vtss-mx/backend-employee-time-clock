"""Pantallas de cada usuario: el menú y las rutas del frontend salen de aquí.

Dos capas, ambas en el backend:
- Permiso (base de datos, catalog.role_screens): qué pantallas tiene cada rol. Es también el permiso
  de los endpoints que usa cada pantalla (`require_screen` en dependencies.py).
- Disponibilidad (reglas de negocio, aquí): de las pantallas permitidas, cuáles aplican al estado
  actual del usuario. P. ej. el empleado ve "Registro facial" hasta que registra su rostro, "Mi
  código QR" solo con su identidad aprobada y "Cambiar de empresa" solo si trabaja en varias.

El inicio de cada usuario es su primera pantalla disponible (según el orden del catálogo).
"""

from collections.abc import Callable

from app.models import FaceStatus, Screen, User
from app.schemas.user import ScreenRead, UserRead
from app.services.catalog_service import Catalogs, get_catalogs

_ENROLLMENT = {FaceStatus.NOT_ENROLLED, FaceStatus.REJECTED}


def _face_status(user: User) -> FaceStatus | None:
    return user.employee.face_status if user.employee else None


def _in_company(user: User) -> bool:
    """El empleado ya opera en una empresa (quien trabaja en varias la elige al entrar)."""
    return user.employee is not None


#: Pantallas que dependen del estado del usuario; las demás aplican siempre que el rol las tenga.
AVAILABILITY: dict[Screen, Callable[[User], bool]] = {
    Screen.EMPLOYEE_ENROLL: lambda user: _in_company(user) and _face_status(user) in _ENROLLMENT,
    Screen.EMPLOYEE_PENDING: lambda user: _face_status(user) == FaceStatus.PENDING_REVIEW,
    Screen.EMPLOYEE_VERIFY: lambda user: _face_status(user) == FaceStatus.APPROVED,
    Screen.EMPLOYEE_QR: lambda user: _face_status(user) == FaceStatus.APPROVED,
    Screen.EMPLOYEE_SELECT_COMPANY: lambda user: len(user.employees) > 1,
    # Integraciones (API): solo si el ADMIN le dio acceso a la empresa.
    Screen.COMPANY_API: lambda user: bool(user.company and user.company.api_enabled),
}


def screens_for(user: User, catalogs: Catalogs | None = None) -> list[ScreenRead]:
    """Pantallas activas que el rol tiene y que aplican al estado del usuario, en orden."""
    catalogs = catalogs or get_catalogs()
    granted = catalogs.role_screens.get(user.role.value, frozenset())
    return [
        ScreenRead.model_validate(row)
        for row in catalogs.entries["screens"]
        if row["active"] and row["code"] in granted and AVAILABILITY.get(Screen(row["code"]), _always)(user)
    ]


def _always(_: User) -> bool:
    return True


def user_read(user: User) -> UserRead:
    """El usuario como lo recibe el frontend (login, renovación, /users/me): con sus pantallas."""
    return UserRead.model_validate(user).model_copy(update={"screens": screens_for(user)})
