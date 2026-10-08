from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
    String,
    false,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
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
        # Cortes del puntaje de riesgo en orden (medio < alto < crítico) dentro de 1..100.
        CheckConstraint(
            "risk_medium_score BETWEEN 1 AND 100 AND risk_high_score BETWEEN 1 AND 100 "
            "AND risk_critical_score BETWEEN 1 AND 100 "
            "AND risk_medium_score < risk_high_score AND risk_high_score < risk_critical_score",
            name="risk_scores",
        ),
        # Si el motor falla solo se permite (y se avisa) o se pide un paso más: nunca se niega ni se deja en
        # revisión a ciegas.
        CheckConstraint("risk_fallback_action IN ('ALLOW', 'ALERT', 'STEP_UP')", name="risk_fallback_action"),
        {"schema": TENANCY},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey(f"{TENANCY}.companies.id", ondelete="CASCADE"), unique=True, index=True, nullable=False
    )
    #: Lentes: apagado por omisión en toda empresa (decisión del dueño, 2026-10-07, migración 0082); el ADMIN lo
    #: enciende por empresa como cualquier otra regla y entonces un rostro con lentes no se registra ni se verifica.
    block_glasses: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
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
    #: Destello de colores en la pantalla (catalog.flash_modes). RETIRADO de la experiencia por decisión del dueño del
    #: producto (2026-10-06, migración 0080): nace y se queda en OFF en toda empresa (la app no lo ofrece); el código de
    #: la fotometría sigue disponible por si se reconsidera.
    flash_liveness: Mapped[str] = mapped_column(
        String(20),
        ForeignKey(f"{CATALOG}.flash_modes.code"),
        default="OFF",
        server_default="OFF",
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
    # --- Antifraude (migración 0062; docs/rd/antifraude-identidad.md) ---
    #: Un validador en modo QR registra asistencia con el QR solo (sin rostro). Decisión D4: apagado en las
    #: empresas nuevas (un QR se puede reenviar a un cómplice); las que ya existían conservan su comportamiento.
    qr_only_attendance: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    #: Nivel de SOSPECHA de duplicado al registrarse (más sensible que el de aceptación): solo marca el registro
    #: para la revisión con los empleados más parecidos, nunca bloquea (catalog.confidence_levels.value).
    duplicate_confidence: Mapped[float] = mapped_column(
        Numeric(7, 5, asdecimal=False),
        ForeignKey(f"{CATALOG}.confidence_levels.value"),
        default=0.99,
        server_default=text("0.99"),
        nullable=False,
    )
    #: Dispositivo del empleado (decisión D2; catalog.employee_device_modes; `app/services/employee_devices.py`).
    employee_device_mode: Mapped[str] = mapped_column(
        String(30),
        ForeignKey(f"{CATALOG}.employee_device_modes.code"),
        default="OBSERVE",
        server_default="OBSERVE",
        nullable=False,
    )
    #: Último nivel predefinido aplicado (catalog.policy_presets); None = ajustes a la medida.
    preset: Mapped[str | None] = mapped_column(String(30), ForeignKey(f"{CATALOG}.policy_presets.code"))
    #: Motor de riesgo (risk_engine): puntaje explicable de cada intento y su acción por nivel.
    risk_engine: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)
    #: Cortes del puntaje (0-100): desde aquí el nivel es medio, alto o crítico.
    risk_medium_score: Mapped[int] = mapped_column(SmallInteger, default=30, server_default=text("30"), nullable=False)
    risk_high_score: Mapped[int] = mapped_column(SmallInteger, default=60, server_default=text("60"), nullable=False)
    risk_critical_score: Mapped[int] = mapped_column(
        SmallInteger, default=80, server_default=text("80"), nullable=False
    )
    #: Acción de cada nivel (catalog.risk_actions; decisión D3: medio = un paso más, alto = en revisión).
    risk_medium_action: Mapped[str] = mapped_column(
        String(30),
        ForeignKey(f"{CATALOG}.risk_actions.code"),
        default="STEP_UP",
        server_default="STEP_UP",
        nullable=False,
    )
    risk_high_action: Mapped[str] = mapped_column(
        String(30),
        ForeignKey(f"{CATALOG}.risk_actions.code"),
        default="REVIEW",
        server_default="REVIEW",
        nullable=False,
    )
    risk_critical_action: Mapped[str] = mapped_column(
        String(30), ForeignKey(f"{CATALOG}.risk_actions.code"), default="DENY", server_default="DENY", nullable=False
    )
    #: Qué hacer si el motor falla (BD lenta, dato corrupto): permitir, permitir y avisar o un paso más.
    risk_fallback_action: Mapped[str] = mapped_column(
        String(30),
        ForeignKey(f"{CATALOG}.risk_actions.code"),
        default="ALLOW",
        server_default="ALLOW",
        nullable=False,
    )
    #: Ajustes de cada señal sobre los de la plataforma (catalog.risk_signals): {código: {"mode", "points"}}.
    risk_signals: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), default=dict, server_default=text("'{}'"), nullable=False
    )
    #: Guardar fotogramas de evidencia de los intentos sospechosos (decisión D1): cifrados en el bucket.
    fraud_evidence: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)
    # --- Protocolo de captura de frontera (antifraude 2a, migración 0066): nacen midiendo, nunca bloquean solos ---
    #: Destello dictado por el servidor: los colores se revelan uno por uno por el canal en vivo, con tiempo por color
    #: (sin el canal, la app usa el destello de siempre y eso es una señal medida: FLASH_UNPACED).
    #: Retirado con el destello (decisión del dueño, 2026-10-06, migración 0080): nace apagado.
    flash_paced: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    #: Ráfaga corta de recortes del rostro con las capturas (continuidad, micromovimiento y pulso; decisión D11).
    capture_burst: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)
    #: Verificación por voz y video del registro facial (decisión del dueño, 2026-10-06; migración 0079): tras las fotos
    #: válidas, el empleado responde en video tres preguntas al azar sobre sus propios datos y el servidor compara su
    #: voz (transcrita aquí) y su rostro con lo registrado; la empresa revisa el video al validar. Apagarla relaja la
    #: seguridad (regla de dos personas).
    voice_verification: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)
    #: Guía por audio del registro facial (decisión del dueño, 2026-10-08): encendida, la app DICTA las indicaciones con
    #: voz («quédate quieto», «voltea a la derecha»...). Apagada por omisión; es una ayuda, no un candado de seguridad,
    #: así que no pasa por la regla de dos personas. La síntesis es del navegador (nada sale del servidor, regla 13).
    voice_guidance_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    #: Voz con que se dicta la guía por audio (código activo de catalog.voice_profiles; lo valida el servicio, como los
    #: modos de señal —sin llave foránea—; la app mapea el código a los parámetros de la voz).
    voice_profile: Mapped[str] = mapped_column(
        String(30), default="FEMALE_WARM", server_default="FEMALE_WARM", nullable=False
    )
    # --- Presencia (antifraude 2b, migración 0070): cada prueba con el modo de una señal (catalog.signal_modes):
    # apagada, solo medir (señal del motor de riesgo, nace así) u obligatoria (sin ella, se rechaza con su código) ---
    #: Firma por petición: cada identificación de un validador lleva la firma de la llave del dispositivo con que inició
    #: sesión sobre un reto del servidor (un token robado no identifica a nadie sin ese dispositivo).
    validator_signing: Mapped[str] = mapped_column(
        String(20),
        ForeignKey(f"{CATALOG}.signal_modes.code"),
        default="OBSERVE",
        server_default="OBSERVE",
        nullable=False,
    )
    #: Ubicación en cada identificación de los validadores que requieren ubicación (no solo al iniciar sesión).
    validator_location: Mapped[str] = mapped_column(
        String(20),
        ForeignKey(f"{CATALOG}.signal_modes.code"),
        default="OBSERVE",
        server_default="OBSERVE",
        nullable=False,
    )
    #: Código de sitio en la entrada y la salida, en los sitios que lo activen (decisión D9).
    site_codes: Mapped[str] = mapped_column(
        String(20),
        ForeignKey(f"{CATALOG}.signal_modes.code"),
        default="OBSERVE",
        server_default="OBSERVE",
        nullable=False,
    )
    #: Ubicación de CADA verificación de identidad (empleado, validador y API pública; decisión del dueño, 2026-10-07,
    #: migración 0085): modo de `signal_modes`. OFF no la pide; OBSERVE la registra (la empresa ve dónde se hizo cada
    #: verificación en el mapa); ENFORCE la exige (sin ubicación válida, el servidor no completa la verificación: 422
    #: LOCATION_REQUIRED/LOCATION_INVALID antes del motor). Por omisión OBSERVE (registra sin dejar a nadie afuera; el
    #: ADMIN la sube a ENFORCE a propósito). Distinto de `validator_location` (prueba de presencia del validador, 2b).
    verification_location: Mapped[str] = mapped_column(
        String(20),
        ForeignKey(f"{CATALOG}.signal_modes.code"),
        default="OBSERVE",
        server_default="OBSERVE",
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    updated_by_id: Mapped[int | None] = mapped_column(ForeignKey(f"{AUTH}.users.id", ondelete="SET NULL"))
