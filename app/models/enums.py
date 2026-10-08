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
    #: Rostro por la API pública de verificación (SDK de Android o iOS con la llave de la empresa, migración 0084): la
    #: bitácora distingue lo que vino de la aplicación de la empresa de lo que pasó en la aplicación web.
    API_FACE = "API_FACE"


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
    #: El ADMIN de la plataforma suspendió a la empresa (falta de pago o decisión manual).
    COMPANY_SUSPENDED = "COMPANY_SUSPENDED"
    #: La cuenta (validador) o su empresa se eliminó (borrado lógico, migración 0068).
    ACCOUNT_DELETED = "ACCOUNT_DELETED"


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
    #: Verificación e identificación facial desde la aplicación móvil de la empresa (SDK; migración 0084). SOLO abre
    #: `/integrations/v1/verification/*`: no lee empleados, asistencia ni validadores.
    VERIFICATION = "VERIFICATION"


class ErrorStatus(StrEnum):
    """Seguimiento de un error del sistema (catalog.error_statuses): lo decide el ADMIN."""

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    IN_REVIEW = "IN_REVIEW"
    RESOLVED = "RESOLVED"


class SlowAlertStatus(StrEnum):
    """Seguimiento de una alerta de peticiones lentas (catalog.slow_alert_statuses, regla 18): lo decide el ADMIN;
    una RESUELTA que vuelve a ocurrir se reabre sola."""

    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"


class PerfKind(StrEnum):
    """Qué mide una fila de `ops.perf_*` (columna `kind`). No es un catálogo: nadie lo lee como texto (la pantalla
    nombra cada tipo en sus diccionarios) y solo lo escribe el código."""

    HTTP = "HTTP"  # una petición, por método y plantilla de su ruta
    FUNCTION = "FUNCTION"  # una función clave medida con `observed`
    WEB_API = "WEB_API"  # una petición vista desde el navegador
    WEB_LCP = "WEB_LCP"  # Web Vitals de cada pantalla de la aplicación web (CLS × 1000 en el histograma)
    WEB_INP = "WEB_INP"
    WEB_CLS = "WEB_CLS"
    WEB_FCP = "WEB_FCP"
    WEB_TTFB = "WEB_TTFB"
    WEB_LONG_TASK = "WEB_LONG_TASK"  # tareas largas del hilo principal del navegador


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


class Currency(StrEnum):
    """Monedas en que se cobra a una empresa (catalog.currencies; código ISO 4217).

    Cada empresa se cobra en la moneda de su plan y TODO su dinero (cargos, pagos, saldos, pronóstico)
    queda en esa moneda: nunca se suman ni se convierten monedas distintas. La lógica solo nombra la
    moneda por omisión; cuáles se ofrecen y sus decimales los decide el catálogo."""

    MXN = "MXN"  # peso mexicano (por omisión)
    USD = "USD"
    EUR = "EUR"


class DiscountType(StrEnum):
    """Descuento en porcentaje del subtotal o monto fijo por cargo (catalog.discount_types)."""

    PERCENT = "PERCENT"
    AMOUNT = "AMOUNT"


class DiscountRecurrence(StrEnum):
    """Cuándo aplica el descuento (catalog.discount_recurrences)."""

    ALWAYS = "ALWAYS"  # en todos los cargos
    FIRST = "FIRST"  # solo en los primeros N cargos
    EVERY = "EVERY"  # cada N cargos (el N-ésimo, el 2N-ésimo...)


class BillingStatus(StrEnum):
    """Estado de servicio de una empresa (catalog.billing_statuses): se deriva de `companies.suspended_at`."""

    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"  # nadie de la empresa inicia sesión ni opera hasta que se reactive


class SuspensionReason(StrEnum):
    """Por qué se suspendió una empresa (catalog.suspension_reasons)."""

    NON_PAYMENT = "NON_PAYMENT"  # automática: un cargo siguió sin pagarse después de la gracia
    MANUAL = "MANUAL"  # la decidió el ADMIN, con su motivo


class ChargeStatus(StrEnum):
    """Estado de un cargo (catalog.charge_statuses)."""

    OPEN = "OPEN"  # con saldo pendiente
    PAID = "PAID"
    VOID = "VOID"  # anulado: no se cobra y lo que tenía aplicado queda a favor


class PaymentStatus(StrEnum):
    """Estado de un pago registrado a mano por el ADMIN (catalog.payment_statuses)."""

    CONFIRMED = "CONFIRMED"
    VOID = "VOID"  # anulado: los cargos que cubría vuelven a quedar por pagar


