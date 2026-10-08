"""Antifraude de identidad (migración 0062; docs/rd/antifraude-identidad.md, fases 0 y 1).

- `CaptureTrace`: huellas perceptuales de las capturas frontales de cada intento (pHash del rostro y del cuadro y
  el embedding CIFRADO) para detectar el reenvío de una captura modificada (recomprimida, con brillo, recortada).
  Se conservan `FACE_REPLAY_RETENTION_DAYS` días; nunca la imagen.
- `PolicyChange`: auditoría de cada cambio de la política de verificación (quién, cuándo, antes → después) y la
  regla de dos personas: lo que relaja la seguridad espera la aprobación de otro ADMIN (decisión D12).
- `RiskAssessment`: la decisión del motor de riesgo de cada intento (puntaje, nivel, acción y sus motivos).
  Particionada por mes; se conserva `FACE_METRICS_RETENTION_DAYS`.
- `FraudCase`, `FraudCaseAttempt`, `FraudCaseEvent`: los casos que revisa el ADMIN (decisión D10), sus intentos
  (con una COPIA de sus números y motivos: el caso sobrevive a la retención de la bitácora) y su historial.
- `FraudEvidence`: la referencia de los fotogramas de evidencia de un caso, CIFRADOS en el bucket (decisión D1;
  `STORED_IMAGES`), que se borran a los `FRAUD_EVIDENCE_RETENTION_DAYS` días.
- `AttackSignature`: la lista de bloqueo de artefactos de ataques CONFIRMADOS (solo huellas pHash, nunca datos
  personales; decisión D6), de una empresa o de toda la plataforma.
- `RiskSignalStat`: cuántos fraudes confirmados y falsos positivos dio cada señal en cada empresa (la línea base
  que ve el ADMIN antes de relajar o exigir una señal).
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    UniqueConstraint,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.db_schemas import AUTH, BIOMETRICS, CATALOG, OPS, TENANCY, WORKFORCE
from app.core.partitions import partitioned
from app.models.mixins import company_fk

_BIG_ID = BigInteger().with_variant(Integer, "sqlite")
_JSON = JSON().with_variant(JSONB(), "postgresql")
#: Estados de un caso que todavía esperan revisión (a lo más un caso así por sujeto).
ACTIVE_CASE = "status IN ('OPEN', 'IN_REVIEW')"


def _case_fk(table: str) -> ForeignKeyConstraint:
    return company_fk(table, "case_id", f"{OPS}.fraud_cases")


class CaptureTrace(Base):
    """Huella perceptual de una captura frontal de un intento (anti-reenvío perceptual, prototipo P2).

    El SHA-256 de `capture_fingerprints` solo detecta la MISMA imagen; aquí se guarda lo que sobrevive a una
    modificación mínima: el pHash de 64 bits del rostro alineado y del cuadro completo, y el embedding del rostro
    CIFRADO (una plantilla, como las de la galería: nunca la imagen). Una captura nueva del mismo empleado a lo
    más a `FACE_PHASH_MAX_DISTANCE` bits y con similitud ≥ `FACE_PERCEPTUAL_MIN_COSINE` respecto a una de OTRO
    intento es un reenvío modificado (señal REPLAY_PERCEPTUAL). Su retención la acota (`FACE_REPLAY_RETENTION_DAYS`,
    depuración por lotes): no se particiona (§3.3 del backend)."""

    __tablename__ = "capture_traces"
    __table_args__ = (
        # Las capturas recientes de un empleado (una consulta por intento), en el orden del índice; también sirve
        # a la FK compuesta del empleado (CASCADE).
        Index("ix_capture_traces_employee", "company_id", "employee_id", "created_at", "id"),
        # Depuración de las vencidas.
        Index("ix_capture_traces_created", "created_at"),
        company_fk("capture_traces", "employee_id", f"{WORKFORCE}.employees"),
        {"schema": BIOMETRICS},
    )

    id: Mapped[int] = mapped_column(_BIG_ID, primary_key=True)
    company_id: Mapped[int] = mapped_column(nullable=False)
    employee_id: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    #: pHash DCT de 64 bits (con signo, como BIGINT) del rostro alineado y del cuadro completo.
    face_phash: Mapped[int] = mapped_column(BigInteger, nullable=False)
    frame_phash: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: Embedding del rostro cifrado con DATA_ENCRYPTION_KEY y su dimensión.
    embedding_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    dimension: Mapped[int] = mapped_column(SmallInteger, nullable=False)


class PolicyChange(Base):
    """Un cambio de la política de verificación de una empresa: se inserta, nunca se edita (salvo su decisión).

    `changes`: `[{"field", "before", "after", "relaxes"}]`. Lo que relaja la seguridad (`relaxes`) queda
    PENDING hasta que OTRO ADMIN lo apruebe (se aplica entonces) o lo rechace; lo demás se aplica al momento
    (APPLIED). Quién lo pidió y quién lo decidió se guardan como su correo literal (como la cobranza)."""

    __tablename__ = "policy_changes"
    __table_args__ = (
        # Historial de la empresa, lo más reciente primero (también la FK de la empresa).
        Index("ix_policy_changes_company", "company_id", "created_at", "id"),
        {"schema": OPS},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey(f"{TENANCY}.companies.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), ForeignKey(f"{CATALOG}.policy_change_statuses.code"), nullable=False
    )
    relaxes: Mapped[bool] = mapped_column(Boolean, nullable=False)
    #: Nivel predefinido que se aplicó (catalog.policy_presets) o None si fue un ajuste.
    preset: Mapped[str | None] = mapped_column(String(30), ForeignKey(f"{CATALOG}.policy_presets.code"))
    changes: Mapped[list[dict[str, Any]]] = mapped_column(_JSON, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(500))
    #: Resumen de la simulación de los últimos días con la política nueva (si cambió el motor de riesgo).
    simulation: Mapped[dict[str, Any] | None] = mapped_column(_JSON)
    #: Sin índice: la tabla es pequeña (un renglón por cambio de política) y nadie borra cuentas de ADMIN seguido.
    requested_by_id: Mapped[int | None] = mapped_column(ForeignKey(f"{AUTH}.users.id", ondelete="SET NULL"))
    requested_by: Mapped[str] = mapped_column(String(255), nullable=False)
    decided_by_id: Mapped[int | None] = mapped_column(ForeignKey(f"{AUTH}.users.id", ondelete="SET NULL"))
    decided_by: Mapped[str | None] = mapped_column(String(255))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(String(500))


class RiskAssessment(Base):
    """La decisión del motor de riesgo sobre un intento facial (`risk_engine`).

    `reasons`: `[{"code", "points", "value", "threshold", "mode"}]` de TODAS las señales que se dispararon (también
    las que solo se miden): con ellas se simula otra política sobre lo ocurrido. `score` es el puntaje que decidió
    (solo señales obligatorias). Particionada por mes en `created_at` con la retención de las métricas."""

    __tablename__ = "risk_assessments"
    __table_args__ = (
        # Simulación de una política sobre los últimos días de una empresa (y la FK de la empresa).
        Index("ix_risk_assessments_company", "company_id", "created_at", "id"),
        CheckConstraint("fraud_label IN ('FRAUD', 'GENUINE')", name="fraud_label"),
        {"schema": OPS, **partitioned("created_at")},
    )

    id: Mapped[int] = mapped_column(_BIG_ID, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey(f"{TENANCY}.companies.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    #: El intento de la bitácora (sin FK: también está particionada); ella dice de quién fue.
    verification_log_id: Mapped[int | None] = mapped_column(_BIG_ID)
    score: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    tier: Mapped[str] = mapped_column(String(20), ForeignKey(f"{CATALOG}.risk_tiers.code"), nullable=False)
    action: Mapped[str] = mapped_column(String(20), ForeignKey(f"{CATALOG}.risk_actions.code"), nullable=False)
    reasons: Mapped[list[dict[str, Any]]] = mapped_column(_JSON, nullable=False)
    #: Huella de la configuración de riesgo con que se decidió y versión del motor.
    policy_version: Mapped[str] = mapped_column(String(16), nullable=False)
    engine: Mapped[str] = mapped_column(String(10), nullable=False)
    #: El intento superó un reto de "un paso más" (riesgo medio ya atendido).
    step_up: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    #: El motor falló y se aplicó la acción de respaldo de la política.
    fallback: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    #: Lo que decidió la revisión de su caso (FRAUD / GENUINE); None = sin revisar.
    fraud_label: Mapped[str | None] = mapped_column(String(20))


class FraudCase(Base):
    """Un caso de fraude: los intentos sospechosos o de riesgo alto de UN sujeto (el empleado o, si no se supo
    quién era, la cuenta que operó la cámara) mientras el caso sigue abierto. Lo revisa el ADMIN."""

    __tablename__ = "fraud_cases"
    __table_args__ = (
        UniqueConstraint("id", "company_id", name="uq_fraud_cases_id_company"),
        # A lo más un caso activo por sujeto: los intentos siguientes se suman a él (inserción atómica).
        Index(
            "uq_fraud_cases_active_subject",
            "company_id",
            "subject",
            unique=True,
            postgresql_where=text(ACTIVE_CASE),
            sqlite_where=text(ACTIVE_CASE),
        ),
        # Bandeja del ADMIN por estado, lo más reciente primero.
        Index("ix_fraud_cases_queue", "status", "last_attempt_at", "id"),
        # Todos los casos ("ALL"), lo más reciente primero, y la depuración de los decididos más viejos (medido con
        # volumen: sin él, la bandeja completa recorría la tabla y ordenaba).
        Index("ix_fraud_cases_recent", "last_attempt_at", "id"),
        # Los casos de una empresa (filtro del ADMIN) y la FK de la empresa.
        Index("ix_fraud_cases_company", "company_id", "last_attempt_at", "id"),
        # FKs con ON DELETE SET NULL hacia las cuentas (quién operó la cámara y quién decidió): sin ellos, depurar un
        # lote de cuentas de «Eliminados» recorría la tabla por cada cuenta (≈130 ms de los 451 ms del lote de 50,
        # migración 0072). Parciales: casi todos los casos no tienen operador y los abiertos no tienen quién decidió.
        Index(
            "ix_fraud_cases_actor_id",
            "actor_id",
            postgresql_where=text("actor_id IS NOT NULL"),
            sqlite_where=text("actor_id IS NOT NULL"),
        ),
        Index(
            "ix_fraud_cases_decided_by_id",
            "decided_by_id",
            postgresql_where=text("decided_by_id IS NOT NULL"),
            sqlite_where=text("decided_by_id IS NOT NULL"),
        ),
        company_fk("fraud_cases", "employee_id", f"{WORKFORCE}.employees"),
        {"schema": OPS},
    )

    id: Mapped[int] = mapped_column(_BIG_ID, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey(f"{TENANCY}.companies.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), ForeignKey(f"{CATALOG}.fraud_case_statuses.code"), nullable=False)
    kind: Mapped[str] = mapped_column(String(30), ForeignKey(f"{CATALOG}.fraud_kinds.code"), nullable=False)
    #: De quién es: "employee:<id>", "actor:<cuenta>" (un validador sin saber a quién identificaba) o "device:<huella>"
    #: (un dispositivo de la API pública de verificación, migración 0084).
    subject: Mapped[str] = mapped_column(String(40), nullable=False)
    employee_id: Mapped[int | None] = mapped_column()
    #: La cuenta que operó la cámara (un validador sin saber a quién identificaba); su índice es el de su FK.
    actor_id: Mapped[int | None] = mapped_column(ForeignKey(f"{AUTH}.users.id", ondelete="SET NULL"))
    #: Intentos sumados al caso (los primeros FRAUD_CASE_MAX_ATTEMPTS guardan su detalle).
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    #: Mayor puntaje de riesgo visto y su nivel (None si solo hubo rechazos de los candados).
    max_score: Mapped[int | None] = mapped_column(SmallInteger)
    tier: Mapped[str | None] = mapped_column(String(20), ForeignKey(f"{CATALOG}.risk_tiers.code"))
    #: El motivo que abrió el caso (motivo de la bitácora o señal del motor).
    reason: Mapped[str] = mapped_column(String(50), nullable=False)
    evidence: Mapped[int] = mapped_column(SmallInteger, default=0, server_default=text("0"), nullable=False)
    decided_by_id: Mapped[int | None] = mapped_column(ForeignKey(f"{AUTH}.users.id", ondelete="SET NULL"))
    decided_by: Mapped[str | None] = mapped_column(String(255))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(String(1000))


class FraudCaseAttempt(Base):
    """Un intento de un caso con la COPIA de lo que se midió (motivos, números y huellas pHash): el caso se
    revisa y aprende aunque la bitácora y las métricas ya hayan vencido."""

    __tablename__ = "fraud_case_attempts"
    __table_args__ = (
        Index("ix_fraud_case_attempts_case", "case_id", "id"),
        _case_fk("fraud_case_attempts"),
        {"schema": OPS},
    )

    id: Mapped[int] = mapped_column(_BIG_ID, primary_key=True)
    company_id: Mapped[int] = mapped_column(nullable=False)
    case_id: Mapped[int] = mapped_column(_BIG_ID, nullable=False)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Referencias al intento (tablas particionadas: sin FK; `attempted_at` acota su partición).
    verification_log_id: Mapped[int | None] = mapped_column(_BIG_ID)
    assessment_id: Mapped[int | None] = mapped_column(_BIG_ID)
    metric_id: Mapped[int | None] = mapped_column(_BIG_ID)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    #: Motivo de la bitácora si se rechazó (catalog.verification_reasons).
    reason: Mapped[str | None] = mapped_column(String(50))
    score: Mapped[int | None] = mapped_column(SmallInteger)
    action: Mapped[str | None] = mapped_column(String(20))
    signals: Mapped[list[dict[str, Any]]] = mapped_column(_JSON, nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    #: Huellas "pHash del rostro:pHash del cuadro" (hex) de sus capturas frontales: la lista de bloqueo.
    phashes: Mapped[list[str]] = mapped_column(_JSON, nullable=False)
    camera: Mapped[str | None] = mapped_column(String(120))
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(255))


class FraudCaseEvent(Base):
    """El historial de un caso (abierto, cambios de estado, notas, evidencia consultada...): solo se inserta."""

    __tablename__ = "fraud_case_events"
    __table_args__ = (
        Index("ix_fraud_case_events_case", "case_id", "id"),
        _case_fk("fraud_case_events"),
        {"schema": OPS},
    )

    id: Mapped[int] = mapped_column(_BIG_ID, primary_key=True)
    company_id: Mapped[int] = mapped_column(nullable=False)
    case_id: Mapped[int] = mapped_column(_BIG_ID, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    kind: Mapped[str] = mapped_column(String(30), ForeignKey(f"{CATALOG}.fraud_case_event_kinds.code"), nullable=False)
    #: Quién (correo literal); None = el sistema.
    actor: Mapped[str | None] = mapped_column(String(255))
    status_from: Mapped[str | None] = mapped_column(String(20))
    status_to: Mapped[str | None] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(String(1000))


class FraudEvidence(Base):
    """Un fotograma de evidencia de un caso: la REFERENCIA de su objeto cifrado en el bucket (`STORED_IMAGES`),
    nunca la imagen. Excepción documentada a "el ADMIN no ve biometría" (decisión D1): solo intentos sospechosos,
    solo el ADMIN por la API y se borra a los `FRAUD_EVIDENCE_RETENTION_DAYS` días."""

    __tablename__ = "fraud_evidence"
    __table_args__ = (
        Index("ix_fraud_evidence_case", "case_id", "id"),
        # Depuración de lo vencido (encola su objeto y borra la fila).
        Index("ix_fraud_evidence_created", "created_at"),
        # La evidencia de un empleado que se elimina (regla 13: sus objetos salen del bucket y sus filas se borran,
        # `release_employee_images` y `erase_employee`): sin él, cada eliminación recorría la tabla (3.6 ms con 60 000
        # filas y creciendo con su retención; migración 0072). Parcial: la de un intento sin empleado no la usa.
        Index(
            "ix_fraud_evidence_employee",
            "company_id",
            "employee_id",
            postgresql_where=text("employee_id IS NOT NULL"),
            sqlite_where=text("employee_id IS NOT NULL"),
        ),
        _case_fk("fraud_evidence"),
        # La hoja de la ráfaga (BURST, migración 0066) entra por el mismo camino (decisión D1), nunca por otro.
        CheckConstraint("kind IN ('FRONTAL', 'STEP', 'FLASH', 'BURST')", name="kind"),
        {"schema": OPS},
    )

    id: Mapped[int] = mapped_column(_BIG_ID, primary_key=True)
    company_id: Mapped[int] = mapped_column(nullable=False)
    case_id: Mapped[int] = mapped_column(_BIG_ID, nullable=False)
    #: Del empleado del caso (si se supo): sus objetos salen del bucket si se borra al empleado.
    employee_id: Mapped[int | None] = mapped_column()
    verification_log_id: Mapped[int | None] = mapped_column(_BIG_ID)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    #: Frontal, movimiento del reto, color del destello u hoja de la ráfaga, y su orden en el intento.
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    #: Parte aleatoria del nombre del objeto (se sube ANTES de insertar la fila).
    uid: Mapped[str] = mapped_column(String(32), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(30))
    object_name: Mapped[str | None] = mapped_column(String(300))
    byte_size: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String(64))
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AttackSignature(Base):
    """Lista de bloqueo: huellas de artefactos de ataques CONFIRMADOS por el ADMIN (decisión D6: solo hashes, nunca
    datos personales). `company_id` es su alcance (None = toda la plataforma: se confirmó en
    `ATTACK_SIGNATURE_PLATFORM_COMPANIES` empresas). `allowed`: un falso positivo la liberó (lista de permitidas: no
    vuelve a bloquearse). Cada proceso la lee completa (acotada) y la conserva `ATTACK_SIGNATURE_CACHE_SECONDS`."""

    __tablename__ = "attack_signatures"
    __table_args__ = (
        UniqueConstraint(
            "kind", "value", "company_id", name="uq_attack_signatures_kind", postgresql_nulls_not_distinct=True
        ),
        # Carga de las vigentes y depuración de las vencidas.
        Index("ix_attack_signatures_expires", "expires_at"),
        CheckConstraint("kind IN ('CAPTURE_PHASH')", name="kind"),
        {"schema": OPS},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    #: CAPTURE_PHASH: "pHash del rostro:pHash del cuadro" (hex) de una captura de ataque.
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    value: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Sin índice: pocas filas y borrar una empresa es raro.
    company_id: Mapped[int | None] = mapped_column(ForeignKey(f"{TENANCY}.companies.id", ondelete="CASCADE"))
    #: Caso que la originó (referencia sin FK: puede ser de otra empresa).
    case_id: Mapped[int | None] = mapped_column(_BIG_ID)
    companies: Mapped[int] = mapped_column(SmallInteger, default=1, server_default=text("1"), nullable=False)
    hits: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    allowed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_hit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RiskSignalStat(Base):
    """Línea base de cada señal en una empresa: cuántos casos con esa señal resultaron fraude confirmado y cuántos
    falsos positivos. La alimentan las revisiones del ADMIN; la ve en la política antes de exigir o relajar."""

    __tablename__ = "risk_signal_stats"
    __table_args__ = ({"schema": OPS},)

    company_id: Mapped[int] = mapped_column(ForeignKey(f"{TENANCY}.companies.id", ondelete="CASCADE"), primary_key=True)
    signal: Mapped[str] = mapped_column(String(30), ForeignKey(f"{CATALOG}.risk_signals.code"), primary_key=True)
    confirmed: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    false_positive: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
