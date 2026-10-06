"""Ráfaga corta de recortes del rostro y señales físicas de la toma (antifraude fase 2a; docs/rd/antifraude-identidad.md
§2.2, decisión D11 del dueño del producto).

La app manda con las capturas una HOJA JPEG con N recortes pequeños del rostro (`tile` px de lado, en una cuadrícula de
`cols` columnas) tomados en ~2-3 s: un tramo QUIETO (`H`: la persona de frente mientras se valida la toma) y un tramo de
MOVIMIENTO (`M`: el primer giro o cabeceo del reto). Con ella el servidor mide lo que una sola foto no puede decir:

- **Continuidad**: el mismo rostro en todos los recortes, sin saltos (un montaje de fuentes distintas salta).
- **Micromovimiento natural**: una persona nunca da dos fotogramas iguales (ruido del sensor, respiración); un video
  congelado o una imagen fija inyectada, sí. Un video en bucle repite fotogramas idénticos lejos uno del otro.
- **Pulso por video (rPPG, POS de Wang et al., IEEE TBME 2017)**: la piel cambia de color con cada latido. Solo se MIDE
  (docs/rd §8 P4: con clips de 2-3 s es poco fiable; nunca decide hasta que el dueño lo calibre).

Y, con las capturas a resolución completa que ya llegan (la hoja reduce 3-4 veces y borra lo fino):

- **Moiré**: una pantalla fotografiada deja picos estrechos en el espectro (la retícula de sus píxeles); un rostro real
  tiene un espectro suave.
- **Ruido del sensor**: un rostro pegado, generado o recapturado suele ser más "liso" que el fondo de la misma toma.
- **Perspectiva (paralaje)**: al girar, la nariz de un rostro 3D se sale de la afín que siguen ojos y comisuras (casi
  coplanares); una foto plana, aunque la inclinen o la deformen, la sigue (prototipo: 0.13-0.19 contra ≤ 0.02).

Solo números: la hoja se analiza en memoria y se descarta (nunca va a la BD ni al bucket, salvo como evidencia de un
caso de fraude por el camino de la decisión D1). Funciones puras: el pipeline les da los recortes y los puntos.
"""

import io
import math
import warnings
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from itertools import pairwise

import cv2
import numpy as np
from PIL import Image

#: Tramos de la ráfaga: quieto (de frente) y movimiento (el primer giro o cabeceo del reto).
HOLD = "H"
MOVE = "M"
#: Orden de los 5 puntos (YuNet): ojo, ojo, nariz, comisura, comisura. Ojos y comisuras están casi en un plano.
PLANE_POINTS = (0, 1, 3, 4)
NOSE = 2
#: Lado de la miniatura con que se buscan fotogramas repetidos (un fotograma idéntico da una miniatura idéntica).
THUMB = 32
#: Máscara de segundo orden (Immerkær): responde al ruido, casi nada a las zonas lisas.
_LAPLACE = np.array([[1, -2, 1], [-2, 4, -2], [1, -2, 1]], dtype=np.float32)
#: Fracción de las respuestas que cuenta para el ruido: las más bajas (los bordes y la textura dan las más altas).
_NOISE_QUANTILE = 0.75
#: SNR de una serie de color sin ninguna variación (imagen congelada): no hay pulso.
FLAT_PULSE_DB = -60.0


class BurstMalformed(ValueError):
    """La hoja no es lo que dice su descripción (formato, medidas o contenido ilegible): una señal, nunca un error."""


@dataclass(frozen=True)
class BurstLayout:
    """Cómo viene la hoja: lado de cada recorte, columnas, y de cada recorte su instante (ms desde el primero) y su
    tramo. Ya validada contra lo que pidió el servidor (`capture_protocol.burst_layout`)."""

    tile: int
    cols: int
    times: tuple[int, ...]
    segments: tuple[str, ...]

    @property
    def count(self) -> int:
        return len(self.times)

    @property
    def size(self) -> tuple[int, int]:
        """(ancho, alto) exactos de la hoja."""
        rows = math.ceil(self.count / self.cols)
        return self.cols * self.tile, rows * self.tile

    def indices(self, segment: str) -> list[int]:
        return [i for i, label in enumerate(self.segments) if label == segment]


