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
    enrollment_rejection_reasons: list[CatalogItem]
    reverification_reasons: list[CatalogItem]
    confidence_levels: list[ConfidenceLevelItem]
    antispoof_levels: list[AntispoofLevelItem]
    face_errors: list[FaceErrorItem]
    enrollment_flags: list[CatalogItem]
