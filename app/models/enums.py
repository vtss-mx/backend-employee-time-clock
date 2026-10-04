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
    #: La empresa exigió o cambió la ubicación del validador: debe volver a entrar en ese lugar.
    LOCATION_POLICY_CHANGED = "LOCATION_POLICY_CHANGED"
    #: La empresa revocó o rechazó el dispositivo del validador.
    DEVICE_REVOKED = "DEVICE_REVOKED"


class DeviceStatus(StrEnum):
    """Estado de un dispositivo de validador (catalog.device_statuses)."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    REVOKED = "REVOKED"


class ApiScope(StrEnum):
    """Permiso de una llave de la API de integración (catalog.api_scopes)."""

    EMPLOYEES_READ = "EMPLOYEES_READ"
    ATTENDANCE_READ = "ATTENDANCE_READ"
    VALIDATORS_READ = "VALIDATORS_READ"


class ErrorStatus(StrEnum):
    """Seguimiento de un error del sistema (catalog.error_statuses): lo decide el ADMIN."""

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    IN_REVIEW = "IN_REVIEW"
    RESOLVED = "RESOLVED"


class ErrorSeverity(StrEnum):
    """Gravedad de un error del sistema (catalog.error_severities)."""

    CRITICAL = "CRITICAL"  # falla no controlada (500) o en segundo plano
    ERROR = "ERROR"  # servicio no disponible u ocupado (5xx controlado)
    WARNING = "WARNING"  # solicitud rechazada (4xx: validación, permisos, reglas de negocio)


class FlashMode(StrEnum):
    """Destello de colores de la prueba de vida (catalog.flash_modes, verification_policy.flash_liveness)."""

    OFF = "OFF"
    OBSERVE = "OBSERVE"  # se pide y se mide, pero nunca bloquea (calibración con capturas reales)
    ENFORCE = "ENFORCE"  # obligatorio: un rostro que no refleja los colores no pasa


class WorkMode(StrEnum):
    """Desde dónde se registró la asistencia (catalog.work_modes)."""

    ON_SITE = "ON_SITE"  # dentro de la geocerca de uno de sus sitios de trabajo
    REMOTE = "REMOTE"  # desde cualquier lugar, en un día que la empresa le permite trabajar remoto
    VALIDATOR = "VALIDATOR"  # al identificarse en la tableta de un validador (cuenta como en sitio)
    COMPANY = "COMPANY"  # lo registró o corrigió la empresa (sin rostro ni ubicación), con su motivo


class AttendanceAction(StrEnum):
    """Lo que registra el empleado en su turno (catalog.attendance_actions)."""

    CHECK_IN = "CHECK_IN"
    BREAK_START = "BREAK_START"
    BREAK_END = "BREAK_END"
    CHECK_OUT = "CHECK_OUT"


class WorkSessionStatus(StrEnum):
    """Estado de la jornada de un turno (catalog.work_session_statuses)."""

    OPEN = "OPEN"  # con entrada y sin salida
    CLOSED = "CLOSED"  # con entrada y salida
    MISSED_CHECKOUT = "MISSED_CHECKOUT"  # venció el límite para checar la salida sin hacerlo


class BoardState(StrEnum):
    """En qué va cada empleado en el tablero del día (catalog.board_states)."""

    SCHEDULED = "SCHEDULED"  # aún no es la hora de su entrada
    MISSING = "MISSING"  # ya debió entrar y no ha checado
    WORKING = "WORKING"
    ON_BREAK = "ON_BREAK"
    DONE = "DONE"  # checó su salida
    MISSED_CHECKOUT = "MISSED_CHECKOUT"  # venció su límite de salida sin checarla
    ABSENT = "ABSENT"  # terminó su turno sin entrada
    DAY_OFF = "DAY_OFF"  # ese día no trabaja (festivo o ausencia aprobada): no es una falta


class AssignmentState(StrEnum):
    """Vigencia de la asignación de un turno (catalog.assignment_states)."""

    CURRENT = "CURRENT"  # rige hoy
    SCHEDULED = "SCHEDULED"  # empieza después (cambio programado)
    ENDED = "ENDED"


class ShiftRequestStatus(StrEnum):
    """Seguimiento de una solicitud de cambio de turno (catalog.shift_request_statuses)."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class PricingMode(StrEnum):
    """Cómo se cobra a una empresa (catalog.pricing_modes)."""

    PER_USER = "PER_USER"  # por cada empleado activo, día por día (prorrateo)
    FLAT = "FLAT"  # un monto fijo por empresa


class PricePeriod(StrEnum):
    """A qué tiempo corresponde el precio (catalog.price_periods)."""

    DAY = "DAY"
    MONTH = "MONTH"
    YEAR = "YEAR"


class DiscountType(StrEnum):
    """Descuento en porcentaje del subtotal o monto fijo por cargo (catalog.discount_types)."""

    PERCENT = "PERCENT"
    AMOUNT = "AMOUNT"


class DiscountRecurrence(StrEnum):
    """Cuándo aplica el descuento (catalog.discount_recurrences)."""

    ALWAYS = "ALWAYS"  # en todos los cargos
    FIRST = "FIRST"  # solo en los primeros N cargos
    EVERY = "EVERY"  # cada N cargos (el N-ésimo, el 2N-ésimo...)


class ApiKeyStatus(StrEnum):
    """Estado de una llave de la API (catalog.api_key_statuses): se deriva de sus fechas."""

    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


class EnrollmentStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class Screen(StrEnum):
    """Pantallas de la aplicación (catalog.screens). Qué rol tiene cada una vive en catalog.role_screens."""

    ADMIN_DASHBOARD = "ADMIN_DASHBOARD"
    ADMIN_COMPANIES = "ADMIN_COMPANIES"
    ADMIN_ERRORS = "ADMIN_ERRORS"
    ADMIN_FACE_SECURITY = "ADMIN_FACE_SECURITY"
    COMPANY_DASHBOARD = "COMPANY_DASHBOARD"
    COMPANY_EMPLOYEES = "COMPANY_EMPLOYEES"
    COMPANY_DEPARTMENTS = "COMPANY_DEPARTMENTS"
    COMPANY_VALIDATIONS = "COMPANY_VALIDATIONS"
    COMPANY_VALIDATORS = "COMPANY_VALIDATORS"
    COMPANY_API = "COMPANY_API"
    COMPANY_SHIFTS = "COMPANY_SHIFTS"
    COMPANY_CALENDAR = "COMPANY_CALENDAR"
    COMPANY_SITES = "COMPANY_SITES"
    COMPANY_ATTENDANCE = "COMPANY_ATTENDANCE"
    VALIDATOR_CHECKPOINT = "VALIDATOR_CHECKPOINT"
    EMPLOYEE_ATTENDANCE = "EMPLOYEE_ATTENDANCE"
    EMPLOYEE_ENROLL = "EMPLOYEE_ENROLL"
    EMPLOYEE_PENDING = "EMPLOYEE_PENDING"
    EMPLOYEE_VERIFY = "EMPLOYEE_VERIFY"
    EMPLOYEE_QR = "EMPLOYEE_QR"
    EMPLOYEE_SELECT_COMPANY = "EMPLOYEE_SELECT_COMPANY"
    PROFILE = "PROFILE"
