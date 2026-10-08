"""Detección de ataques de presentación por FAMILIAS de rasgos (PAD de frontera; docs/rd/pad-frontera-2026-10-08.md).

Decisión del dueño del producto (2026-10-07, «al menos 1000 señales de riesgo, I+D de frontera»): en vez de inventar
1000 reglas que nieguen (y rechacen a personas reales), este módulo MIDE más de 1000 rasgos REALES y enumerables de las
capturas frontales y los agrupa en FAMILIAS; cada familia alimenta UNA señal del motor de riesgo que nace en «Solo
medir» y NUNCA niega por sí sola hasta que el dueño la calibre con datos reales (`risk_rules.CALIBRATING_SIGNALS`).

Los rasgos salen de una rejilla DETERMINISTA `familia.región.escala.canal.estadístico`:

- 7 familias, cada una una pista física real de un ataque de presentación (foto, pantalla o máscara):
  - `texture`   microtextura (fracción de patrones LBP uniformes: una impresión o pantalla la cambia);
  - `frequency` energía en la banda alta del espectro (la retícula de una pantalla, el moiré);
  - `color`     momento de color del canal (media normalizada);
  - `noise`     ruido del sensor (energía del residuo de alta frecuencia entre la de la señal);
  - `specular`  reflejos especulares (fracción de píxeles casi saturados: el brillo de una pantalla o del papel);
  - `sharpness` nitidez de alta frecuencia (energía del laplaciano entre la varianza);
  - `chroma`    croma (distancia del canal a la luma; en la luma, la saturación media).
- 17 regiones: la rejilla de 4×4 (`r0c0`..`r3c3`) más el rostro entero (`whole`).
- 3 escalas (`s0`, `s1`, `s2`): el rostro reducido a tres tamaños (lo fino y lo grueso de cada pista).
- 4 canales: `R`, `G`, `B` y la luma `Y`.

7 × 17 × 3 × 4 = 1428 rasgos con nombre único (`PAD_FEATURES`). Cada rasgo está acotado a [0, 1] por construcción
(fracciones y cocientes), así la familia es la media de sus rasgos, también en [0, 1], sin constantes mágicas.

Privacidad (regla 13): se consumen los MISMOS arreglos ya decodificados de la frontal (nunca se decodifica dos veces ni
se guarda imagen alguna); se calculan los 1428 rasgos, se agregan a 7 números por familia y los rasgos crudos se
descartan. Solo los 7 números por intento llegan a `ops.face_attempt_metrics.pad`. Funciones puras y vectorizadas, con
costo acotado (tamaños de trabajo pequeños y fijos): corren en el pool de workers, sin transacción abierta.
"""

from collections.abc import Sequence

import cv2
import numpy as np

#: Familias de pistas de ataque de presentación (cada una alimenta UNA señal del motor de riesgo).
FAMILIES: tuple[str, ...] = ("texture", "frequency", "color", "noise", "specular", "sharpness", "chroma")
#: Estadístico con que termina el nombre de cada rasgo de cada familia (uno por familia, apropiado a su pista).
STATS: dict[str, str] = {
    "texture": "lbp",
    "frequency": "band",
    "color": "mean",
    "noise": "res",
    "specular": "spec",
    "sharpness": "lap",
    "chroma": "chr",
}
#: Las 17 regiones: el rostro entero y la rejilla de 4×4 (fila, columna).
GRID = 4
REGIONS: tuple[str, ...] = ("whole", *(f"r{row}c{col}" for row in range(GRID) for col in range(GRID)))
#: Las 3 escalas y el lado (px) al que se lleva el recorte del rostro en cada una (todas divisibles entre la rejilla).
SCALES: tuple[str, ...] = ("s0", "s1", "s2")
SCALE_SIZES: tuple[int, ...] = (64, 48, 32)
#: Los 4 canales (azul, verde y rojo del BGR de OpenCV, más la luma).
CHANNELS: tuple[str, ...] = ("R", "G", "B", "Y")

#: Lado mínimo (px) de un cuadro para que sea medible; por debajo no hay con qué medir (no es sospechoso: «no medible»).
PAD_MIN_INPUT_PX = 16
#: Nivel (0-255) desde el que un píxel cuenta como reflejo especular (brillo de una pantalla o del papel).
BRIGHT_LEVEL = 240.0
#: Frecuencia normalizada (0-0.707) desde la que una corona del espectro cuenta como banda alta (retícula de pantalla).
_BAND_CUTOFF = 0.25
_EPS = 1e-9

#: El registro ENUMERABLE y estable de los rasgos: `familia.región.escala.canal.estadístico` (1428, todos únicos).
PAD_FEATURES: tuple[str, ...] = tuple(
    f"{family}.{region}.{scale}.{channel}.{STATS[family]}"
    for family in FAMILIES
    for region in REGIONS
    for scale in SCALES
    for channel in CHANNELS
)


def _usable(frame: np.ndarray) -> bool:
    """Un cuadro medible: BGR (tres canales) y con lado suficiente; si no, no se mide (nunca es sospechoso)."""
    return frame.ndim == 3 and frame.shape[2] == 3 and min(frame.shape[0], frame.shape[1]) >= PAD_MIN_INPUT_PX


def _region_bounds(size: int) -> dict[str, tuple[slice, slice]]:
    """Los cortes (filas, columnas) de cada región sobre un plano de `size`×`size`."""
    step = size // GRID
    bounds: dict[str, tuple[slice, slice]] = {"whole": (slice(0, size), slice(0, size))}
    for row in range(GRID):
        for col in range(GRID):
            bounds[f"r{row}c{col}"] = (slice(row * step, (row + 1) * step), slice(col * step, (col + 1) * step))
    return bounds


