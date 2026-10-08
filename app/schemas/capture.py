"""Lo que la app manda con las capturas faciales además de las imágenes (antifraude 1b, docs/rd §2.5 y §2.7).

Todo lo informa el cliente: el servidor lo valida ESTRICTO (campos conocidos, tipos y rangos acotados, nada extra) y lo
toma como señal, nunca como prueba. La telemetría nunca lleva datos de la persona ni la lista de dispositivos: solo
números e indicadores (regla 13 de la raíz).
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TrackTelemetry(_Strict):
    """La pista de la cámara: lo que reporta `getSettings()` y los máximos de `getCapabilities()` (None: el navegador
    no lo informa, p. ej. Firefox sin `getCapabilities`)."""

    width: int | None = Field(default=None, ge=0, le=20_000)
    height: int | None = Field(default=None, ge=0, le=20_000)
    frame_rate: float | None = Field(default=None, ge=0, le=1_000)
    width_max: int | None = Field(default=None, ge=0, le=20_000)
    height_max: int | None = Field(default=None, ge=0, le=20_000)
    frame_rate_max: float | None = Field(default=None, ge=0, le=1_000)
    #: La pista trae `deviceId` (una cámara real siempre lo tiene con el permiso dado).
    device_id: bool = True


class FrameTelemetry(_Strict):
    """Ritmo de los fotogramas del video (`requestVideoFrameCallback`): cuántos, intervalo medio y su variación."""

    count: int = Field(ge=0, le=100_000)
    mean_ms: float = Field(ge=0, le=100_000)
    #: Coeficiente de variación del intervalo (desviación / media): una cámara real varía; un video sintético, no.
    cv: float = Field(ge=0, le=1_000)
    #: Con qué reloj se midieron los intervalos (compatibilidad universal, docs/rd/compatibilidad-biometria.md):
    #: `presentation`, cuándo llegó cada cuadro (`VideoFrameCallbackMetadata.presentationTime`, lo da Chrome, Safari y
    #: Firefox); `render`, el `now` de la llamada, que es el instante de DIBUJAR y va alineado al refresco de la
    #: pantalla (60/120 Hz): una cámara real de 30 cuadros en fase con la pantalla da intervalos idénticos en él. None:
    #: una versión anterior de la app, que medía con `render`.
    clock: Literal["presentation", "render"] | None = None


class ScreenTelemetry(_Strict):
    """Pantalla y entrada del dispositivo (CSS px)."""

    width: int = Field(ge=0, le=100_000)
    height: int = Field(ge=0, le=100_000)
    pixel_ratio: float = Field(ge=0, le=100)
    touch_points: int = Field(ge=0, le=1_000)


#: Quién integró el SDK móvil de la API pública de verificación (`docs/sdk/contrato-verificacion.md` §7): el nativo de
#: cada plataforma o un envoltorio sobre él (.NET MAUI, React Native, Flutter).
type NativeClient = Literal["android-sdk", "ios-sdk", "maui-sdk", "react-native-sdk", "flutter-sdk"]


class NativeTelemetry(_Strict):
    """Lo que informa un SDK móvil (API pública de verificación, migración 0084) además de lo del navegador: quién lo
    integró, en qué plataforma corre, versiones y modelo de equipo. Nunca datos de la persona (el nombre que el usuario
    le puso a su equipo, cuentas, identificadores de publicidad) ni la lista de cámaras. Lo que el SDK no puede medir
    viaja como `null` («sin medir»: nunca cuenta como sospechoso)."""

    client: NativeClient
    #: La plataforma REAL donde corre (también con un envoltorio).
    platform: Literal["android", "ios"]
    sdk_version: str = Field(max_length=40, pattern=r"^\d+\.\d+\.\d+([-+][0-9A-Za-z.-]{1,20})?$")
    os_version: str = Field(max_length=20, pattern=r"^[0-9A-Za-z._-]{1,20}$")
    #: Modelo de equipo (Android `Build.MODEL`; iOS el identificador de hardware, p. ej. `iPhone16,2`).
    device_model: str = Field(max_length=64, pattern=r"^[\x20-\x7E]{1,64}$")
    #: La cámara usada es una cámara frontal física integrada (None: sin medir).
    front_camera_physical: bool | None = None
    #: El SDK corre en un emulador o simulador (None: sin medir).
    emulator: bool | None = None


class CaptureTelemetry(_Strict):
    """Telemetría de una toma (versión 1). La app oficial siempre la manda; su ausencia también es una señal. Los SDK
    móviles agregan `native` (la aplicación web nunca lo manda: su forma no cambia)."""

    v: Literal[1]
    #: `navigator.webdriver`.
    webdriver: bool
    #: Rastros de automatización encontrados (ChromeDriver, Playwright, Selenium, PhantomJS, Headless...).
    automation: int = Field(ge=0, le=100)
    #: Alguna cámara de `enumerateDevices()` es virtual (solo el indicador, nunca la lista).
    virtual_camera: bool
    track: TrackTelemetry | None = None
    frames: FrameTelemetry | None = None
    screen: ScreenTelemetry | None = None
    native: NativeTelemetry | None = None


class LocationSample(_Strict):
    """Una lectura de la ubicación de las varias que toma la app en una ventana corta (inmutable: dos iguales son la
    misma lectura)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy: float = Field(ge=0, le=100_000)


class BurstMeta(_Strict):
    """Cómo viene la hoja de la ráfaga (antifraude 2a, versión 1): lado de cada recorte (px), columnas de la
    cuadrícula, el instante de cada recorte (ms desde el primero) y su tramo (`H` quieto, `M` movimiento, en orden). El
    servidor lo vuelve a validar contra lo que pidió (`capture_protocol.burst_layout`): lo que no cumpla es una
    señal."""

    v: Literal[1]
    tile: int = Field(ge=16, le=512)
    cols: int = Field(ge=1, le=64)
    t: list[int] = Field(min_length=1, max_length=100)
    s: str = Field(min_length=1, max_length=100, pattern=r"^H*M*$")
