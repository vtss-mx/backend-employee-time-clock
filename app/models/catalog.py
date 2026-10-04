"""Catálogos (esquema `catalog`): las listas de valores de la aplicación viven en la base de datos.

Cada catálogo tiene un código estable (lo usan la lógica y las llaves foráneas), el nombre que se
muestra, una descripción, su orden y si está activo. La estructura y los datos los crean las
migraciones de Alembic (0020 en adelante; datos en alembic/seed/); estos modelos deben coincidir. Los
Enum de `app/models/enums.py` solo nombran los códigos que la lógica necesita; una prueba
garantiza que coincidan con estas tablas.
"""

from sqlalchemy import Boolean, ForeignKey, Index, Numeric, SmallInteger, String, false, text, true
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.db_schemas import CATALOG


class CatalogEntry:
    """Columnas comunes de todos los catálogos."""

    __table_args__ = ({"schema": CATALOG},)

    code: Mapped[str] = mapped_column(String(30), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str | None] = mapped_column(String(300))
    sort_order: Mapped[int] = mapped_column(SmallInteger, default=0, server_default=text("0"), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)


class ToneMixin:
    #: Color con que se muestra el estado: muted | info | success | warning | danger.
    tone: Mapped[str] = mapped_column(String(20), default="muted", server_default="muted", nullable=False)


class CatalogRole(CatalogEntry, Base):
    """Roles de usuario (ADMIN, COMPANY, EMPLOYEE, VALIDATOR)."""

    __tablename__ = "roles"


class CatalogVerificationMethod(CatalogEntry, Base):
    """Métodos de identificación: rostro, QR, o QR + rostro."""

    __tablename__ = "verification_methods"


class CatalogValidatorMode(CatalogEntry, Base):
    """Modos de un validador de identidad; sus métodos permitidos, en `validator_mode_methods`."""

    __tablename__ = "validator_modes"


class ValidatorModeMethod(Base):
    """Qué métodos de identificación permite cada modo de validador."""

    __tablename__ = "validator_mode_methods"
    __table_args__ = (
        # FK hacia el método: la llave primaria empieza por el modo y no la cubre.
        Index("ix_validator_mode_methods_method_code", "method_code"),
        {"schema": CATALOG},
    )

    mode_code: Mapped[str] = mapped_column(
        ForeignKey(f"{CATALOG}.validator_modes.code", ondelete="CASCADE"), primary_key=True
    )
    method_code: Mapped[str] = mapped_column(ForeignKey(f"{CATALOG}.verification_methods.code"), primary_key=True)
    sort_order: Mapped[int] = mapped_column(SmallInteger, default=0, server_default=text("0"), nullable=False)


class CatalogFaceStatus(ToneMixin, CatalogEntry, Base):
    """Estados del registro facial del empleado (description: explicación para la empresa)."""

    __tablename__ = "face_statuses"

    #: Cómo se le explica el estado al propio empleado.
    employee_note: Mapped[str] = mapped_column(String(120), nullable=False)


class CatalogEnrollmentStatus(ToneMixin, CatalogEntry, Base):
    """Estados de una solicitud de registro facial."""

    __tablename__ = "enrollment_statuses"


class CatalogDeviceStatus(ToneMixin, CatalogEntry, Base):
    """Estados de un dispositivo de validador: pendiente, autorizado, rechazado o revocado."""

    __tablename__ = "device_statuses"


class CatalogWorkMode(CatalogEntry, Base):
    """Desde dónde se registró la asistencia: en sitio, remoto o en un validador."""

    __tablename__ = "work_modes"


class CatalogAttendanceAction(CatalogEntry, Base):
    """Lo que registra el empleado en su turno: entrada, inicio y fin de descanso, salida."""

    __tablename__ = "attendance_actions"


class CatalogWorkSessionStatus(ToneMixin, CatalogEntry, Base):
    """Estados de la jornada de un turno."""

    __tablename__ = "work_session_statuses"


class CatalogShiftRequestStatus(ToneMixin, CatalogEntry, Base):
    """Estados de una solicitud de cambio de turno."""

    __tablename__ = "shift_request_statuses"


class CatalogBoardState(ToneMixin, CatalogEntry, Base):
    """En qué va cada empleado en el tablero de asistencia del día (lo calcula el backend)."""

    __tablename__ = "board_states"


