from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
    String,
    func,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.db_schemas import AUTH, CATALOG, TENANCY


class VerificationPolicy(Base):
    """Política de verificación de cada empresa (una fila por empresa), editable por COMPANY.

    Define qué se exige en cada captura facial y qué métodos de identificación se permiten.
    """

    __tablename__ = "verification_policy"
    __table_args__ = (
        # FK con ON DELETE SET NULL (quién la cambió): evita recorrer la tabla al borrar un usuario.
        Index(
            "ix_verification_policy_updated_by_id",
            "updated_by_id",
            postgresql_where=text("updated_by_id IS NOT NULL"),
            sqlite_where=text("updated_by_id IS NOT NULL"),
        ),
        CheckConstraint("liveness_steps BETWEEN 1 AND 3", name="liveness_steps"),
        CheckConstraint("liveness_timeout_seconds BETWEEN 20 AND 180", name="liveness_timeout_seconds"),
        CheckConstraint("lockout_max_failures BETWEEN 3 AND 20", name="lockout_max_failures"),
        CheckConstraint("lockout_minutes BETWEEN 1 AND 1440", name="lockout_minutes"),
        CheckConstraint("qr_lifetime_seconds BETWEEN 15 AND 300", name="qr_lifetime_seconds"),
        CheckConstraint("min_capture_quality BETWEEN 0 AND 0.9", name="min_capture_quality"),
        CheckConstraint("max_location_accuracy_m BETWEEN 10 AND 1000", name="max_location_accuracy_m"),
        CheckConstraint("max_travel_kmh BETWEEN 30 AND 1000", name="max_travel_kmh"),
        {"schema": TENANCY},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey(f"{TENANCY}.companies.id", ondelete="CASCADE"), unique=True, index=True, nullable=False
    )
    block_glasses: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    block_headwear: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    block_mask: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    liveness_challenge: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    anti_spoofing: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    qr_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Confianza mínima (probabilidad de que sea la misma persona) para aceptar una verificación facial:
    # uno de los niveles del catálogo (catalog.confidence_levels.value).
    min_confidence: Mapped[float] = mapped_column(
        Numeric(7, 5, asdecimal=False),
        ForeignKey(f"{CATALOG}.confidence_levels.value"),
        default=0.99999,
        server_default=text("0.99999"),
        nullable=False,
    )
    # Confianza mínima al IDENTIFICAR entre toda la plantilla (1:N, validadores). Buscar entre todos
    # multiplica las oportunidades de una coincidencia falsa: se puede exigir más que al verificar a una
    # persona (1:1). Nunca es menor que `min_confidence`.
    identify_confidence: Mapped[float] = mapped_column(
        Numeric(7, 5, asdecimal=False),
        ForeignKey(f"{CATALOG}.confidence_levels.value"),
        default=0.99999,
        server_default=text("0.99999"),
        nullable=False,
    )
    # Calidad mínima de cada captura (0 = sin mínimo): detección, nitidez y luz combinadas.
    min_capture_quality: Mapped[float] = mapped_column(
        Numeric(3, 2, asdecimal=False), default=0.4, server_default=text("0.4"), nullable=False
    )
    # --- Asistencia por turno (registros con ubicación) ---
    # Precisión mínima de la ubicación (m): una lectura menos precisa no prueba dónde está la persona.
    max_location_accuracy_m: Mapped[int] = mapped_column(
        SmallInteger, default=100, server_default=text("100"), nullable=False
    )
    # Viaje imposible: dos registros más lejos de lo que se puede viajar en el tiempo entre ellos
    # (ubicación falsificada o una cuenta compartida).
    detect_impossible_travel: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)
    max_travel_kmh: Mapped[int] = mapped_column(SmallInteger, default=200, server_default=text("200"), nullable=False)
    # Los validadores de identidad solo operan desde una tableta o un teléfono (no computadoras).
    validator_mobile_only: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)

    # --- Candados contra engaños: cada empresa decide (todos activos por defecto) ---
    #: Sensibilidad del anti-spoofing (catalog.antispoof_levels).
    anti_spoofing_level: Mapped[str] = mapped_column(
        String(30),
        ForeignKey(f"{CATALOG}.antispoof_levels.code"),
        default="STANDARD",
        server_default="STANDARD",
        nullable=False,
    )
    #: Movimientos aleatorios de la prueba de vida (1 a 3): con más, un video grabado o generado de
    #: antemano tiene que acertar una secuencia más larga.
    liveness_steps: Mapped[int] = mapped_column(SmallInteger, default=2, server_default=text("2"), nullable=False)
    #: Segundos para responder el reto completo: menos tiempo deja menos margen para fabricar la respuesta.
    liveness_timeout_seconds: Mapped[int] = mapped_column(
        SmallInteger, default=60, server_default=text("60"), nullable=False
    )
    #: Destello de colores en la pantalla (catalog.flash_modes): apagado, solo medir u obligatorio.
    flash_liveness: Mapped[str] = mapped_column(
        String(20),
        ForeignKey(f"{CATALOG}.flash_modes.code"),
        default="OBSERVE",
        server_default="OBSERVE",
        nullable=False,
    )
    block_virtual_cameras: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)
    reject_foreign_images: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)
    detect_static_captures: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)
    detect_replays: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)
    check_capture_continuity: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)
    enforce_human_timing: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)
    detect_duplicate_faces: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)
    lockout_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)
    #: La galería de cada empleado aprende de sus identificaciones seguras (face_learning).
    adaptive_learning: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)
    #: Cada dispositivo de un validador debe autorizarlo la empresa antes de operar.
    validator_device_approval: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=False
    )
    lockout_max_failures: Mapped[int] = mapped_column(SmallInteger, default=5, server_default=text("5"), nullable=False)
    lockout_minutes: Mapped[int] = mapped_column(SmallInteger, default=15, server_default=text("15"), nullable=False)
    #: Segundos que vive cada QR dinámico del empleado antes de renovarse solo.
    qr_lifetime_seconds: Mapped[int] = mapped_column(
        SmallInteger, default=30, server_default=text("30"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    updated_by_id: Mapped[int | None] = mapped_column(ForeignKey(f"{AUTH}.users.id", ondelete="SET NULL"))