class StorageCategory(StrEnum):
    """Grupos del almacenamiento que ocupa cada empresa (catalog.storage_categories)."""

    PEOPLE = "PEOPLE"
    BIOMETRICS = "BIOMETRICS"
    ATTENDANCE = "ATTENDANCE"
    SECURITY = "SECURITY"
    BILLING = "BILLING"


class ApiKeyStatus(StrEnum):
    """Estado de una llave de la API (catalog.api_key_statuses): se deriva de sus fechas."""

    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


class EnrollmentStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class VoiceQuestion(StrEnum):
    """Preguntas de la verificación por voz y video del registro facial (catalog.voice_questions): sobre datos que el
    empleado tiene registrados y conoce, más una suma al azar como prueba cognitiva (decisión del dueño del producto,
    2026-10-06; repertorio ampliado el 2026-10-07). `app/services/voice_questions` decide cuáles son elegibles para
    cada empleado y elige `VOICE_QUESTIONS_PER_SESSION` al azar, sin repetir el mismo tipo en una sesión.

    Variedad (todo validado en el servidor, `app/speech/matching.py`):
    - Nombre: `FIRST_NAME` (solo el nombre), `SURNAMES` (los apellidos completos), `FULL_NAME` (nombre y apellidos) y,
      SOLO cuando `last_name` se separa en EXACTAMENTE dos palabras (el caso común «Pérez López»), `FIRST_SURNAME` y
      `SECOND_SURNAME`; con un apellido compuesto («De la Cruz») no se puede partir con certeza, así que esas dos se
      omiten y se usa `SURNAMES`.
    - Fecha de nacimiento: `BIRTH_DATE` (completa), `BIRTH_MONTH` (mes, por nombre o cifra), `BIRTH_YEAR` (año, completo
      o sus dos últimas cifras) y `BIRTH_DAY` (día).
    - Empresa: `COMPANY_NAME`; número de empleado: `EMPLOYEE_NUMBER` (solo si lo tiene); departamento y sitio:
      `DEPARTMENT`, `WORK_SITE` (solo si están asignados y vigentes).
    - `ARITHMETIC_SUM`: la suma de dos números pequeños al azar que genera el servidor; NO es un dato de identidad (es
      una prueba cognitiva y antirreplay: cambia en cada intento).
    """

    FULL_NAME = "FULL_NAME"
    BIRTH_DATE = "BIRTH_DATE"
    COMPANY_NAME = "COMPANY_NAME"
    EMPLOYEE_NUMBER = "EMPLOYEE_NUMBER"
    DEPARTMENT = "DEPARTMENT"
    WORK_SITE = "WORK_SITE"
    FIRST_NAME = "FIRST_NAME"
    SURNAMES = "SURNAMES"
    FIRST_SURNAME = "FIRST_SURNAME"
    SECOND_SURNAME = "SECOND_SURNAME"
    BIRTH_MONTH = "BIRTH_MONTH"
    BIRTH_YEAR = "BIRTH_YEAR"
    BIRTH_DAY = "BIRTH_DAY"
    ARITHMETIC_SUM = "ARITHMETIC_SUM"


class FraudKind(StrEnum):
    """Tipo de fraude de un caso y familia de las señales del motor de riesgo (catalog.fraud_kinds)."""

    PRESENTATION = "PRESENTATION"  # foto, pantalla o máscara frente a la cámara
    INJECTION = "INJECTION"  # cámara virtual, programa o llamada directa a la API
    REPLAY = "REPLAY"  # capturas reenviadas o artefactos de un ataque conocido
    LOCATION = "LOCATION"  # ubicación falsa
    BUDDY_PUNCHING = "BUDDY_PUNCHING"  # una persona registra por otra
    INTERNAL = "INTERNAL"  # abuso desde la empresa o un validador
    MORPH = "MORPH"  # registro facial con rasgos de dos personas
    OTHER = "OTHER"


class SignalMode(StrEnum):
    """Modo de una señal del motor de riesgo (catalog.signal_modes)."""

    OFF = "OFF"  # no se mide
    OBSERVE = "OBSERVE"  # se mide y se registra, sin cambiar la decisión (calibración)
    ENFORCE = "ENFORCE"  # sus puntos deciden; una regla dura niega