class CatalogAssignmentState(ToneMixin, CatalogEntry, Base):
    """Vigencia de la asignación de un turno: vigente, programada o terminada."""

    __tablename__ = "assignment_states"


class CatalogDayOffType(ToneMixin, CatalogEntry, Base):
    """Tipos de ausencia (días que no se trabaja): vacaciones, permiso, incapacidad, otro motivo.

    Los días festivos no van aquí (son de toda la empresa: `company_holidays`). `requestable`: el
    empleado lo puede pedir desde "Mi asistencia" (la empresa lo aprueba o rechaza); los demás solo
    los registra la empresa. `phrase` arma lo que se le dice al empleado ("Estás de vacaciones del
    ... al ...") cuando intenta checar.
    """

    __tablename__ = "day_off_types"

    phrase: Mapped[str] = mapped_column(String(80), nullable=False)
    requestable: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)


class CatalogAttendanceEditReason(CatalogEntry, Base):
    """Motivos sugeridos cuando la empresa registra o corrige la asistencia de un empleado (también puede
    escribir otro): se guarda el texto con quién y cuándo."""

    __tablename__ = "attendance_edit_reasons"


class CatalogApiScope(CatalogEntry, Base):
    """Permisos que puede tener una llave de la API de integración (solo lectura de la propia empresa)."""

    __tablename__ = "api_scopes"


class CatalogApiKeyStatus(ToneMixin, CatalogEntry, Base):
    """Estado de una llave de la API: activa, vencida o revocada (se deriva de sus fechas)."""

    __tablename__ = "api_key_statuses"


class CatalogErrorStatus(ToneMixin, CatalogEntry, Base):
    """Seguimiento de un error del sistema: pendiente, en proceso, en revisión o solucionado."""

    __tablename__ = "error_statuses"


class CatalogErrorSeverity(ToneMixin, CatalogEntry, Base):
    """Gravedad de un error del sistema: crítico, error o advertencia."""

    __tablename__ = "error_severities"


class CatalogVerificationReason(CatalogEntry, Base):
    """Motivos de un intento fallido (bitácora); `message` es lo que se le muestra a la persona."""

    __tablename__ = "verification_reasons"

    message: Mapped[str] = mapped_column(String(200), nullable=False)


class CatalogAccessory(CatalogEntry, Base):
    """Accesorios que la empresa puede pedir retirar (lentes, gorra, cubrebocas)."""

    __tablename__ = "accessories"

    #: Para armar "Quítate {phrase} para continuar".
    phrase: Mapped[str] = mapped_column(String(60), nullable=False)


class CatalogLivenessAction(CatalogEntry, Base):
    """Retos de la prueba de vida (girar la cabeza) con la instrucción que se muestra."""

    __tablename__ = "liveness_actions"

    instruction: Mapped[str] = mapped_column(String(120), nullable=False)


class CatalogCountry(CatalogEntry, Base):
    """Países para los teléfonos (code = ISO 3166-1 alfa-2) con su lada internacional."""

    __tablename__ = "countries"

    dial_code: Mapped[str] = mapped_column(String(6), nullable=False)
    #: Se muestran primero en el selector de lada.
    featured: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)


class CatalogEnrollmentRejectionReason(CatalogEntry, Base):
    """Motivos sugeridos al rechazar un registro facial (la empresa también puede escribir otro)."""

    __tablename__ = "enrollment_rejection_reasons"


class CatalogReverificationReason(CatalogEntry, Base):
    """Motivos sugeridos al pedir que un empleado registre su rostro de nuevo."""

    __tablename__ = "reverification_reasons"


class CatalogConfidenceLevel(CatalogEntry, Base):
    """Niveles de confianza que la empresa puede exigir (code = posición 80-100 del control).

    Cifras medidas en LFW (6 000 pares) con el modelo de producción: similitud exigida, % de
    impostores aceptados y % de capturas legítimas rechazadas (una sola captura).
    """

    __tablename__ = "confidence_levels"

    #: Único: la política de cada empresa lo referencia (verification_policy.min_confidence).
    value: Mapped[float] = mapped_column(Numeric(7, 5, asdecimal=False), unique=True, nullable=False)
    similarity: Mapped[float] = mapped_column(Numeric(5, 3, asdecimal=False), nullable=False)
    false_accept_rate: Mapped[float] = mapped_column(Numeric(6, 3, asdecimal=False), nullable=False)
    rejection_rate: Mapped[float] = mapped_column(Numeric(5, 1, asdecimal=False), nullable=False)


