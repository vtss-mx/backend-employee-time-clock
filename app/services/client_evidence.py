"""Lo que el CLIENTE informa con un intento facial y las señales que el servidor deriva de ello (antifraude 1b).

Docs/rd §2.5 (inyección en el navegador) y §2.6 (dispositivo). Todo lo que mide el navegador lo puede falsear quien
controla el navegador; aun así sirve: atrapa herramientas comunes y scripts ingenuos, y su AUSENCIA también es una señal
(la app oficial siempre manda la telemetría y firma con la llave del dispositivo). Por eso estas señales solo miden o,
como mucho, piden más (un paso más o "en revisión"): nunca niegan (`risk_rules.ASK_ONLY_SIGNALS`).

- `ClientEvidence`: la IP y el navegador de la petición (los pone el servidor), la telemetría de la toma y la prueba
  del dispositivo (las manda la app). Viaja con el intento (`face_signals`).
- `DeviceCheck`: lo que el servidor supo del dispositivo del empleado (`employee_devices.check`) y sus señales.
- `parse_telemetry`: validación ESTRICTA (`schemas/capture.py`); grande o mal formada cuenta como ausente.
- `telemetry_hits`: automatización, cámaras virtuales instaladas, pista incoherente, ritmo sintético y pantalla
  incoherente; reglas puras, las mismas para cualquier flujo.
"""

from dataclasses import dataclass

from pydantic import ValidationError

from app.core.config import settings
from app.core.devices import classify_device
from app.models import RiskSignal
from app.schemas.capture import CaptureTelemetry, FrameTelemetry, ScreenTelemetry, TrackTelemetry
from app.services.risk_rules import Hit

#: Una pantalla de teléfono no pasa de este ancho o alto en px CSS (los más grandes rondan 1 000).
PHONE_MAX_CSS_PX = 1_400


@dataclass(frozen=True)
class DeviceProofInput:
    """La prueba del dispositivo del empleado: su llave pública (SPKI, base64) y la firma del reto que recibió con el
    reto de la prueba de vida (`device_nonce`). Vacía si la app no la mandó."""

    public_key: str | None = None
    nonce: str | None = None
    signature: str | None = None

    @property
    def sent(self) -> bool:
        return bool(self.public_key or self.nonce or self.signature)


@dataclass(frozen=True)
class TelemetryReading:
    """La telemetría de la toma ya validada (None si no llegó) y si llegó pero no era válida."""

    data: CaptureTelemetry | None = None
    invalid: bool = False


@dataclass(frozen=True)
class ClientEvidence:
    """Del cliente y de la petición, para el motor de riesgo."""

    ip: str | None = None
    user_agent: str | None = None
    #: None: el flujo no recibe telemetría (no se espera ni se marca su ausencia).
    telemetry: TelemetryReading | None = None
    #: None: el flujo no vincula el dispositivo (validador, empresa en persona).
    device: DeviceProofInput | None = None
    #: Lo que el servidor ya verificó de la petición antes del intento (antifraude 2b: la firma por petición y la
    #: ubicación del validador, `validator_presence`): sus señales para el motor de riesgo.
    checks: tuple[Hit, ...] = ()


@dataclass(frozen=True)
class DeviceCheck:
    """El dispositivo del intento: lo usa el motor de riesgo y lo anota el registro del intento."""

    mode: str
    #: Hash de la llave probada (None: sin llave o con una prueba que no verifica).
    key_hash: str | None = None
    #: Mandó una prueba que no verifica (firma, reto vencido o de otra cuenta).
    invalid: bool = False
    #: De confianza para el modo de la empresa (`employee_devices`: visto, aprobado o con un paso más superado).
    trusted: bool = False
    #: Usos anteriores del empleado en este dispositivo (0: es la primera vez).
    uses: int = 0
    #: Empleados de la empresa (contando a este) que lo usaron dentro de la ventana.
    shared: int = 1
    #: Nombre para la fila nueva.
    name: str = ""

    def hits(self) -> list[Hit]:
        """Las señales del dispositivo (DEVICE_KEY_MISSING, DEVICE_NEW y DEVICE_SHARED)."""
        hits: list[Hit] = []
        if self.key_hash is None:
            hits.append(Hit(RiskSignal.DEVICE_KEY_MISSING, 1.0 if self.invalid else 0.0))
        elif not self.trusted:
            hits.append(Hit(RiskSignal.DEVICE_NEW, float(self.uses)))
        least = settings.RISK_DEVICE_SHARED_MIN_EMPLOYEES
        if self.shared >= least:
            hits.append(Hit(RiskSignal.DEVICE_SHARED, float(self.shared), float(least)))
        return hits


