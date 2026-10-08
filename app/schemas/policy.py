from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import settings
from app.i18n import StoredText
from app.schemas.common import Page


class VerificationPolicyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    block_glasses: bool = Field(
        description="Exigir retirar lentes (incluye lentes de sol). Apagado por omisión en toda empresa desde el "
        "2026-10-07 (decisión del dueño); encendido, un rostro con lentes no se registra ni se verifica"
    )
    block_headwear: bool = Field(description="Exigir retirar gorra, sombrero o visera (salvo empleados exentos)")
    block_mask: bool = Field(description="Exigir retirar cubrebocas")
    liveness_challenge: bool = Field(description="Prueba de vida: girar la cabeza en una dirección aleatoria")
    anti_spoofing: bool = Field(description="Detectar fotos impresas, pantallas y videos (anti-spoofing pasivo)")
    qr_enabled: bool = Field(description="Permitir identificarse con el código QR")
    min_confidence: float = Field(
        description="Confianza mínima para aceptar el reconocimiento facial (catalog.confidence_levels)"
    )
    validator_mobile_only: bool = Field(
        default=True, description="Los validadores de identidad solo operan desde una tableta o un teléfono"
    )
    identify_confidence: float = Field(
        default=0.99999,
        description="Confianza mínima al identificar entre toda la plantilla (1:N, validadores); nunca menor "
        "que min_confidence",
    )
    min_capture_quality: float = Field(
        default=0.4, description="Calidad mínima de cada captura (0 = sin mínimo): detección, nitidez y luz"
    )
    max_location_accuracy_m: int = Field(
        default=100, description="Asistencia: precisión mínima (m) de la ubicación de cada registro"
    )
    detect_impossible_travel: bool = Field(
        default=True, description="Asistencia: rechazar registros más lejos de lo que se puede viajar desde el anterior"
    )
    max_travel_kmh: int = Field(default=200, description="Velocidad máxima creíble entre dos registros (km/h)")
    verification_location: str = Field(
        default="OBSERVE",
        description="Ubicación de cada verificación de identidad (empleado, validador y API; catalog.signal_modes): "
        "OFF no la pide, OBSERVE la registra (mapa de «Verificaciones») y ENFORCE la exige. La lee la empresa para "
        "saber si enviarla; la configura el ADMIN. Distinta de validator_location (prueba de presencia del validador)",
    )
    # --- Candados contra engaños (cada uno se puede desactivar) ---
    anti_spoofing_level: str = Field(
        default="STANDARD", description="Sensibilidad del anti-spoofing (catalog.antispoof_levels)"
    )
    liveness_steps: int = Field(
        default=2,
        description="Movimientos aleatorios de la prueba de vida (1 a 3: girar, mirar arriba o abajo, acercarse)",
    )
    liveness_timeout_seconds: int = Field(default=60, description="Segundos para responder el reto completo")
    flash_liveness: str = Field(
        default="OFF",
        description="Destello de colores en la pantalla (catalog.flash_modes): OFF, OBSERVE (solo medir) o ENFORCE. "
        "Retirado de la experiencia por decisión del dueño (2026-10-06): OFF en toda empresa",
    )
    block_virtual_cameras: bool = Field(
        default=True, description="Rechazar cámaras virtuales (programas que inyectan video)"
    )
    reject_foreign_images: bool = Field(
        default=True, description="Rechazar imágenes con metadatos de cámara o de edición (no son capturas en vivo)"
    )
    detect_static_captures: bool = Field(default=True, description="Rechazar capturas idénticas (una foto fija)")
    voice_verification: bool = Field(
        default=True,
        description="Registro facial (decisión del dueño, 2026-10-06): tras las fotos, tres preguntas en video sobre "
        "los datos del empleado (voz y rostro comparados en el servidor; la empresa revisa el video al validar)",
    )
    voice_guidance_enabled: bool = Field(
        default=False,
        description="Guía por audio del registro facial (decisión del dueño, 2026-10-08): la app dicta las "
        "indicaciones con voz; la síntesis es del navegador",
    )
    voice_profile: str = Field(default="FEMALE_WARM", description="Voz de la guía por audio (catalog.voice_profiles)")
    detect_replays: bool = Field(default=True, description="Rechazar capturas que ya se habían recibido (reenvío)")
    check_capture_continuity: bool = Field(
        default=True, description="Exigir que todas las capturas sean de la misma toma (cámara, encuadre y luz)"
    )
    enforce_human_timing: bool = Field(
        default=True, description="Rechazar respuestas al reto más rápidas que una persona"
    )
    detect_duplicate_faces: bool = Field(
        default=True, description="Detectar el mismo rostro registrado en otro empleado de la empresa"
    )
    lockout_enabled: bool = Field(default=True, description="Bloquear temporalmente tras intentos fallidos seguidos")
    lockout_max_failures: int = Field(default=5, description="Intentos fallidos seguidos que bloquean")
    lockout_minutes: int = Field(default=15, description="Minutos de bloqueo")
    validator_device_approval: bool = Field(
        default=True, description="Cada dispositivo de un validador debe autorizarlo la empresa antes de operar"
    )
    qr_lifetime_seconds: int = Field(
        default=30, description="Segundos que vive cada QR dinámico del empleado antes de renovarse solo"
    )
    adaptive_learning: bool = Field(
        default=True,
        description=(
            "Aprendizaje continuo: cada identificación segura (prueba de vida y confianza holgada) enseña a la "
            "galería del empleado; las muestras del registro aprobado nunca se reemplazan"
        ),
    )
    #: Nombres de cámaras virtuales que no se aceptan (la webapp avisa antes de capturar).
    blocked_cameras: list[str] = Field(
        default_factory=lambda: list(settings.FACE_BLOCKED_CAMERAS),
        description="Cámaras virtuales rechazadas (por nombre, palabra completa)",
    )
    #: Un validador en modo QR registra asistencia con el QR solo (decisión D4: apagado en las empresas nuevas).
    qr_only_attendance: bool = Field(
        default=False, description="Un validador en modo QR registra asistencia con el QR solo (sin rostro)"
    )
    updated_at: datetime | None = None
    updated_by: str | None = None