class CatalogAntispoofLevel(CatalogEntry, Base):
    """Niveles de sensibilidad del anti-spoofing que la empresa puede exigir.

    `threshold`: probabilidad de rostro real por debajo de la cual una captura parece una foto, una
    pantalla o un video. `any_frame`: basta una sola captura sospechosa (si no, decide la mayoría).
    """

    __tablename__ = "antispoof_levels"

    threshold: Mapped[float] = mapped_column(Numeric(4, 3, asdecimal=False), nullable=False)
    any_frame: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)


class CatalogFlashMode(CatalogEntry, Base):
    """Modos del destello de colores de la prueba de vida (verification_policy.flash_liveness)."""

    __tablename__ = "flash_modes"


class CatalogSessionRevocationReason(CatalogEntry, Base):
    """Por qué se cerró una sesión (auth_sessions.revoked_reason) y qué se le dice a la persona."""

    __tablename__ = "session_revocation_reasons"

    message: Mapped[str] = mapped_column(String(200), nullable=False)


class CatalogFaceError(CatalogEntry, Base):
    """Errores de la captura facial que devuelve la API (pose, luz, accesorios, prueba de vida...).

    `message` puede llevar datos del error entre llaves ({max_dimension}); `retryable`: la persona
    puede corregir y volver a intentar.
    """

    __tablename__ = "face_errors"

    message: Mapped[str] = mapped_column(String(200), nullable=False)
    retryable: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)


class CatalogEnrollmentFlag(CatalogEntry, Base):
    """Marcas de un registro facial para que la empresa lo revise (accesorios o posible suplantación)."""

    __tablename__ = "enrollment_flags"


class CatalogScreen(CatalogEntry, Base):
    """Pantallas de la aplicación: el frontend arma el menú y sus rutas con las que recibe del backend.

    `path` es la ruta base de la pantalla; `short_name`, el título corto en la barra superior de los
    teléfonos; `icon`, el nombre del ícono (lucide); `badge`, el contador que acompaña la opción.
    Qué rol ve cada pantalla vive en `role_screens`, y cada endpoint exige la pantalla que lo usa.
    """

    __tablename__ = "screens"

    path: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    short_name: Mapped[str | None] = mapped_column(String(30))
    icon: Mapped[str] = mapped_column(String(40), nullable=False)
    badge: Mapped[str | None] = mapped_column(String(30))


class CatalogMenuModule(CatalogEntry, Base):
    """Módulos del menú: agrupan las pantallas (sus submódulos) en el menú lateral, en este orden.

    `icon` es el nombre del ícono (lucide) del encabezado del módulo. Qué pantalla va en cada módulo
    vive en `menu_module_screens`.
    """

    __tablename__ = "menu_modules"

    icon: Mapped[str] = mapped_column(String(40), nullable=False)


class MenuModuleScreen(Base):
    """En qué módulo del menú va cada pantalla (en uno solo: la llave primaria es la pantalla)."""

    __tablename__ = "menu_module_screens"
    __table_args__ = (
        Index("ix_menu_module_screens_module_code", "module_code"),
        {"schema": CATALOG},
    )

    screen_code: Mapped[str] = mapped_column(
        ForeignKey(f"{CATALOG}.screens.code", ondelete="CASCADE"), primary_key=True
    )
    module_code: Mapped[str] = mapped_column(
        ForeignKey(f"{CATALOG}.menu_modules.code", ondelete="CASCADE"), nullable=False
    )


class RoleScreen(Base):
    """Qué pantallas tiene cada rol (permiso de la pantalla y de los endpoints que usa)."""

    __tablename__ = "role_screens"
    __table_args__ = (
        # FK hacia la pantalla (y "qué roles la tienen"): la llave primaria empieza por el rol.
        Index("ix_role_screens_screen_code", "screen_code"),
        {"schema": CATALOG},
    )

    role_code: Mapped[str] = mapped_column(ForeignKey(f"{CATALOG}.roles.code", ondelete="CASCADE"), primary_key=True)
    screen_code: Mapped[str] = mapped_column(
        ForeignKey(f"{CATALOG}.screens.code", ondelete="CASCADE"), primary_key=True
    )