def _lbp_uniform(region: np.ndarray) -> float:
    """Fracción de patrones LBP UNIFORMES (≤ 2 transiciones 0↔1 en los 8 vecinos): la microtextura de un rostro real es
    rica y poco uniforme; una impresión o una pantalla la aplanan. Una zona plana da 1 (todo uniforme)."""
    center = region[1:-1, 1:-1]
    neighbors = (
        region[:-2, :-2],
        region[:-2, 1:-1],
        region[:-2, 2:],
        region[1:-1, 2:],
        region[2:, 2:],
        region[2:, 1:-1],
        region[2:, :-2],
        region[1:-1, :-2],
    )
    bits = np.stack([(neighbor >= center).astype(np.int8) for neighbor in neighbors])
    transitions = sum((bits[i] != bits[(i + 1) % 8]).astype(np.int32) for i in range(len(neighbors)))
    return float((transitions <= 2).mean())


def _band_fraction(region: np.ndarray) -> float:
    """Fracción de la energía del espectro que cae en la banda alta de frecuencias (retícula de una pantalla, moiré); la
    componente continua no cuenta. Una región plana (sin espectro) da 0."""
    patch = region - float(region.mean())
    power = np.abs(np.fft.fft2(patch)) ** 2
    power[0, 0] = 0.0
    total = float(power.sum())
    if total <= _EPS:
        return 0.0
    height, width = region.shape
    radius = np.hypot(np.fft.fftfreq(height)[:, None], np.fft.fftfreq(width)[None, :])
    return float(power[radius >= _BAND_CUTOFF].sum()) / total


def _noise_fraction(region: np.ndarray) -> float:
    """Energía del residuo de alta frecuencia (ruido del sensor) entre esa energía más la de la señal: un rostro pegado,
    generado o recapturado suele traer menos ruido propio que una toma real."""
    residual = region - cv2.GaussianBlur(region, (3, 3), 0)
    residual_energy = float((residual**2).mean())
    signal_energy = float(region.var())
    return residual_energy / (residual_energy + signal_energy + _EPS)


def _sharp_fraction(region: np.ndarray) -> float:
    """Energía del laplaciano (alta frecuencia) entre esa energía más la varianza: cuánta nitidez fina tiene la región
    (una pantalla reproducida pierde la nitisez del original)."""
    laplacian = cv2.Laplacian(region, cv2.CV_32F)
    high = float((laplacian**2).mean())
    return high / (high + float(region.var()) + _EPS)


def _feature(family: str, channel: str, region: np.ndarray, luma: np.ndarray, saturation: np.ndarray) -> float:
    """Un rasgo (siempre en [0, 1]) de una familia en una región de un canal. `luma` y `saturation` son la luma y la
    saturación de la MISMA región (los usa la familia de croma)."""
    if family == "texture":
        return _lbp_uniform(region)
    if family == "frequency":
        return _band_fraction(region)
    if family == "color":
        return float(region.mean()) / 255.0
    if family == "noise":
        return _noise_fraction(region)
    if family == "specular":
        return float(np.count_nonzero(region >= BRIGHT_LEVEL)) / region.size
    if family == "sharpness":
        return _sharp_fraction(region)
    # chroma: en la luma, la saturación media; en un canal de color, su distancia media a la luma.
    if channel == "Y":
        return float(saturation.mean()) / 255.0
    return float(np.abs(region - luma).mean()) / 255.0


def _accumulate(frame: np.ndarray, totals: dict[str, float]) -> None:
    """Suma a `totals` los 1428 rasgos de un cuadro (en cada escala: los planos, la luma y la saturación, y por región,
    canal y familia su rasgo)."""
    for scale, size in zip(SCALES, SCALE_SIZES, strict=True):
        resized = cv2.resize(frame, (size, size), interpolation=cv2.INTER_AREA).astype(np.float32)
        blue, green, red = resized[:, :, 0], resized[:, :, 1], resized[:, :, 2]
        luma = 0.114 * blue + 0.587 * green + 0.299 * red
        saturation = resized.max(axis=2) - resized.min(axis=2)
        planes = {"R": red, "G": green, "B": blue, "Y": luma}
        for region, (rows, cols) in _region_bounds(size).items():
            luma_region, saturation_region = luma[rows, cols], saturation[rows, cols]
            for channel in CHANNELS:
                plane_region = planes[channel][rows, cols]
                for family in FAMILIES:
                    name = f"{family}.{region}.{scale}.{channel}.{STATS[family]}"
                    totals[name] += _feature(family, channel, plane_region, luma_region, saturation_region)


def extract(frames: Sequence[np.ndarray]) -> dict[str, float]:
    """El vector completo de rasgos PAD (promediado entre los cuadros medibles). Sin ningún cuadro medible devuelve `{}`
    («no medible», nunca sospechoso)."""
    usable = [frame for frame in frames if _usable(frame)]
    if not usable:
        return {}
    totals = dict.fromkeys(PAD_FEATURES, 0.0)
    for frame in usable:
        _accumulate(frame, totals)
    count = len(usable)
    return {name: round(totals[name] / count, 6) for name in PAD_FEATURES}


def families(vector: dict[str, float]) -> dict[str, float]:
    """Agrega el vector de rasgos a UN número normalizado por familia (su media, en [0, 1]). Vector vacío → `{}`."""
    if not vector:
        return {}
    scores: dict[str, float] = {}
    for family in FAMILIES:
        values = [value for name, value in vector.items() if name.startswith(f"{family}.")]
        scores[family] = round(sum(values) / len(values), 6)
    return scores
