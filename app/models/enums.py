from enum import StrEnum


class UserRole(StrEnum):
    #: Administrador de la plataforma: da de alta y administra empresas (no ve sus empleados).
    ADMIN = "ADMIN"
    #: Administrador de una empresa.
    COMPANY = "COMPANY"
    EMPLOYEE = "EMPLOYEE"
    #: Validador de identidad de una empresa (tableta o teléfono): identifica a sus empleados por
    #: rostro o por su código QR.
    VALIDATOR = "VALIDATOR"


class VerificationMethod(StrEnum):
    FACE = "FACE"
    QR = "QR"
    #: Doble factor (validador): el QR indica quién es y el rostro lo confirma.
    QR_FACE = "QR_FACE"


class ValidatorMode(StrEnum):
    """Cómo identifica a los empleados un validador (lo define su empresa)."""

    QR = "QR"
    FACE = "FACE"
    #: Cualquiera de los dos: el operador elige en cada identificación.
    QR_OR_FACE = "QR_OR_FACE"
    #: Ambos: el QR del empleado y, después, su rostro (deben ser de la misma persona).
    QR_AND_FACE = "QR_AND_FACE"


class FaceStatus(StrEnum):
    """Estado del registro facial del empleado."""

    NOT_ENROLLED = "NOT_ENROLLED"  # aún no registra su rostro (primer inicio de sesión)
    PENDING_REVIEW = "PENDING_REVIEW"  # registrado, en validación por COMPANY
    APPROVED = "APPROVED"  # identidad validada: puede verificarse
    REJECTED = "REJECTED"  # rechazado: debe registrarse de nuevo


class SessionRevocationReason(StrEnum):
    """Por qué se cerró una sesión (catalog.session_revocation_reasons)."""

    LOGOUT = "LOGOUT"
    LOGOUT_ALL = "LOGOUT_ALL"
    REVOKED_BY_USER = "REVOKED_BY_USER"
    PASSWORD_CHANGED = "PASSWORD_CHANGED"
    PASSWORD_RESET = "PASSWORD_RESET"
    ACCOUNT_DEACTIVATED = "ACCOUNT_DEACTIVATED"
    COMPANY_DEACTIVATED = "COMPANY_DEACTIVATED"
    EMPLOYEE_REMOVED = "EMPLOYEE_REMOVED"
    #: Un inicio de sesión nuevo desplazó a este (una sola sesión activa por persona).
    SIGNED_IN_ELSEWHERE = "SIGNED_IN_ELSEWHERE"
    #: Se presentó un refresh token ya rotado: se asume robo.
    REFRESH_REUSE_DETECTED = "REFRESH_REUSE_DETECTED"


class EnrollmentStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
