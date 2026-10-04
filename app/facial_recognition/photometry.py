"""Reto fotométrico ("destello de colores"): la pantalla ilumina el rostro con colores al azar.

El servidor elige la secuencia de colores al emitir el reto; la aplicación pinta la pantalla completa
de cada color y captura un fotograma con cada uno. Un rostro real, a pocos centímetros de la pantalla,
refleja ese color: su cromaticidad (proporción de rojo, verde y azul) se desplaza HACIA el color
emitido. Lo que no está frente a la pantalla no responde así:

- Un video inyectado (cámara virtual, deepfake, grabación) no conoce los colores: el rostro no cambia
  o cambia hacia otro lado.
- Una pantalla que muestra un rostro emite su propia luz: refleja poco el color.
- En una foto impresa el papel completo (rostro y fondo) responde igual; en una persona real el fondo,
  más lejos, responde menos que el rostro.

Solo se usan números: la cromaticidad media de la zona central del rostro (nariz y mejillas) y la de
los lados del encuadre. La medición depende de la luz ambiente (a pleno sol el destello casi no se
nota): por eso primero se OBSERVA (se mide sin bloquear) y se calibra con capturas reales antes de
exigirlo (política `flash_liveness`).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import combinations

import numpy as np

#: Colores del destello (código → RGB). Primarios y secundarios: los pares complementarios dan el
#: mayor cambio de cromaticidad.
FLASH_PALETTE: dict[str, tuple[int, int, int]] = {
    "RED": (255, 0, 0),
    "GREEN": (0, 255, 0),
    "BLUE": (0, 0, 255),
    "YELLOW": (255, 255, 0),
    "CYAN": (0, 255, 255),
    "MAGENTA": (255, 0, 255),
}


def flash_hex(code: str) -> str:
    """Color en formato #RRGGBB (lo que pinta la aplicación)."""
    r, g, b = FLASH_PALETTE[code]
    return f"#{r:02X}{g:02X}{b:02X}"


def _chroma(rgb: np.ndarray) -> np.ndarray:
    """Proporción de rojo, verde y azul (suma 1); sin luz, ceros."""
    total = float(rgb.sum())
    return rgb / total if total > 1e-6 else np.zeros(3)


def emitted_chroma(code: str) -> np.ndarray:
    return _chroma(np.asarray(FLASH_PALETTE[code], dtype=np.float64))


def _region_rgb(image: np.ndarray, x0: int, y0: int, x1: int, y1: int) -> np.ndarray | None:
    """RGB medio de una región (recortada a la imagen); None si queda vacía."""
    h, w = image.shape[:2]
    x0, x1 = max(0, x0), min(w, x1)
    y0, y1 = max(0, y0), min(h, y1)
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None
    b, g, r = image[y0:y1, x0:x1].reshape(-1, 3).mean(axis=0)
    return np.array([r, g, b], dtype=np.float64)


def _triple(values: np.ndarray) -> tuple[float, float, float]:
    r, g, b = (round(float(v), 6) for v in values)
    return r, g, b


@dataclass(frozen=True)
class FlashSample:
    """Cromaticidad del rostro (nariz y mejillas) y del fondo junto a él en un fotograma del destello."""

    face: tuple[float, float, float]
    #: Franjas a los lados del rostro; None si el rostro llena el encuadre.
    background: tuple[float, float, float] | None


def flash_sample(image: np.ndarray, box: tuple[int, int, int, int]) -> FlashSample:
    """Mide un fotograma BGR con el rostro en `box` (x, y, ancho, alto)."""
    x, y, w, h = box
    face = _region_rgb(image, x + w // 4, y + int(h * 0.35), x + w - w // 4, y + int(h * 0.85))
    if face is None:
        raise ValueError("El rostro quedó fuera del fotograma")
    sides = [
        region
        for region in (
            _region_rgb(image, x - w // 2, y, x - w // 8, y + h),
            _region_rgb(image, x + w + w // 8, y, x + w + w // 2, y + h),
        )
        if region is not None
    ]
    return FlashSample(
        face=_triple(_chroma(face)),
        background=_triple(_chroma(np.mean(sides, axis=0))) if sides else None,
    )


@dataclass(frozen=True)
class FlashResponse:
    """Qué tanto respondió el rostro a los colores emitidos.

    - `score`: coseno medio entre el cambio de cromaticidad del rostro y el de los colores emitidos,
      en cada par de fotogramas con colores distintos (1 = siguió los colores; 0 = no respondió;
      negativo = respondió a otros colores).
    - `magnitude`: tamaño medio de ese cambio (con mucha luz ambiente es casi 0: no concluyente).
    - `background_magnitude`: lo mismo en el fondo (en una persona real responde menos que el rostro).
    """

    score: float
    magnitude: float
    background_magnitude: float | None
    pairs: int

    def conclusive(self, min_magnitude: float) -> bool:
        return self.pairs > 0 and self.magnitude >= min_magnitude


def flash_response(samples: Sequence[FlashSample], colors: Sequence[str]) -> FlashResponse:
    """Compara cada par de fotogramas con colores distintos (respuesta del rostro contra lo emitido)."""
    scores: list[float] = []
    magnitudes: list[float] = []
    backgrounds: list[float] = []
    for i, j in combinations(range(min(len(samples), len(colors))), 2):
        if colors[i] == colors[j]:
            continue
        emitted = emitted_chroma(colors[i]) - emitted_chroma(colors[j])
        seen = np.asarray(samples[i].face) - np.asarray(samples[j].face)
        size = float(np.linalg.norm(seen))
        magnitudes.append(size)
        scores.append(float(seen @ emitted) / (size * float(np.linalg.norm(emitted))) if size > 1e-9 else 0.0)
        first, second = samples[i].background, samples[j].background
        if first is not None and second is not None:
            backgrounds.append(float(np.linalg.norm(np.asarray(first) - np.asarray(second))))
    if not scores:
        return FlashResponse(score=0.0, magnitude=0.0, background_magnitude=None, pairs=0)
    return FlashResponse(
        score=round(float(np.mean(scores)), 4),
        magnitude=round(float(np.mean(magnitudes)), 5),
        background_magnitude=round(float(np.mean(backgrounds)), 5) if backgrounds else None,
        pairs=len(scores),
    )
