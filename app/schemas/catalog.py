"""Catálogos para el frontend (GET /api/catalogs): todo lo que la interfaz muestra como lista."""

from pydantic import BaseModel, ConfigDict


class CatalogItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code: str
    name: str
    description: str | None = None
    sort_order: int
    #: Inactivo: ya no se ofrece en listas de selección, pero sigue nombrando registros viejos.
    active: bool


class StatusItem(CatalogItem):
    #: muted | info | success | warning | danger
    tone: str


class FaceStatusItem(StatusItem):
    #: Cómo se le explica el estado al propio empleado (description es para la empresa).
    employee_note: str


class ValidatorModeItem(CatalogItem):
    #: Métodos de identificación que permite el modo (códigos de verification_methods).
    methods: list[str]


class ReasonItem(CatalogItem):
    message: str


class AccessoryItem(CatalogItem):
    #: Para armar "Quítate {phrase} para continuar" ("los lentes", "el cubrebocas").
    phrase: str


class FaceErrorItem(ReasonItem):
    #: La persona puede corregir (luz, pose, accesorios...) y volver a intentar.
    retryable: bool


class CountryItem(CatalogItem):
    dial_code: str
    featured: bool


class TaxIdTypeItem(CatalogItem):
    """Tipo de identificador fiscal de una empresa (`description` = su formato en palabras, para la ayuda del campo)."""

    #: País al que pertenece; null = sirve para cualquier país («Otro identificador fiscal»).
    country_code: str | None = None
    #: Sigla con que se muestra («RFC», «EIN»).
    short_name: str
    #: Regla del número ya normalizado (mayúsculas, sin espacios, guiones, puntos ni diagonales): expresión regular que
    #: se compara completa y su largo. El backend la vuelve a aplicar al guardar, con el dígito verificador si lo tiene.
    pattern: str
    min_length: int
    max_length: int
    #: Un número de ejemplo, como se guarda.
    example: str


class ConfidenceLevelItem(CatalogItem):
    value: float
    similarity: float
    #: % de impostores aceptados y % de capturas legítimas rechazadas (medidos en LFW).
    false_accept_rate: float
    rejection_rate: float


class AntispoofLevelItem(CatalogItem):
    #: Probabilidad de rostro real por debajo de la cual una captura parece foto, pantalla o video.
    threshold: float
    #: Basta una sola captura sospechosa para rechazar (si no, decide la mayoría).
    any_frame: bool


class CurrencyItem(CatalogItem):
    """Moneda del cobro (`code` = código ISO 4217)."""

    #: Símbolo corto con que se escribe ("$", "€"); el código ISO distingue las monedas con el mismo símbolo.
    symbol: str
    #: Decimales de la moneda: cada importe se redondea a ellos.
    decimals: int


class DayOffTypeItem(StatusItem):
    #: Cómo se le dice al empleado ("Estás de vacaciones") cuando intenta checar ese día.
    phrase: str
    #: El empleado lo puede pedir desde "Mi asistencia" (los demás solo los registra la empresa).
    requestable: bool


class CatalogsRead(BaseModel):
    roles: list[CatalogItem]
    verification_methods: list[CatalogItem]
    validator_modes: list[ValidatorModeItem]
    face_statuses: list[FaceStatusItem]
    enrollment_statuses: list[StatusItem]
    device_statuses: list[StatusItem]
    verification_reasons: list[ReasonItem]
    accessories: list[AccessoryItem]
    countries: list[CountryItem]
    #: Tipos de identificador fiscal de las empresas, por país (ADMIN, alta y edición de empresas).
    tax_id_types: list[TaxIdTypeItem]
    enrollment_rejection_reasons: list[CatalogItem]
    reverification_reasons: list[CatalogItem]
    confidence_levels: list[ConfidenceLevelItem]
    antispoof_levels: list[AntispoofLevelItem]
    #: Modos del destello de colores de la prueba de vida (política `flash_liveness`).
    flash_modes: list[CatalogItem]
    face_errors: list[FaceErrorItem]
    enrollment_flags: list[CatalogItem]
    #: Preguntas de la verificación por voz del registro facial (migración 0079): la app las muestra al empleado y la
    #: empresa las ve en la revisión del video, en el idioma de la petición.
    voice_questions: list[CatalogItem]
    #: Voces de la guía por audio del registro facial (migración 0088): la empresa elige con cuál se dictan las
    #: indicaciones; la síntesis es del navegador (regla 13).
    voice_profiles: list[CatalogItem]
    api_scopes: list[CatalogItem]
    api_key_statuses: list[StatusItem]
    error_statuses: list[StatusItem]
    error_severities: list[StatusItem]
    work_modes: list[CatalogItem]
    attendance_actions: list[CatalogItem]
    work_session_statuses: list[StatusItem]
    shift_request_statuses: list[StatusItem]
    board_states: list[StatusItem]
    assignment_states: list[StatusItem]
    day_off_types: list[DayOffTypeItem]
    attendance_edit_reasons: list[CatalogItem]
    #: Cobranza (ADMIN): cómo se cobra, a qué tiempo corresponde el precio, descuentos, estados y medios de pago.
    pricing_modes: list[CatalogItem]
    price_periods: list[CatalogItem]
    discount_types: list[CatalogItem]
    discount_recurrences: list[CatalogItem]
    billing_statuses: list[StatusItem]
    suspension_reasons: list[CatalogItem]
    charge_statuses: list[StatusItem]
    payment_statuses: list[StatusItem]
    payment_methods: list[CatalogItem]
    #: Documentos de la empresa (migración 0075): tipos de los archivos que se guardan para facturarle.
    company_document_types: list[CatalogItem]
    employee_document_types: list[CatalogItem]
    #: Monedas en que se puede cobrar a una empresa (la de su plan).
    currencies: list[CurrencyItem]
    #: Consumo (ADMIN): grupos del almacenamiento de cada empresa.
    storage_categories: list[CatalogItem]
    #: Rendimiento (ADMIN, migración 0063): seguimiento de una alerta de peticiones lentas (regla 18).
    slow_alert_statuses: list[StatusItem]
    #: Antifraude (migración 0062): tipos de fraude, modos de una señal, motivos de revisión (lo que ve la empresa
    #: de un registro "en revisión"), niveles y acciones de riesgo, estados de un registro en revisión, modos del
    #: dispositivo del empleado, niveles predefinidos de la política, estados de un cambio de la política y de un
    #: caso de fraude y lo que pasa en su historial. Las señales del motor (qué se mide) NO viajan aquí: solo las ve
    #: el ADMIN en la política (no se le enseña al atacante qué se mide).
    fraud_kinds: list[CatalogItem]
    signal_modes: list[CatalogItem]
    review_reasons: list[CatalogItem]
    risk_tiers: list[StatusItem]
    risk_actions: list[CatalogItem]
    attendance_review_statuses: list[StatusItem]
    employee_device_modes: list[CatalogItem]
    policy_presets: list[CatalogItem]
    policy_change_statuses: list[StatusItem]
    fraud_case_statuses: list[StatusItem]
    fraud_case_event_kinds: list[CatalogItem]
