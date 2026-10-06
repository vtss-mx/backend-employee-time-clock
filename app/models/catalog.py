"""Catálogos (esquema `catalog`): las listas de valores de la aplicación viven en la base de datos.

Cada catálogo tiene un código estable (lo usan la lógica y las llaves foráneas), el nombre que se
muestra, una descripción, su orden y si está activo. La estructura y los datos los crean las
migraciones de Alembic (0020 en adelante; datos en alembic/seed/); estos modelos deben coincidir. Los
Enum de `app/models/enums.py` solo nombran los códigos que la lógica necesita; una prueba
garantiza que coincidan con estas tablas.
"""

from typing import Any

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Numeric, SmallInteger, String, false, text, true
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.db_schemas import CATALOG


class CatalogEntry:
    """Columnas comunes de todos los catálogos."""

    #: Un catálogo con restricciones propias (p. ej. `currencies`) las agrega antes del esquema.
    __table_args__: tuple[Any, ...] = ({"schema": CATALOG},)

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


class CatalogPricingMode(CatalogEntry, Base):
    """Cómo se cobra a una empresa: por empleado activo (prorrateo por día) o monto fijo."""

    __tablename__ = "pricing_modes"


class CatalogPricePeriod(CatalogEntry, Base):
    """A qué tiempo corresponde el precio del plan: día, mes o año."""

    __tablename__ = "price_periods"


class CatalogCurrency(CatalogEntry, Base):
    """Monedas en que se puede cobrar a una empresa (code = código ISO 4217: MXN, USD, EUR).

    `symbol` es el símbolo corto con que se escribe ($, €) y `decimals`, cuántos decimales tiene la moneda:
    cada importe se redondea a ellos al cerrarse (`billing_rules.money`). Las columnas de dinero son
    NUMERIC(…, 2): por eso a lo más 2 decimales (CHECK `decimals`); una moneda con más necesita migrar esas
    columnas antes de agregarse aquí. Las FK de planes, cargos y pagos apuntan a este catálogo (sin índice,
    §3.1.8: nunca se borra)."""

    __tablename__ = "currencies"
    __table_args__ = (
        CheckConstraint("decimals BETWEEN 0 AND 2", name="decimals"),
        {"schema": CATALOG},
    )

    symbol: Mapped[str] = mapped_column(String(8), nullable=False)
    decimals: Mapped[int] = mapped_column(SmallInteger, default=2, server_default=text("2"), nullable=False)


class CatalogDiscountType(CatalogEntry, Base):
    """Descuento del plan: porcentaje del subtotal o monto fijo por cargo."""

    __tablename__ = "discount_types"


class CatalogDiscountRecurrence(CatalogEntry, Base):
    """Cuándo aplica el descuento: en todos los cargos, en los primeros N o cada N."""

    __tablename__ = "discount_recurrences"


class CatalogBillingStatus(ToneMixin, CatalogEntry, Base):
    """Estado de servicio de una empresa: activa o suspendida."""

    __tablename__ = "billing_statuses"


class CatalogSuspensionReason(CatalogEntry, Base):
    """Por qué se suspendió una empresa: falta de pago (automática) o decisión del ADMIN."""

    __tablename__ = "suspension_reasons"


class CatalogChargeStatus(ToneMixin, CatalogEntry, Base):
    """Estado de un cargo: por pagar, pagado o anulado."""

    __tablename__ = "charge_statuses"


class CatalogPaymentStatus(ToneMixin, CatalogEntry, Base):
    """Estado de un pago: confirmado o anulado."""

    __tablename__ = "payment_statuses"


class CatalogPaymentMethod(CatalogEntry, Base):
    """Cómo pagó la empresa (transferencia, depósito, efectivo...): lo anota el ADMIN al registrar el pago."""

    __tablename__ = "payment_methods"


class CatalogCompanyDocumentType(CatalogEntry, Base):
    """Tipos de documento de una empresa (constancia de situación fiscal, acta constitutiva, comprobante de
    domicilio...): los archivos que la empresa y la plataforma guardan para facturarle (`tenancy.company_documents`)."""

    __tablename__ = "company_document_types"


