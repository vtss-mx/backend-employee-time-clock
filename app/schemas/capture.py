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


class ScreenTelemetry(_Strict):
    """Pantalla y entrada del dispositivo (CSS px)."""

    width: int = Field(ge=0, le=100_000)
    height: int = Field(ge=0, le=100_000)
    pixel_ratio: float = Field(ge=0, le=100)
    touch_points: int = Field(ge=0, le=1_000)


class CaptureTelemetry(_Strict):
    """Telemetría de una toma (versión 1). La app oficial siempre la manda; su ausencia también es una señal."""

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
