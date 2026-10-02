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

#: (a, b) por modelo de reconocimiento (FACE_RECOGNITION_MODEL).
_CALIBRATION: dict[str, tuple[float, float]] = {
    "fusion": (-13.3133, 38.0366),
    "sface": (-10.8702, 33.6267),
}


def match_confidence(similarity: float, model: str) -> float:
    a, b = _CALIBRATION[model]
    return 1.0 / (1.0 + math.exp(-(a + b * similarity)))


def similarity_for_confidence(confidence: float, model: str) -> float:
    """Similitud mínima con la que la confianza alcanza el nivel pedido (inversa de la anterior)."""
    a, b = _CALIBRATION[model]
    return (math.log(confidence / (1.0 - confidence)) - a) / b