class CatalogStorageCategory(CatalogEntry, Base):
    """Grupos del almacenamiento que ocupa cada empresa (personal, biometría, asistencia...)."""

    __tablename__ = "storage_categories"


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


class CatalogSlowAlertStatus(ToneMixin, CatalogEntry, Base):
    """Seguimiento de una alerta de peticiones lentas (regla 18): abierta, en atención o resuelta."""

    __tablename__ = "slow_alert_statuses"


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


class CatalogTaxIdType(CatalogEntry, Base):
    """Tipos de identificador fiscal de una empresa (migración 0074; decisión del dueño del producto: «puede ser RFC o
    algún otro identificador que se use para poder registrar una empresa de cualquier parte del mundo»).

    `code` lleva el país para no confundir siglas que se repiten entre países (`CO_NIT`, `PE_RUC`, `GB_VAT`...);
    `country_code` es el país al que pertenece o NULL si sirve para cualquiera (`OTHER`, «Otro identificador
    fiscal»). El tipo principal de un país es el primero de los suyos por `sort_order` (el que la aplicación propone).

    Su regla de formato es DATO, no código: `pattern` (expresión regular del número ya normalizado, que se compara
    completa), `min_length`/`max_length` y `example`. La normalización es la misma para todos (mayúsculas, sin
    espacios, guiones, puntos ni diagonales: `app/schemas/tax_ids.py`); el dígito verificador de los que lo tienen y
    las reglas propias del RFC (genéricos, fecha) viven en ese módulo, por código. `short_name` es la sigla con que
    se muestra («RFC», «EIN»), `name` su nombre completo y `description` el formato en palabras (la ayuda del
    campo); los tres se traducen (`catalog.translations`). La FK hacia el país va sin índice (§3.1.8: catálogo)."""

    __tablename__ = "tax_id_types"
    __table_args__ = (
        CheckConstraint("min_length >= 1 AND max_length >= min_length AND max_length <= 30", name="lengths"),
        {"schema": CATALOG},
    )

    country_code: Mapped[str | None] = mapped_column(String(2), ForeignKey(f"{CATALOG}.countries.code"))
    short_name: Mapped[str] = mapped_column(String(30), nullable=False)
    pattern: Mapped[str] = mapped_column(String(200), nullable=False)
    min_length: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    max_length: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    example: Mapped[str] = mapped_column(String(30), nullable=False)


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


# ---------------------------------------------------------------- antifraude (migración 0062)


class CatalogFraudKind(CatalogEntry, Base):
    """Tipos de fraude de un caso (presentación, inyección, reenvío, ubicación falsa...). También agrupan las
    señales del motor de riesgo: cada familia suma a lo más `RISK_FAMILY_MAX_POINTS`."""

    __tablename__ = "fraud_kinds"


class CatalogSignalMode(CatalogEntry, Base):
    """Modo de cada señal del motor de riesgo: apagada, solo medir u obligatoria."""

    __tablename__ = "signal_modes"


class CatalogReviewReason(CatalogEntry, Base):
    """Por qué un registro quedó en revisión, en términos del negocio: es lo que ve la empresa (nunca qué señal
    técnica lo delató, para no enseñarle al atacante cómo rodear el candado)."""

    __tablename__ = "review_reasons"


class CatalogRiskSignal(CatalogEntry, Base):
    """Señales del motor de riesgo con su configuración de la plataforma (el ADMIN la ajusta por empresa en su
    política). Solo la ve el ADMIN: no viaja en `GET /catalogs` (no se le enseña al atacante qué se mide).

    `points`: lo que suma al puntaje; `mode`: apagada, solo medir u obligatoria; `hard`: regla dura (obligatoria,
    niega sin importar el puntaje); `client`: la informa el dispositivo (menos confiable); `kind`: el tipo de fraude
    que sugiere (familia); `review_reason`: cómo se le explica a la empresa."""

    __tablename__ = "risk_signals"

    kind: Mapped[str] = mapped_column(String(30), ForeignKey(f"{CATALOG}.fraud_kinds.code"), nullable=False)
    points: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    mode: Mapped[str] = mapped_column(String(30), ForeignKey(f"{CATALOG}.signal_modes.code"), nullable=False)
    hard: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    client: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    review_reason: Mapped[str] = mapped_column(String(30), ForeignKey(f"{CATALOG}.review_reasons.code"), nullable=False)


