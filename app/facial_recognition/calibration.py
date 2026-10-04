"""Confianza de una comparación facial: probabilidad de que sea la misma persona.

La similitud coseno no es un porcentaje interpretable (un empleado legítimo obtiene ~0.6-0.8).
Se convierte a probabilidad con una regresión logística ajustada sobre LFW (6 000 pares, mismo
detector y modelo que en producción, clases balanceadas):

    confianza = 1 / (1 + e^-(a + b · similitud))

Fusión en LFW (similitud exigida · falsos aceptados · rechazos de una captura legítima):
  80 %      0.386 · 0.03 % · 0.9 %   (mínimo configurable)
  90 %      0.408 · 0.03 % · 1.1 %
  99 %      0.471 · 0     · 2.4 %
  99.9 %    0.532 · 0     · 5.4 %
  99.99 %   0.592 · 0     · 11.6 %
  99.999 %  0.653 · 0     · 24.3 %   (nivel configurado; el impostor más parecido de LFW: 0.469)
Los niveles más altos son una extrapolación de la curva: ningún impostor de LFW se acerca a ellos.
"""

import math

#: (a, b) por modelo. "facenet" (solo FaceNet) se ajustó igual sobre LFW (scripts/calibrate_lfw.py):
#: sirve para exigir que cada modelo de la fusión coincida por su cuenta (`model_floor_confidence`).
_CALIBRATION: dict[str, tuple[float, float]] = {
    "fusion": (-13.3133, 38.0366),
    "sface": (-10.8702, 33.6267),
    "facenet": (-11.5726, 27.9249),
}

#: Confianza mínima de cada modelo por separado, nunca por debajo de este piso.
MODEL_FLOOR_BASE = 0.40
#: Cada modelo exige "tres nueves menos" que la fusión: 99.999 % → 99 %; hasta 99.9 % → el piso.
MODEL_FLOOR_FACTOR = 1000.0


def model_floor_confidence(confidence: float) -> float:
    """Confianza que debe alcanzar CADA modelo de la fusión (SFace y FaceNet) cuando la fusión exige
    `confidence`. Defensa contra imágenes fabricadas para engañar a un solo modelo: en LFW (6000
    pares) agrega a lo más 0.23 % de rechazos legítimos (nivel 80 %; 0 desde 99 %), elimina al único
    impostor que la fusión sola aceptaba y, si un modelo quedara totalmente engañado, el otro solo
    dejaría pasar 0.3-0.6 % de los impostores (sin pisos: 39-100 % hasta 99.9 %)."""
    return max(MODEL_FLOOR_BASE, 1.0 - MODEL_FLOOR_FACTOR * (1.0 - confidence))


def match_confidence(similarity: float, model: str) -> float:
    a, b = _CALIBRATION[model]
    return 1.0 / (1.0 + math.exp(-(a + b * similarity)))


def similarity_for_confidence(confidence: float, model: str) -> float:
    """Similitud mínima con la que la confianza alcanza el nivel pedido (inversa de la anterior)."""
    a, b = _CALIBRATION[model]
    return (math.log(confidence / (1.0 - confidence)) - a) / b
