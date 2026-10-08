from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.db_schemas import OPS, TENANCY
from app.core.partitions import partitioned


class FaceAttemptMetric(Base):
    """Los números de cada intento facial, para que la plataforma se mida y se ajuste sola.

    Solo números (probabilidades, cuánto se movió la persona, respuesta al destello, tiempos): nunca
    imágenes ni plantillas. Con ellos la autocalibración (`face_security`) endurece los umbrales y se
    detectan ataques contra una empresa. Desde la migración 0062 cada fila se enlaza con su intento de la
    bitácora (`verification_log_id`, decisión D7 del dueño del producto): así un caso de fraude confirmado
    no "envenena" la calibración (`fraud_label`) y se puede simular una política sobre lo ocurrido.
    Particionada por mes en `created_at` (`app/core/partitions.py`): lo que pasa de
    FACE_METRICS_RETENTION_DAYS días sale con su partición.
    """

    __tablename__ = "face_attempt_metrics"
    __table_args__ = (
        # Ataques recientes contra una empresa (refuerzo automático, en CADA reto facial): el motivo antes
        # de la fecha lleva directo a los intentos sospechosos de la ventana, aunque la empresa tenga miles
        # de intentos exitosos en ella (al entrar el turno). También sirve a la FK de la empresa
        # (migración 0047).
        Index("ix_face_attempt_metrics_company_reason", "company_id", "reason", "created_at"),
        # Autocalibración (intentos exitosos recientes) y depuración por antigüedad.
        Index("ix_face_attempt_metrics_created", "created_at", "id"),
        # Sin CHECK en `fraud_label` (migración 0062): validarlo recorrería todas las particiones de la tabla más
        # grande; la etiqueta solo la escribe la revisión de un caso (`MetricLabelRepository`).
        {"schema": OPS, **partitioned("created_at")},
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey(f"{TENANCY}.companies.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    #: Motivo del rechazo (el mismo de la bitácora); None si fue exitoso.
    reason: Mapped[str | None] = mapped_column(String(50))
    #: Movimientos del reto y modo del destello con que se hizo el intento.
    steps: Mapped[int | None] = mapped_column(SmallInteger)
    flash_mode: Mapped[str | None] = mapped_column(String(20))
    #: Segundos desde que se emitió el reto hasta que llegó la respuesta.
    response_seconds: Mapped[float | None] = mapped_column(Float)
    #: Anti-spoofing: probabilidad de rostro real (mínima y media de las frontales; mínima de los pasos).
    frontal_real_min: Mapped[float | None] = mapped_column(Float)
    frontal_real_mean: Mapped[float | None] = mapped_column(Float)
    step_real_min: Mapped[float | None] = mapped_column(Float)
    #: Lo mínimo que se movió en cada tipo de paso (giro, mirar arriba/abajo, acercarse).
    yaw_min: Mapped[float | None] = mapped_column(Float)
    pitch_min: Mapped[float | None] = mapped_column(Float)
    closer_min: Mapped[float | None] = mapped_column(Float)
    #: Respuesta al destello (photometry.FlashResponse).
    flash_score: Mapped[float | None] = mapped_column(Float)
    flash_magnitude: Mapped[float | None] = mapped_column(Float)
    flash_background: Mapped[float | None] = mapped_column(Float)
    #: Cociente rostro/fondo de la respuesta al destello (≈ 1: pantalla o papel; un rostro real responde más).
    flash_ratio: Mapped[float | None] = mapped_column(Float)
    #: Calidad y luz medias de las frontales.
    quality_mean: Mapped[float | None] = mapped_column(Float)
    brightness_mean: Mapped[float | None] = mapped_column(Float)
    #: Protocolo de captura (antifraude 2a, migración 0066): recortes de la ráfaga analizados, su micromovimiento
    #: (niveles de gris entre recortes seguidos), el pulso por video (SNR en dB; solo se mide), el moiré y el cociente
    #: de ruido rostro/fondo de las frontales, el paralaje de los giros y la respuesta más lenta del destello dictado
    #: por el servidor (ms; None si no fue dictado).
    burst_frames: Mapped[int | None] = mapped_column(SmallInteger)
    burst_motion: Mapped[float | None] = mapped_column(Float)
    pulse_snr: Mapped[float | None] = mapped_column(Float)
    moire: Mapped[float | None] = mapped_column(Float)
    noise_ratio: Mapped[float | None] = mapped_column(Float)
    parallax: Mapped[float | None] = mapped_column(Float)
    flash_pace_ms: Mapped[float | None] = mapped_column(Float)
    #: Consenso de identidad de la ráfaga (decisión del dueño, 2026-10-06): mediana del parecido de sus mejores recortes
    #: quietos con las muestras de la persona (solo endurece: bajo lo exigido menos el margen, "Rostro no reconocido").
    burst_consensus: Mapped[float | None] = mapped_column(Float)
    #: El intento de la bitácora (decisión D7): enlaza los números con su caso de fraude y su evaluación de
    #: riesgo. Sin llave foránea: la bitácora también está particionada (mismo plazo).
    verification_log_id: Mapped[int | None] = mapped_column(BigInteger().with_variant(Integer, "sqlite"))
    #: Lo que decidió la revisión de un caso: FRAUD (no "envenena" la calibración de las personas reales) o
    #: GENUINE (falso positivo: un genuino difícil). None = sin revisar.
    fraud_label: Mapped[str | None] = mapped_column(String(20))
    #: Plataforma del navegador que hizo el intento (migración 0081, deriva de señales): IOS_SAFARI, ANDROID_CHROME,
    #: DESKTOP u OTHER (`app/core/devices.py`, `platform_of`). Una categoría gruesa, nunca el User-Agent ni la persona;
    #: None en los intentos anteriores a la migración.
    platform: Mapped[str | None] = mapped_column(String(20))


class SecurityThreshold(Base):
    """Un umbral que la plataforma ajustó sola con lo medido (autocalibración, `face_security`).

    Solo endurece: el valor nunca queda por debajo del de la configuración (el piso seguro) ni por
    encima de su tope (para no bloquear a las personas reales). Una fila por umbral.
    """

    __tablename__ = "security_thresholds"
    __table_args__ = ({"schema": OPS},)

    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    #: Intentos exitosos medidos con que se calculó.
    samples: Mapped[int] = mapped_column(Integer, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