def decode_sheet(data: bytes, layout: BurstLayout) -> np.ndarray:
    """La hoja en BGR, solo si es un JPEG con las medidas exactas de su descripción (se leen los encabezados ANTES de
    decodificar: una "bomba" de descompresión no reserva memoria)."""
    if not data.startswith(b"\xff\xd8"):
        raise BurstMalformed("not-jpeg")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            header = Image.open(io.BytesIO(data))
            size = header.size
    except Exception as exc:
        raise BurstMalformed("unreadable") from exc
    if size != layout.size:
        raise BurstMalformed("size")
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None or (image.shape[1], image.shape[0]) != layout.size:
        raise BurstMalformed("undecodable")
    return image


def tiles_of(sheet: np.ndarray, layout: BurstLayout) -> list[np.ndarray]:
    """Los recortes de la hoja, en orden (fila por fila)."""
    side = layout.tile
    return [
        sheet[
            (i // layout.cols) * side : (i // layout.cols + 1) * side,
            (i % layout.cols) * side : (i % layout.cols + 1) * side,
        ]
        for i in range(layout.count)
    ]


def gray(image: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.float32)


def micro_motion(grays: Sequence[np.ndarray]) -> float | None:
    """Cuánto cambia la imagen de un recorte al siguiente (mediana de la diferencia media, niveles de gris 0-255): ruido
    del sensor más el movimiento natural. Un video congelado o una imagen fija inyectada dan 0."""
    if len(grays) < 2:
        return None
    changes = [float(np.abs(a - b).mean()) for a, b in pairwise(grays)]
    return round(float(np.median(changes)), 4)


def repeated_frames(grays: Sequence[np.ndarray], identical: float) -> int:
    """Fotogramas que REPITEN uno anterior no contiguo, con algo distinto en medio: la firma de un video en bucle
    reproducido fotograma por fotograma (una cámara real nunca repite su ruido). Una cámara que se congela (todos los
    del medio iguales) no cuenta aquí: eso lo mide el micromovimiento."""
    if len(grays) < 3:
        return 0
    thumbs = np.stack([cv2.resize(g, (THUMB, THUMB), interpolation=cv2.INTER_AREA).reshape(-1) for g in grays])
    distance = np.abs(thumbs[:, None, :] - thumbs[None, :, :]).mean(axis=2)
    repeated = 0
    for j in range(2, len(grays)):
        for i in range(j - 1):
            between = distance[i + 1 : j, j]
            if distance[i, j] <= identical and float(between.max()) > identical:
                repeated += 1
                break
    return repeated


def interocular(points: np.ndarray) -> float:
    return max(float(np.linalg.norm(points[1] - points[0])), 1e-6)


def landmark_jumps(points: Sequence[np.ndarray | None], max_jump: float) -> int:
    """Saltos entre recortes CONSECUTIVOS con rostro: los puntos se desplazan más de `max_jump` distancias entre ojos o
    el rostro cambia de tamaño de golpe (más de la mitad). Un movimiento natural es continuo; un montaje, no."""
    jumps = 0
    for before, after in pairwise(points):
        if before is None or after is None:
            continue
        scale = interocular(after) / interocular(before)
        shift = float(np.linalg.norm(after - before, axis=1).mean()) / interocular(before)
        if shift > max_jump or not 0.67 <= scale <= 1.5:
            jumps += 1
    return jumps


def skin_rgb(tile: np.ndarray, box: tuple[int, int, int, int]) -> tuple[float, float, float] | None:
    """RGB medio de la piel de mejillas y nariz (la zona que más cambia con el pulso y menos con los ojos y la boca)."""
    x, y, w, h = box
    height, width = tile.shape[:2]
    x0, x1 = max(0, x + w // 5), min(width, x + w - w // 5)
    y0, y1 = max(0, y + int(h * 0.45)), min(height, y + int(h * 0.75))
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    b, g, r = (float(v) for v in tile[y0:y1, x0:x1].reshape(-1, 3).mean(axis=0))
    return r, g, b


@dataclass(frozen=True)
class Pulse:
    """El pulso medido: cuánto sobresale la frecuencia cardiaca del resto de la banda (dB) y cuál es (latidos/min)."""

    snr_db: float
    bpm: float


def pulse(
    rgbs: Sequence[tuple[float, float, float]], times_ms: Sequence[int], *, rate: float, low_hz: float, high_hz: float
) -> Pulse | None:
    """rPPG con POS (plano ortogonal a la piel, Wang et al. 2017): la serie de color se lleva a una frecuencia fija,
    se normaliza por su media, se proyecta al plano que cancela los cambios de brillo y de movimiento, y se busca el
    pico de la banda cardiaca. SNR = energía en el pico (±0.15 Hz) contra el resto de la banda. Implementación propia
    (rPPG-Toolbox tiene una licencia RAIL que prohíbe la vigilancia de empleo; pyVHR es GPL-3.0)."""
    if len(rgbs) < 8 or len(rgbs) != len(times_ms):
        return None
    seconds = np.asarray(times_ms, dtype=np.float64) / 1000
    grid = np.arange(seconds[0], seconds[-1], 1 / rate)
    if len(grid) < 8:
        return None
    colors = np.asarray(rgbs, dtype=np.float64)
    series = np.stack([np.interp(grid, seconds, colors[:, k]) for k in range(3)], axis=1)
    normalized = series / np.maximum(series.mean(axis=0), 1e-6) - 1
    s1 = normalized[:, 1] - normalized[:, 2]
    s2 = normalized[:, 1] + normalized[:, 2] - 2 * normalized[:, 0]
    signal = s1 + (s1.std() / max(float(s2.std()), 1e-12)) * s2
    if float(signal.std()) < 1e-9:
        return Pulse(snr_db=FLAT_PULSE_DB, bpm=0.0)
    ticks = np.arange(len(signal))
    signal = signal - np.polyval(np.polyfit(ticks, signal, 1), ticks)
    nfft = 512
    power = np.abs(np.fft.rfft(signal * np.hanning(len(signal)), nfft)) ** 2
    freqs = np.fft.rfftfreq(nfft, 1 / rate)
    band = (freqs >= low_hz) & (freqs <= high_hz)
    peak_at = int(np.argmax(np.where(band, power, 0.0)))
    peak = band & (np.abs(freqs - freqs[peak_at]) <= 0.15)
    rest = float(power[band & ~peak].sum())
    snr = 10 * math.log10(max(float(power[peak].sum()), 1e-18) / max(rest, 1e-18))
    return Pulse(snr_db=round(snr, 2), bpm=round(float(freqs[peak_at]) * 60, 1))


def parallax(reference: np.ndarray, current: np.ndarray) -> float:
    """Cuánto se sale la nariz de la afín que siguen ojos y comisuras entre dos poses (en distancias entre ojos de la
    referencia). Una superficie plana (foto o pantalla), aunque se gire, se incline o se deforme, sigue una afín (la
    perspectiva débil de un plano): ≈ 0. Un rostro real que gira 15-30° deja la nariz fuera: 0.1-0.19."""
    plane = list(PLANE_POINTS)
    design = np.hstack([reference[plane], np.ones((len(plane), 1))])
    transform, *_ = np.linalg.lstsq(design, current[plane], rcond=None)
    predicted = np.append(reference[NOSE], 1.0) @ transform
    return round(float(np.linalg.norm(predicted - current[NOSE])) / interocular(reference), 4)


@cache
def _rings(side: int) -> tuple[np.ndarray, ...]:
    """Las coronas de frecuencia de un parche de `side` (sin los ejes ni las líneas de la cuadrícula JPEG de 8 px)."""
    fy, fx = np.indices((side, side)) - side // 2
    radius = np.rint(np.hypot(fx, fy)).astype(int)
    grid = side // 8
    excluded = (fx == 0) | (fy == 0) | (fx % grid == 0) | (fy % grid == 0)
    rings = [(radius == r) & ~excluded for r in range(int(side * 0.18), side // 2)]
    return tuple(ring for ring in rings if int(ring.sum()) >= 8)


def moire(image: np.ndarray, box: tuple[int, int, int, int]) -> float | None:
    """Energía de patrón de pantalla del rostro (dB): en cada corona de frecuencias, cuánto sobresale el pico más alto
    de la mediana de su corona; se queda la mayor. Un rostro real tiene un espectro suave (≈ 10-12 dB en el prototipo);
    la retícula de una pantalla fotografiada deja picos estrechos (14-17 dB). El parche se alinea a la cuadrícula de
    8 px del JPEG para excluir sus bloques (caen en líneas fijas del espectro)."""
    x, y, w, h = box
    side = 128 if w >= 160 else 64
    height, width = image.shape[:2]
    cx, cy = x + w // 2, y + int(h * 0.55)
    x0 = min(max(0, cx - side // 2), width - side) // 8 * 8
    y0 = min(max(0, cy - side // 2), height - side) // 8 * 8
    if x0 < 0 or y0 < 0 or w < 48:
        return None
    patch = gray(image[y0 : y0 + side, x0 : x0 + side])
    patch -= patch.mean()
    window = np.outer(np.hanning(side), np.hanning(side))
    spectrum = np.abs(np.fft.fftshift(np.fft.fft2(patch * window))) ** 2
    best = max(float(spectrum[ring].max()) / max(float(np.median(spectrum[ring])), 1e-9) for ring in _rings(side))
    # Un parche sin ninguna textura (todo del mismo tono: una imagen sintética) no tiene espectro que medir.
    return round(10 * math.log10(best), 3) if best > 0 else None


def noise_level(region: np.ndarray) -> float | None:
    """Ruido de una zona en gris con el filtro de Immerkær (σ ≈ √(π/2)·media|respuesta|/6), promediando solo las
    respuestas más bajas: los bordes y la textura no lo inflan y, a diferencia de la mediana, no queda en saltos enteros
    (una captura JPEG da respuestas cuantizadas)."""
    if region.shape[0] < 6 or region.shape[1] < 6:
        return None
    response = np.abs(cv2.filter2D(region, cv2.CV_32F, _LAPLACE)[1:-1, 1:-1]).reshape(-1)
    calm = response[response <= float(np.quantile(response, _NOISE_QUANTILE))]
    return float(math.sqrt(math.pi / 2) * float(calm.mean()) / 6)


def noise_ratio(image: np.ndarray, box: tuple[int, int, int, int], *, min_background: float) -> float | None:
    """Ruido del rostro (mejillas y nariz) entre el del fondo a sus lados, en la misma captura. Una toma real tiene el
    mismo sensor en los dos; un rostro pegado, generado o recapturado suele ser más liso que el fondo. None si el fondo
    no se ve (rostro que llena el encuadre) o es plano del todo (sin ruido medible: sol directo, fondo saturado)."""
    x, y, w, h = box
    full = gray(image)
    height, width = full.shape
    face = full[max(0, y + h // 4) : min(height, y + h - h // 6), max(0, x + w // 5) : min(width, x + w - w // 5)]
    rows = slice(max(0, y), min(height, y + h))
    sides = [
        full[rows, max(0, x - w // 2) : max(0, x - w // 8)],
        full[rows, min(width, x + w + w // 8) : min(width, x + w + w // 2)],
    ]
    levels = [level for side in sides if (level := noise_level(side)) is not None]
    face_level = noise_level(face)
    if not levels or face_level is None or max(levels) < min_background:
        return None
    return round(face_level / max(levels), 4)