def parse_telemetry(raw: str | None) -> TelemetryReading:
    """La telemetría tal como llegó: ausente, inválida (grande, mal formada o con campos de más) o válida."""
    if not raw:
        return TelemetryReading()
    if len(raw.encode()) > settings.CAPTURE_TELEMETRY_MAX_BYTES:
        return TelemetryReading(invalid=True)
    try:
        return TelemetryReading(CaptureTelemetry.model_validate_json(raw))
    except ValidationError:
        return TelemetryReading(invalid=True)


def _sides(first: int | None, second: int | None) -> tuple[int | None, int | None]:
    """Los dos lados de mayor a menor si se conocen los dos (si falta uno, tal como llegaron)."""
    if first is None or second is None:
        return first, second
    return (first, second) if first >= second else (second, first)


def track_problems(track: TrackTelemetry | None) -> int:
    """Incoherencias de la pista: lo que reporta fuera de lo que dice poder (resolución o cuadros por segundo) o sin
    `deviceId` (una cámara real lo tiene).

    La resolución se compara por LADOS (el mayor con el mayor, el menor con el menor), no por ancho y alto: un teléfono
    en vertical reporta la pista girada (720 × 1280) mientras sus capacidades siguen en horizontal (1920 × 1080 como
    máximo), y eso es la orientación del dispositivo, no una cámara que dice más de lo que puede. Una cámara virtual que
    reporta 1920 × 1080 con capacidades de 1280 × 720 sigue marcándose (sus dos lados se pasan)."""
    if track is None:
        return 0
    sides = _sides(track.width, track.height)
    limits = _sides(track.width_max, track.height_max)
    pairs = (*zip(sides, limits, strict=True), (track.frame_rate, track.frame_rate_max))
    over = sum(1 for value, limit in pairs if value is not None and limit is not None and value > limit + 0.5)
    return over + (0 if track.device_id else 1)


def frames_synthetic(frames: FrameTelemetry) -> bool:
    """El ritmo de los cuadros es demasiado exacto para una cámara real (FRAME_TIMING_SYNTHETIC).

    Solo se juzga con el reloj de LLEGADA de cada cuadro (`clock = presentation`). Con el reloj del dibujo (`render`, o
    sin decirlo: una app anterior) la señal NO se puede medir: ese reloj va alineado al refresco de la pantalla y una
    cámara real de 30 cuadros en fase con una pantalla de 60 Hz da intervalos idénticos (33.3 ms), lo mismo que una
    cámara virtual. Lo que no se puede medir nunca cuenta como sospechoso (se queda sin señal)."""
    if frames.clock != "presentation":
        return False
    return frames.count >= settings.RISK_FRAME_TIMING_MIN_FRAMES and frames.cv < settings.RISK_FRAME_TIMING_MIN_CV


def screen_problems(screen: ScreenTelemetry | None, user_agent: str | None) -> int:
    """Incoherencias de la pantalla: sin medidas (navegador sin interfaz) o un "teléfono" sin pantalla táctil o con
    una pantalla de escritorio (un navegador de escritorio que finge ser teléfono o un emulador)."""
    if screen is None:
        return 0
    problems = 1 if not (screen.width and screen.height and screen.pixel_ratio) else 0
    if classify_device(user_agent) == "phone":
        problems += screen.touch_points == 0
        problems += max(screen.width, screen.height) > PHONE_MAX_CSS_PX
    return problems


def telemetry_hits(reading: TelemetryReading | None, user_agent: str | None) -> list[Hit]:
    """Las señales de la telemetría de la toma (solo si el flujo la espera)."""
    if reading is None:
        return []
    data = reading.data
    if data is None:
        return [Hit(RiskSignal.TELEMETRY_MISSING, 1.0 if reading.invalid else 0.0)]
    hits: list[Hit] = []
    traces = data.automation + (1 if data.webdriver else 0)
    if traces:
        hits.append(Hit(RiskSignal.AUTOMATION, float(traces)))
    if data.virtual_camera:
        hits.append(Hit(RiskSignal.VIRTUAL_CAMERA_PRESENT))
    if track := track_problems(data.track):
        hits.append(Hit(RiskSignal.TRACK_INCONSISTENT, float(track)))
    if (frames := data.frames) is not None and frames_synthetic(frames):
        hits.append(Hit(RiskSignal.FRAME_TIMING_SYNTHETIC, round(frames.cv, 5), settings.RISK_FRAME_TIMING_MIN_CV))
    if screen := screen_problems(data.screen, user_agent):
        hits.append(Hit(RiskSignal.SCREEN_INCOHERENT, float(screen)))
    return hits