class RiskSignalSettingRead(BaseModel):
    """Una señal del motor de riesgo en esta empresa: la de la plataforma y la vigente, con su línea base."""

    code: str
    name: str
    description: str | None = None
    #: Tipo de fraude que sugiere (familia: su suma tiene tope).
    kind: str
    #: Regla dura (al ser obligatoria niega sin importar el puntaje) y si la informa el dispositivo.
    hard: bool
    client: bool
    default_points: int
    default_mode: str
    points: int
    mode: str
    #: Casos de esta empresa con esta señal confirmados como fraude y descartados como falso positivo.
    confirmed: int = 0
    false_positive: int = 0
    #: Solo se mide: nunca puede exigirse (p. ej. el pulso por video hasta que el dueño lo calibre).
    measure_only: bool = False


class AdminPolicyRead(VerificationPolicyRead):
    """La política completa que configura el ADMIN: además de lo que leen la empresa y su personal, el motor de
    riesgo y los controles antifraude (nunca viajan a la empresa: no se le enseña al atacante qué se mide)."""

    duplicate_confidence: float = Field(description="Nivel de sospecha de duplicado al registrarse (solo marca)")
    employee_device_mode: str = Field(description="Dispositivo del empleado (catalog.employee_device_modes, D2)")
    preset: str | None = Field(default=None, description="Último nivel predefinido aplicado (None = a la medida)")
    risk_engine: bool
    risk_medium_score: int
    risk_high_score: int
    risk_critical_score: int
    risk_medium_action: str
    risk_high_action: str
    risk_critical_action: str
    risk_fallback_action: str
    fraud_evidence: bool = Field(description="Guardar fotogramas de evidencia de los intentos sospechosos (D1)")
    flash_paced: bool = Field(description="Destello dictado por el servidor, color por color (antifraude 2a)")
    capture_burst: bool = Field(description="Ráfaga corta de recortes del rostro con las capturas (antifraude 2a)")
    validator_signing: str = Field(
        default="OBSERVE", description="Firma por petición del dispositivo del validador (catalog.signal_modes, 2b)"
    )
    validator_location: str = Field(
        default="OBSERVE", description="Ubicación en cada identificación de los validadores que la requieren (2b)"
    )
    site_codes: str = Field(
        default="OBSERVE", description="Código de sitio en la entrada y la salida, en los sitios que lo activen (2b)"
    )
    risk_signals: list[RiskSignalSettingRead]
    #: Cambios que relajan la seguridad y esperan la aprobación de otro ADMIN.
    pending_changes: int = 0
    #: La regla de dos personas está activa en la plataforma (POLICY_TWO_PERSON_RULE).
    two_person_rule: bool = True


