from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import settings


class VerificationPolicyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    block_glasses: bool = Field(description="Exigir retirar lentes (incluye lentes de sol)")
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
        default="OBSERVE",
        description="Destello de colores en la pantalla (catalog.flash_modes): OFF, OBSERVE (solo medir) o ENFORCE",
    )
    block_virtual_cameras: bool = Field(
        default=True, description="Rechazar cámaras virtuales (programas que inyectan video)"
    )
    reject_foreign_images: bool = Field(
        default=True, description="Rechazar imágenes con metadatos de cámara o de edición (no son capturas en vivo)"
    )
    detect_static_captures: bool = Field(default=True, description="Rechazar capturas idénticas (una foto fija)")
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
    updated_at: datetime | None = None
    updated_by: str | None = None


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