class CatalogRiskTier(ToneMixin, CatalogEntry, Base):
    """Niveles del puntaje de riesgo (bajo, medio, alto, crítico)."""

    __tablename__ = "risk_tiers"


class CatalogRiskAction(CatalogEntry, Base):
    """Qué se hace con un nivel de riesgo: permitir, permitir y avisar, un paso más, en revisión o negar."""

    __tablename__ = "risk_actions"


class CatalogAttendanceReviewStatus(ToneMixin, CatalogEntry, Base):
    """Seguimiento de un registro de asistencia "en revisión" (la empresa lo confirma o lo rechaza)."""

    __tablename__ = "attendance_review_statuses"


class CatalogEmployeeDeviceMode(CatalogEntry, Base):
    """Cómo se trata el dispositivo del empleado (decisión D2; `app/services/employee_devices.py`)."""

    __tablename__ = "employee_device_modes"


class CatalogPolicyPreset(CatalogEntry, Base):
    """Niveles predefinidos de la política de verificación (Estándar, Alto, Máximo)."""

    __tablename__ = "policy_presets"


class CatalogPolicyChangeStatus(ToneMixin, CatalogEntry, Base):
    """Estado de un cambio de la política (aplicado o, si relaja la seguridad, por aprobar de otro ADMIN)."""

    __tablename__ = "policy_change_statuses"


class CatalogFraudCaseStatus(ToneMixin, CatalogEntry, Base):
    """Estado de un caso de fraude (abierto, en revisión, confirmado, falso positivo, no concluyente)."""

    __tablename__ = "fraud_case_statuses"


class CatalogFraudCaseEventKind(CatalogEntry, Base):
    """Qué pasó en el historial de un caso de fraude."""

    __tablename__ = "fraud_case_event_kinds"


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


#: Columnas de texto de los catálogos que una persona lee (las que tienen traducción); los idiomas de las traducciones
#: (el de omisión, es-MX, son las columnas de cada catálogo). Un idioma nuevo = su valor aquí y en una migración.
TRANSLATED_FIELDS = ("name", "description", "message", "phrase", "instruction", "employee_note", "short_name")
TRANSLATION_LOCALES = ("en-US",)


class CatalogTranslation(Base):
    """Los textos de los catálogos en los demás idiomas (regla 16 de la raíz; migración 0064).

    Una fila por catálogo, registro, idioma y columna: `('screens', 'ADMIN_DASHBOARD', 'en-US', 'name') →
    'Dashboard'`. El español vive en las columnas de cada catálogo (son el idioma de omisión y lo que leen la lógica y
    las llaves foráneas); aquí solo lo que cambia con el idioma. Una sola tabla para todos los catálogos: agregar un
    idioma o un catálogo no cambia la estructura de ninguno. La caché de catálogos la lee completa al recargar (una
    consulta más cada `CATALOG_CACHE_SECONDS`, ninguna por petición); su llave primaria basta (nadie la filtra).
    """

    __tablename__ = "translations"
    __table_args__ = (
        CheckConstraint(f"locale IN ({', '.join(repr(code) for code in TRANSLATION_LOCALES)})", name="locale"),
        CheckConstraint(f"field IN ({', '.join(repr(name) for name in TRANSLATED_FIELDS)})", name="field"),
        {"schema": CATALOG},
    )

    #: Nombre de la tabla del catálogo (`screens`, `face_errors`...) y código del registro.
    catalog: Mapped[str] = mapped_column(String(40), primary_key=True)
    code: Mapped[str] = mapped_column(String(30), primary_key=True)
    locale: Mapped[str] = mapped_column(String(5), primary_key=True)
    field: Mapped[str] = mapped_column(String(20), primary_key=True)
    #: Cabe en la columna que traduce (lo verifica `tests/test_i18n.py`): el tope es el de la más larga.
    text: Mapped[str] = mapped_column(String(300), nullable=False)