class RiskSignalUpdate(BaseModel):
    """El ajuste de una señal en la empresa (lo omitido queda como estaba)."""

    mode: str | None = Field(default=None, max_length=30)
    points: int | None = Field(default=None, ge=0, le=100)


class VerificationPolicyUpdate(BaseModel):
    """Actualización parcial: solo se modifican los campos enviados."""

    block_glasses: bool | None = None
    block_headwear: bool | None = None
    block_mask: bool | None = None
    liveness_challenge: bool | None = None
    anti_spoofing: bool | None = None
    qr_enabled: bool | None = None
    validator_mobile_only: bool | None = None
    #: Uno de los niveles activos de catalog.confidence_levels (lo valida el servicio).
    min_confidence: float | None = None
    identify_confidence: float | None = None
    min_capture_quality: float | None = Field(default=None, ge=0, le=0.9)
    max_location_accuracy_m: int | None = Field(default=None, ge=10, le=1000)
    detect_impossible_travel: bool | None = None
    max_travel_kmh: int | None = Field(default=None, ge=30, le=1000)
    #: Uno de los niveles activos de catalog.antispoof_levels (lo valida el servicio).
    anti_spoofing_level: str | None = Field(default=None, max_length=30)
    liveness_steps: int | None = Field(default=None, ge=1, le=3)
    liveness_timeout_seconds: int | None = Field(default=None, ge=20, le=180)
    #: Uno de los modos activos de catalog.flash_modes (lo valida el servicio).
    flash_liveness: str | None = Field(default=None, max_length=20)
    block_virtual_cameras: bool | None = None
    reject_foreign_images: bool | None = None
    detect_static_captures: bool | None = None
    detect_replays: bool | None = None
    check_capture_continuity: bool | None = None
    enforce_human_timing: bool | None = None
    detect_duplicate_faces: bool | None = None
    lockout_enabled: bool | None = None
    lockout_max_failures: int | None = Field(default=None, ge=3, le=20)
    lockout_minutes: int | None = Field(default=None, ge=1, le=1440)
    validator_device_approval: bool | None = None
    qr_lifetime_seconds: int | None = Field(default=None, ge=15, le=300)
    adaptive_learning: bool | None = None
    # --- Antifraude ---
    qr_only_attendance: bool | None = None
    #: Uno de los niveles activos de catalog.confidence_levels (lo valida el servicio).
    duplicate_confidence: float | None = None
    employee_device_mode: str | None = Field(default=None, max_length=30)
    risk_engine: bool | None = None
    risk_medium_score: int | None = Field(default=None, ge=1, le=100)
    risk_high_score: int | None = Field(default=None, ge=1, le=100)
    risk_critical_score: int | None = Field(default=None, ge=1, le=100)
    risk_medium_action: str | None = Field(default=None, max_length=30)
    risk_high_action: str | None = Field(default=None, max_length=30)
    risk_critical_action: str | None = Field(default=None, max_length=30)
    risk_fallback_action: str | None = Field(default=None, max_length=30)
    #: Ajuste de cada señal (solo las enviadas): modo y puntos.
    risk_signals: dict[str, RiskSignalUpdate] | None = Field(default=None, max_length=50)
    fraud_evidence: bool | None = None
    flash_paced: bool | None = None
    capture_burst: bool | None = None
    voice_verification: bool | None = None
    #: Guía por audio (decisión del dueño, 2026-10-08): si se dicta y con cuál voz (código de catalog.voice_profiles).
    voice_guidance_enabled: bool | None = None
    voice_profile: str | None = Field(default=None, max_length=30)
    #: Antifraude 2b: códigos activos de catalog.signal_modes (los valida el servicio).
    validator_signing: str | None = Field(default=None, max_length=20)
    validator_location: str | None = Field(default=None, max_length=20)
    site_codes: str | None = Field(default=None, max_length=20)
    #: Ubicación de cada verificación de identidad (codigos activos de catalog.signal_modes; los valida el servicio).
    verification_location: str | None = Field(default=None, max_length=20)
    #: Por qué se cambia (se guarda en el historial; recomendado al relajar la seguridad).
    reason: str | None = Field(default=None, max_length=500)


