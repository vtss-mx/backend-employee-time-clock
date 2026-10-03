"""Distancias sobre la superficie terrestre (ubicación permitida de los validadores)."""

import math

#: Radio medio de la Tierra (m, IUGG).
EARTH_RADIUS_M = 6_371_008.8


def distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Distancia en metros entre dos puntos WGS84 (fórmula de haversine).

    A las distancias de un radio de operación (decenas o miles de metros) el error frente a un
    elipsoide es de centímetros: suficiente para decidir si alguien está dentro o fuera.
    """
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lng2 - lng1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))
