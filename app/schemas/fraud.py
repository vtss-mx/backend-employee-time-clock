"""Casos de fraude que revisa el ADMIN de la plataforma (decisión D10): contrato de la API."""

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.i18n import LocalizedValueError
from app.schemas.common import EmployeeRef, Page


class RiskReasonRead(BaseModel):
    """Una señal del motor de riesgo en un intento: su valor, su umbral, sus puntos y su modo."""

    code: str
    #: Nombre de la señal (catalog.risk_signals no viaja a la app: el contrato lo trae resuelto).
    name: str
    #: Qué mide y por qué delata un fraude (la explicación del catálogo, en el idioma de quien lee).
    description: str | None = None
    points: int
    mode: str
    kind: str
    value: float | None = None
    threshold: float | None = None


class FraudCaseRead(BaseModel):
    """Un caso en la bandeja: de qué empresa, de quién, qué tipo de fraude sugiere y cuántos intentos suma."""

    id: int
    company_id: int
    company_name: str
    status: str
    kind: str
    #: El motivo que abrió el caso (motivo de la bitácora o señal del motor) y su nombre.
    reason: str
    reason_name: str
    #: El empleado (su ficha de trabajo: nombre y número) o, si no se supo quién era, la cuenta que operó la cámara.
    employee: EmployeeRef | None = None
    actor: str | None = None
    attempts: int
    max_score: int | None = None
    tier: str | None = None
    evidence: int
    created_at: datetime
    last_attempt_at: datetime
    decided_by: str | None = None
    decided_at: datetime | None = None
    decision_note: str | None = None


class FraudCaseList(Page[FraudCaseRead]):
    pass


class FraudNetworkRead(BaseModel):
    """La red de la IP del intento según la base local DB-IP Lite (la IP nunca sale del servidor; CC BY 4.0)."""

    country: str | None = None
    asn: int | None = None
    organization: str | None = None
    #: Nube o centro de datos (VPN, servidor intermediario o un programa).
    hosting: bool = False


class FraudCaseAttemptRead(BaseModel):
    """Un intento del caso con la copia de lo que se midió (sobrevive a la retención de la bitácora)."""

    id: int
    attempted_at: datetime
    success: bool
    reason: str | None = None
    score: int | None = None
    action: str | None = None
    signals: list[RiskReasonRead]
    metrics: dict[str, float | int | str]
    #: Huellas de sus capturas (lo que pasa a la lista de bloqueo si se confirma).
    signatures: int
    camera: str | None = None
    ip_address: str | None = None
    #: La red de su IP (consultada al leer el caso; None sin base local o con una IP privada).
    network: FraudNetworkRead | None = None
    user_agent: str | None = None


class FraudCaseEventRead(BaseModel):
    id: int
    created_at: datetime
    kind: str
    actor: str | None = None
    status_from: str | None = None
    status_to: str | None = None
    note: str | None = None


class FraudEvidenceRead(BaseModel):
    """Un fotograma de evidencia (la imagen se pide aparte: `GET .../evidence/{id}`)."""

    id: int
    kind: str
    position: int
    created_at: datetime


class FraudCaseDetail(FraudCaseRead):
    attempts_detail: list[FraudCaseAttemptRead]
    events: list[FraudCaseEventRead]
    evidence_items: list[FraudEvidenceRead]


class FraudCaseCount(BaseModel):
    """Casos por revisar (contador del menú del ADMIN), con tope."""

    active: int


class FraudCaseDecision(BaseModel):
    """Revisar un caso: tomarlo (IN_REVIEW) o decidirlo. Confirmar o descartar exige una nota (queda en su
    historial)."""

    status: str = Field(max_length=20, description="IN_REVIEW, CONFIRMED, FALSE_POSITIVE o INCONCLUSIVE")
    note: str | None = Field(default=None, max_length=1000)

    @field_validator("note")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        return " ".join(value.split()) or None if value else None


class FraudCaseNote(BaseModel):
    note: str = Field(min_length=3, max_length=1000)

    @field_validator("note")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = " ".join(value.split())
        if len(value) < 3:
            raise LocalizedValueError("FRAUD_NOTE_TEXT_REQUIRED")
        return value


class FraudEvidenceImage(BaseModel):
    """Un fotograma descifrado (el contrato único es JSON: la imagen va en base64, como la foto de perfil)."""

    id: int
    kind: str
    position: int
    content_type: str
    data: str
