"""Ubicación permitida de los validadores: solo inician sesión dentro del radio de su domicilio.

La empresa activa "requiere ubicación" en el validador y fija el punto en el mapa y el radio. Al
iniciar sesión, el dispositivo envía su ubicación (la del navegador) y aquí se decide:

- Sin ubicación → 403 `LOCATION_REQUIRED` (la webapp la pide con el aviso nativo y reintenta).
- Lectura demasiado imprecisa → 403 `LOCATION_INACCURATE` (no se puede asegurar que esté dentro).
- Fuera del radio → 403 `LOCATION_OUT_OF_RANGE`, con la distancia y el radio en `details`.

A la distancia se le descuenta la precisión informada por el dispositivo, hasta un margen máximo
(`VALIDATOR_LOCATION_TOLERANCE_M`), para no rechazar a quien está en el borde por el error del GPS.
"""

from typing import cast

from app.core.config import settings
from app.core.exceptions import PermissionDeniedError
from app.core.geo import distance_m
from app.models import User, UserRole
from app.schemas.auth import DeviceLocation

LOCATION_REQUIRED = (
    "Este validador solo puede iniciar sesión en su lugar de operación: permite el acceso a tu ubicación."
)
LOCATION_INACCURATE = (
    "La ubicación de tu dispositivo no es precisa (±{accuracy}). Activa la ubicación precisa o el GPS e "
    "inténtalo de nuevo."
)
LOCATION_OUT_OF_RANGE = (
    "Estás a {distance} del lugar de este validador. Solo puede iniciar sesión a no más de {radius} de ese punto."
)


def format_distance(meters: float) -> str:
    """350 m · 1.2 km · 15 km."""
    if meters < 1000:
        return f"{round(meters)} m"
    km = meters / 1000
    return f"{km:.1f} km" if km < 10 else f"{round(km)} km"


def ensure_location_allowed(user: User, location: DeviceLocation | None) -> None:
    """Valida la ubicación del dispositivo al iniciar sesión (solo validadores que la requieren)."""
    validator = user.validator if user.role == UserRole.VALIDATOR else None
    if validator is None or not validator.location_required:
        return
    # Exigir ubicación implica punto y radio: lo validan el alta y la edición del validador
    # (LOCATION_POINT_REQUIRED / LOCATION_RADIUS_REQUIRED) y la base (ck_validators_location_point).
    latitude, longitude = cast(float, validator.latitude), cast(float, validator.longitude)
    radius = cast(int, validator.location_radius_m)
    if location is None:
        raise PermissionDeniedError(LOCATION_REQUIRED, code="LOCATION_REQUIRED", details={"radius_m": radius})

    accuracy = location.accuracy or 0.0
    if accuracy > settings.VALIDATOR_LOCATION_MAX_ACCURACY_M:
        raise PermissionDeniedError(
            LOCATION_INACCURATE.format(accuracy=format_distance(accuracy)),
            code="LOCATION_INACCURATE",
            details={"accuracy_m": round(accuracy), "max_accuracy_m": settings.VALIDATOR_LOCATION_MAX_ACCURACY_M},
        )
    distance = distance_m(latitude, longitude, location.latitude, location.longitude)
    if distance - min(accuracy, settings.VALIDATOR_LOCATION_TOLERANCE_M) > radius:
        raise PermissionDeniedError(
            LOCATION_OUT_OF_RANGE.format(distance=format_distance(distance), radius=format_distance(radius)),
            code="LOCATION_OUT_OF_RANGE",
            details={"distance_m": round(distance), "radius_m": radius},
        )