class SimulationActions(BaseModel):
    """Cuántos intentos terminaron (o terminarían) en cada acción."""

    allow: int = 0
    alert: int = 0
    step_up: int = 0
    review: int = 0
    deny: int = 0


class SimulationReason(BaseModel):
    code: str
    count: int


class RiskSimulationRead(BaseModel):
    """Lo que habría pasado en los últimos días con esta política (solo con lo que se midió en su momento)."""

    days: int
    #: Intentos evaluados (con tope: RISK_SIMULATION_MAX_ATTEMPTS) y si se llegó al tope.
    evaluated: int
    capped: bool
    current: SimulationActions
    candidate: SimulationActions
    #: Intentos que la política nueva trataría más estricto / más suave que la vigente.
    stricter: int
    looser: int
    #: Fraudes confirmados que la política nueva detendría (un paso más, en revisión o negar) de los confirmados.
    frauds_stopped: int
    frauds: int
    #: Intentos no marcados como fraude que la nueva política ya no permitiría directo (estimación de molestias).
    genuine_affected: int
    #: Las señales que más pesaron en lo que la nueva política no permitiría directo.
    top_reasons: list[SimulationReason]


class PolicyFieldChange(BaseModel):
    """Un campo que cambió: antes → después y si relaja la seguridad (`risk_signals.<código>.<modo|puntos>` para
    el ajuste de una señal)."""

    field: str
    before: Any = None
    after: Any = None
    relaxes: bool


class PolicyChangeRead(BaseModel):
    """Un cambio del historial de la política (catalog.policy_change_statuses)."""

    id: int
    status: str
    relaxes: bool
    preset: str | None = None
    changes: list[PolicyFieldChange]
    reason: str | None = None
    simulation: RiskSimulationRead | None = None
    requested_by: str
    requested_by_me: bool = False
    created_at: datetime
    #: Hasta cuándo se puede aprobar (solo los pendientes).
    expires_at: datetime | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None
    #: La de quien decidió o, si lo cerró el sistema (venció o la política cambió), en el idioma de quien lee.
    decision_note: StoredText = None


class PolicyChangeList(Page[PolicyChangeRead]):
    pass


class PolicyUpdateResult(BaseModel):
    """La política después de pedir un cambio y el cambio que quedó en el historial (None si no cambiaba nada).
    Si relaja la seguridad, el cambio queda PENDING y la política sigue como estaba hasta que otro ADMIN lo apruebe."""

    policy: AdminPolicyRead
    change: PolicyChangeRead | None = None


class PolicyPresetApply(BaseModel):
    preset: str = Field(max_length=30, description="catalog.policy_presets")
    reason: str | None = Field(default=None, max_length=500)


class PolicyChangeDecision(BaseModel):
    """Rechazar un cambio pendiente: el motivo es obligatorio (lo lee quien lo pidió)."""

    note: str = Field(min_length=3, max_length=500)


class RiskPolicyCandidate(BaseModel):
    """La configuración de riesgo que se quiere probar (lo omitido queda como la vigente)."""

    risk_engine: bool | None = None
    risk_medium_score: int | None = Field(default=None, ge=1, le=100)
    risk_high_score: int | None = Field(default=None, ge=1, le=100)
    risk_critical_score: int | None = Field(default=None, ge=1, le=100)
    risk_medium_action: str | None = Field(default=None, max_length=30)
    risk_high_action: str | None = Field(default=None, max_length=30)
    risk_critical_action: str | None = Field(default=None, max_length=30)
    #: El modo del dispositivo del empleado también decide (un paso más o "en revisión" ante uno desconocido).
    employee_device_mode: str | None = Field(default=None, max_length=30)
    risk_signals: dict[str, RiskSignalUpdate] | None = Field(default=None, max_length=50)