class RiskSignal(StrEnum):
    """Señales que mide el motor de riesgo (catalog.risk_signals: puntos, modo y tipo por omisión)."""

    SPOOF_PROB_LOW = "SPOOF_PROB_LOW"
    FLASH_WEAK = "FLASH_WEAK"
    FLASH_FLAT = "FLASH_FLAT"
    REPLAY_PERCEPTUAL = "REPLAY_PERCEPTUAL"
    KNOWN_ATTACK = "KNOWN_ATTACK"
    MATCH_MARGIN_LOW = "MATCH_MARGIN_LOW"
    CAMERA_LABEL_MISSING = "CAMERA_LABEL_MISSING"
    LOCATION_EDGE = "LOCATION_EDGE"
    LOCATION_ROUND_ACCURACY = "LOCATION_ROUND_ACCURACY"
    COMPANY_UNDER_ATTACK = "COMPANY_UNDER_ATTACK"
    # --- Antifraude 1b (migración 0065): nacen en "solo medir" y nunca niegan (risk_rules.ASK_ONLY_SIGNALS) ---
    DEVICE_NEW = "DEVICE_NEW"
    DEVICE_KEY_MISSING = "DEVICE_KEY_MISSING"
    DEVICE_SHARED = "DEVICE_SHARED"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    NETWORK_HOSTING = "NETWORK_HOSTING"
    NETWORK_COUNTRY_MISMATCH = "NETWORK_COUNTRY_MISMATCH"
    NETWORK_JUMP = "NETWORK_JUMP"
    LOCATION_STATIC = "LOCATION_STATIC"
    LOCATION_JUMP = "LOCATION_JUMP"
    AUTOMATION = "AUTOMATION"
    VIRTUAL_CAMERA_PRESENT = "VIRTUAL_CAMERA_PRESENT"
    TRACK_INCONSISTENT = "TRACK_INCONSISTENT"
    FRAME_TIMING_SYNTHETIC = "FRAME_TIMING_SYNTHETIC"
    SCREEN_INCOHERENT = "SCREEN_INCOHERENT"
    TELEMETRY_MISSING = "TELEMETRY_MISSING"
    JPEG_TABLE_UNKNOWN = "JPEG_TABLE_UNKNOWN"
    # --- Antifraude 2a (migración 0066): protocolo de captura; igual, nacen en "solo medir" y nunca niegan ---
    BURST_MISSING = "BURST_MISSING"
    BURST_DISCONTINUOUS = "BURST_DISCONTINUOUS"
    BURST_FROZEN = "BURST_FROZEN"
    BURST_LOOP = "BURST_LOOP"
    MOIRE_HIGH = "MOIRE_HIGH"
    NOISE_MISMATCH = "NOISE_MISMATCH"
    PERSPECTIVE_FLAT = "PERSPECTIVE_FLAT"
    PULSE_ABSENT = "PULSE_ABSENT"  # solo se mide (risk_rules.MEASURE_ONLY_SIGNALS): nunca decide
    FLASH_UNPACED = "FLASH_UNPACED"
    FLASH_PACE_TIMING = "FLASH_PACE_TIMING"
    FLASH_PACE_MISMATCH = "FLASH_PACE_MISMATCH"
    # --- Antifraude 2b (migración 0070): presencia del validador y del empleado; nacen en "solo medir" ---
    VALIDATOR_UNSIGNED = "VALIDATOR_UNSIGNED"
    VALIDATOR_SIGNATURE_INVALID = "VALIDATOR_SIGNATURE_INVALID"  # regla dura (firma alterada), también en "solo medir"
    VALIDATOR_KEY_MISMATCH = "VALIDATOR_KEY_MISMATCH"
    VALIDATOR_LOCATION_MISSING = "VALIDATOR_LOCATION_MISSING"
    VALIDATOR_LOCATION_INACCURATE = "VALIDATOR_LOCATION_INACCURATE"
    VALIDATOR_OUT_OF_ZONE = "VALIDATOR_OUT_OF_ZONE"
    SITE_CODE_MISSING = "SITE_CODE_MISSING"
    SITE_CODE_INVALID = "SITE_CODE_INVALID"


