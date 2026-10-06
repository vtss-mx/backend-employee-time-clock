"""Reglas de acceso de un EMPLOYEE a sus propias funciones (una sola implementación).

`ensure_account_usable` (en cada petición) ya garantiza que la cuenta y su empleo en la empresa de
la sesión estén activos; aquí se agrega lo propio de cada función: tener empleo y, para
identificarse o mostrar su QR, una identidad aprobada por su empresa.
"""

from app.core.exceptions import PermissionDeniedError
from app.models import Employee, FaceStatus, User

#: Llave del mensaje según el estado del registro facial (sin registro: `FACE_NOT_ENROLLED_YET`).
_NOT_APPROVED = {
    FaceStatus.PENDING_REVIEW: "FACE_PENDING_REVIEW",
    FaceStatus.REJECTED: "FACE_REJECTED_ENROLL_AGAIN",
}


def active_employee(user: User) -> Employee:
    """El empleo con el que opera la persona (activo)."""
    employee = user.employee
    if employee is None:
        raise PermissionDeniedError(key="EMPLOYEES_ONLY")
    if not employee.active or not user.active:
        raise PermissionDeniedError(code="USER_INACTIVE", key="EMPLOYEE_INACTIVE")
    return employee


def approved_employee(user: User) -> Employee:
    """Empleo activo con la identidad aprobada por su empresa (para identificarse o mostrar su QR)."""
    employee = active_employee(user)
    if employee.face_status != FaceStatus.APPROVED:
        raise PermissionDeniedError(
            code="FACE_NOT_APPROVED", key=_NOT_APPROVED.get(employee.face_status, "FACE_NOT_ENROLLED_YET")
        )
    return employee
