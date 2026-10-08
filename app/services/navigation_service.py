"""Pantallas de cada usuario: el menú y las rutas del frontend salen de aquí.

Dos capas, ambas en el backend:
- Permiso (base de datos, catalog.role_screens): qué pantallas tiene cada rol. Es también el permiso
  de los endpoints que usa cada pantalla (`require_screen` en dependencies.py).
- Disponibilidad (reglas de negocio, aquí): de las pantallas permitidas, cuáles aplican al estado
  actual del usuario. P. ej. el empleado ve "Registro facial" hasta que registra su rostro, "Mi
  código QR" solo con su identidad aprobada y "Cambiar de empresa" solo si trabaja en varias.

El menú se agrupa en módulos (catalog.menu_modules; cada pantalla va en uno, catalog.menu_module_screens).
El inicio de cada usuario es su primera pantalla disponible (según el orden de los módulos y de las
pantallas).
"""

from collections.abc import Callable

from app.models import FaceStatus, Screen, User
from app.schemas.user import MenuModuleRead, ScreenRead, UserRead
from app.services.catalog_service import Catalogs, get_catalogs

_ENROLLMENT = {FaceStatus.NOT_ENROLLED, FaceStatus.REJECTED}

#: Pantallas que exigen la identidad aprobada (decisión del dueño del producto): mientras el registro
#: facial del empleado no esté aprobado, solo usa su identidad (registrar su rostro, verlo en validación)
#: y su cuenta (perfil, elegir empresa). Además de quitarlas del menú, `require_screen` cierra con 403
#: FACE_NOT_APPROVED cada endpoint que solo sirve a estas pantallas: una API nueva no puede olvidarlo.
IDENTITY_SCREENS = frozenset({Screen.EMPLOYEE_VERIFY, Screen.EMPLOYEE_ATTENDANCE, Screen.EMPLOYEE_QR})


def _face_status(user: User) -> FaceStatus | None:
    return user.employee.face_status if user.employee else None


def _in_company(user: User) -> bool:
    """El empleado ya opera en una empresa (quien trabaja en varias la elige al entrar)."""
    return user.employee is not None


def _requires_documents(user: User) -> bool:
    """La empresa del empleado exige documentos de onboarding (comprobante de domicilio e identificación oficial)."""
    employee = user.employee
    return employee is not None and employee.company.require_employee_documents


#: Pantallas que dependen del estado del usuario; las demás aplican siempre que el rol las tenga.
AVAILABILITY: dict[Screen, Callable[[User], bool]] = {
    Screen.EMPLOYEE_ENROLL: lambda user: _in_company(user) and _face_status(user) in _ENROLLMENT,
    # Documentos del onboarding (decisión del dueño, 2026-10-07): solo si la empresa los exige
    # (`companies.require_employee_documents`, que decide el ADMIN). La empresa del empleado ya viene cargada
    # (`Employee.company`, lazy="joined"): sin consultas de más.
    Screen.EMPLOYEE_DOCUMENTS: lambda user: _requires_documents(user),
    Screen.EMPLOYEE_PENDING: lambda user: _face_status(user) == FaceStatus.PENDING_REVIEW,
    # IDENTITY_SCREENS: verificarse, la asistencia (cada registro se confirma con su rostro) y el QR.
    **dict.fromkeys(IDENTITY_SCREENS, lambda user: _face_status(user) == FaceStatus.APPROVED),
    Screen.EMPLOYEE_SELECT_COMPANY: lambda user: len(user.employees) > 1,
    # Integraciones (API): solo si el ADMIN le dio acceso a la empresa.
    Screen.COMPANY_API: lambda user: bool(user.company and user.company.api_enabled),
    # Validadores: solo si el ADMIN le dio un límite mayor que cero (sus APIs: 403 VALIDATORS_DISABLED).
    Screen.COMPANY_VALIDATORS: lambda user: bool(user.company and user.company.validators_enabled),
}


def screens_for(user: User, catalogs: Catalogs | None = None) -> list[ScreenRead]:
    """Pantallas activas que el rol tiene y que aplican al estado del usuario, agrupadas por módulo
    del menú (en el orden de los módulos y, dentro de cada uno, en el de las pantallas)."""
    catalogs = catalogs or get_catalogs()
    granted = catalogs.role_screens.get(user.role.value, frozenset())
    module_order = {row["code"]: index for index, row in enumerate(catalogs.entries["menu_modules"])}
    screens = [
        ScreenRead.model_validate({**row, "module": catalogs.screen_modules.get(row["code"])})
        for row in catalogs.entries["screens"]
        if row["active"] and row["code"] in granted and AVAILABILITY.get(Screen(row["code"]), _always)(user)
    ]
    # sorted() es estable: dentro de un módulo se conserva el orden de las pantallas.
    return sorted(screens, key=lambda screen: module_order.get(screen.module or "", len(module_order)))


def modules_for(screens: list[ScreenRead], catalogs: Catalogs | None = None) -> list[MenuModuleRead]:
    """Los módulos activos que usan esas pantallas, en el orden del catálogo."""
    catalogs = catalogs or get_catalogs()
    used = {screen.module for screen in screens}
    return [
        MenuModuleRead.model_validate(row)
        for row in catalogs.entries["menu_modules"]
        if row["active"] and row["code"] in used
    ]


def _always(_: User) -> bool:
    return True


def user_read(user: User) -> UserRead:
    """El usuario como lo recibe el frontend (login, renovación, /users/me): con sus pantallas y los
    módulos del menú que las agrupan."""
    catalogs = get_catalogs()
    screens = screens_for(user, catalogs)
    return UserRead.model_validate(user).model_copy(
        update={"screens": screens, "modules": modules_for(screens, catalogs)}
    )