class RiskTier(StrEnum):
    """Nivel del puntaje de riesgo (catalog.risk_tiers)."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class RiskAction(StrEnum):
    """Qué se hace con un intento según su nivel de riesgo (catalog.risk_actions), de menos a más estricto."""

    ALLOW = "ALLOW"
    ALERT = "ALERT"  # permitir y abrir un caso para el ADMIN
    STEP_UP = "STEP_UP"  # un reto más exigente en el momento
    REVIEW = "REVIEW"  # se registra "en revisión" (la empresa confirma o rechaza) y se abre un caso
    DENY = "DENY"  # se niega, cuenta para el bloqueo y se abre un caso


class AttendanceReviewStatus(StrEnum):
    """Seguimiento de un registro de asistencia "en revisión" (catalog.attendance_review_statuses)."""

    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"


class EmployeeDeviceMode(StrEnum):
    """Dispositivo del empleado (catalog.employee_device_modes; decisión D2, `app/services/employee_devices.py`)."""

    OFF = "OFF"
    OBSERVE = "OBSERVE"
    STEP_UP = "STEP_UP"
    APPROVAL = "APPROVAL"


class PolicyPreset(StrEnum):
    """Niveles predefinidos de la política de verificación (catalog.policy_presets)."""

    STANDARD = "STANDARD"
    HIGH = "HIGH"
    MAXIMUM = "MAXIMUM"


class PolicyChangeStatus(StrEnum):
    """Estado de un cambio de la política de verificación (catalog.policy_change_statuses)."""

    APPLIED = "APPLIED"
    PENDING = "PENDING"  # relaja la seguridad: espera la aprobación de otro ADMIN (regla de dos personas)
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class FraudCaseStatus(StrEnum):
    """Estado de un caso de fraude (catalog.fraud_case_statuses)."""

    OPEN = "OPEN"
    IN_REVIEW = "IN_REVIEW"
    CONFIRMED = "CONFIRMED"
    FALSE_POSITIVE = "FALSE_POSITIVE"
    INCONCLUSIVE = "INCONCLUSIVE"


class FraudCaseEventKind(StrEnum):
    """Qué pasó en el historial de un caso de fraude (catalog.fraud_case_event_kinds)."""

    OPENED = "OPENED"
    STATUS_CHANGED = "STATUS_CHANGED"
    NOTE = "NOTE"
    EVIDENCE_VIEWED = "EVIDENCE_VIEWED"
    SIGNATURES_BLOCKED = "SIGNATURES_BLOCKED"
    SIGNATURES_RELEASED = "SIGNATURES_RELEASED"
    LEARNING_FORGOTTEN = "LEARNING_FORGOTTEN"


class Screen(StrEnum):
    """Pantallas de la aplicación (catalog.screens). Qué rol tiene cada una vive en catalog.role_screens."""

    ADMIN_DASHBOARD = "ADMIN_DASHBOARD"
    ADMIN_COMPANIES = "ADMIN_COMPANIES"
    ADMIN_ERRORS = "ADMIN_ERRORS"
    ADMIN_FACE_SECURITY = "ADMIN_FACE_SECURITY"
    ADMIN_FRAUD_CASES = "ADMIN_FRAUD_CASES"
    ADMIN_BILLING = "ADMIN_BILLING"
    ADMIN_USAGE = "ADMIN_USAGE"
    ADMIN_PERFORMANCE = "ADMIN_PERFORMANCE"
    #: Deriva de las señales del motor facial (antifraude fase 3): por señal, plataforma y empresa, cada semana.
    ADMIN_DRIFT = "ADMIN_DRIFT"
    COMPANY_DASHBOARD = "COMPANY_DASHBOARD"
    COMPANY_EMPLOYEES = "COMPANY_EMPLOYEES"
    COMPANY_DEPARTMENTS = "COMPANY_DEPARTMENTS"
    COMPANY_VALIDATIONS = "COMPANY_VALIDATIONS"
    COMPANY_VALIDATORS = "COMPANY_VALIDATORS"
    COMPANY_API = "COMPANY_API"
    COMPANY_DOCUMENTS = "COMPANY_DOCUMENTS"
    COMPANY_SHIFTS = "COMPANY_SHIFTS"
    COMPANY_CALENDAR = "COMPANY_CALENDAR"
    COMPANY_SITES = "COMPANY_SITES"
    COMPANY_ATTENDANCE = "COMPANY_ATTENDANCE"
    #: Verificaciones de identidad con su ubicación en el mapa (decisión del dueño, 2026-10-07; migración 0085).
    COMPANY_VERIFICATIONS = "COMPANY_VERIFICATIONS"
    VALIDATOR_CHECKPOINT = "VALIDATOR_CHECKPOINT"
    EMPLOYEE_ATTENDANCE = "EMPLOYEE_ATTENDANCE"
    EMPLOYEE_ENROLL = "EMPLOYEE_ENROLL"
    #: Documentos de identidad del onboarding (decisión del dueño, 2026-10-07): solo si la empresa los exige.
    EMPLOYEE_DOCUMENTS = "EMPLOYEE_DOCUMENTS"
    EMPLOYEE_PENDING = "EMPLOYEE_PENDING"
    EMPLOYEE_VERIFY = "EMPLOYEE_VERIFY"
    EMPLOYEE_QR = "EMPLOYEE_QR"
    EMPLOYEE_SELECT_COMPANY = "EMPLOYEE_SELECT_COMPANY"
    PROFILE = "PROFILE"
