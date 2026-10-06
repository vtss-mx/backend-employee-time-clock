"""Ubicación permitida de los validadores: solo operan dentro del radio de su domicilio.

La empresa activa "requiere ubicación" en el validador y fija el punto en el mapa y el radio. El dispositivo envía su
ubicación (la del navegador) y aquí se decide, con UNA regla pura (`check`) para los dos momentos:

- Al iniciar sesión (`ensure_location_allowed`), siempre exigida:
  - sin ubicación → 403 `LOCATION_REQUIRED` (la webapp la pide con el aviso nativo y reintenta);
  - lectura demasiado imprecisa (o sin precisión) → 403 `LOCATION_INACCURATE`;
  - fuera del radio → 403 `LOCATION_OUT_OF_RANGE`, con la distancia y el radio en `details`.
- En cada identificación (antifraude 2b, `validator_presence`): los mismos códigos si la política la exige
  (`validator_location = ENFORCE`); con «Solo medir» cada problema es una señal del motor de riesgo
  (`VALIDATOR_LOCATION_MISSING`, `VALIDATOR_LOCATION_INACCURATE`, `VALIDATOR_OUT_OF_ZONE`).

A la distancia se le descuenta la precisión informada por el dispositivo, hasta un margen máximo
(`VALIDATOR_LOCATION_TOLERANCE_M`), para no rechazar a quien está en el borde por el error del GPS.
"""

from dataclasses import dataclass
from typing import Any, cast

from app.core.config import settings
from app.core.exceptions import PermissionDeniedError
from app.core.geo import distance_m
from app.models import User, UserRole, Validator
from app.schemas.auth import DeviceLocation

#: Llaves del mensaje de cada problema según el momento (la regla y el código son los mismos).
LOGIN_KEYS = {"LOCATION_REQUIRED": "LOCATION_REQUIRED", "LOCATION_OUT_OF_RANGE": "LOCATION_OUT_OF_RANGE"}
IDENTIFY_KEYS = {
    "LOCATION_REQUIRED": "CHECKPOINT_LOCATION_REQUIRED",
    "LOCATION_OUT_OF_RANGE": "CHECKPOINT_LOCATION_OUT_OF_RANGE",
}


def format_distance(meters: float) -> str:
    """350 m · 1.2 km · 15 km (igual en es-MX y en en-US: los dos usan el punto decimal y el sistema métrico, como
    `formatDistance` de la aplicación web)."""
    if meters < 1000:
        return f"{round(meters)} m"
    km = meters / 1000
    return f"{km:.1f} km" if km < 10 else f"{round(km)} km"


@dataclass(frozen=True)
class LocationVerdict:
    """Lo que se decidió de una ubicación: el problema (código del rechazo; None si está en su lugar), los datos del
    mensaje y del detalle, y la distancia medida (None sin ubicación)."""

    code: str | None = None
    key: str | None = None
    params: dict[str, Any] | None = None
    details: dict[str, Any] | None = None
    distance: float | None = None
    radius: int = 0

    def error(self, keys: dict[str, str]) -> PermissionDeniedError:
        """El 403 de este problema, con la llave de su momento (iniciar sesión o identificar)."""
        code = cast(str, self.code)
        return PermissionDeniedError(code=code, key=self.key or keys[code], params=self.params, details=self.details)


def check(validator: Validator, location: DeviceLocation | None) -> LocationVerdict:
    """¿El dispositivo está dentro del radio del validador? (solo validadores que requieren ubicación)."""
    # Exigir ubicación implica punto y radio: lo validan el alta y la edición del validador
    # (LOCATION_POINT_REQUIRED / LOCATION_RADIUS_REQUIRED) y la base (ck_validators_location_point).
    latitude, longitude = cast(float, validator.latitude), cast(float, validator.longitude)
    radius = cast(int, validator.location_radius_m)
    if location is None:
        return LocationVerdict("LOCATION_REQUIRED", details={"radius_m": radius}, radius=radius)
    distance = distance_m(latitude, longitude, location.latitude, location.longitude)
    # Sin precisión no se sabe qué tan cerca está: nunca se supone perfecta (0 m). El navegador siempre
    # la informa; solo una llamada armada a mano la omite.
    if location.accuracy is None:
        return LocationVerdict(
            "LOCATION_INACCURATE",
            key="LOCATION_ACCURACY_MISSING",
            details={"accuracy_m": None},
            distance=distance,
            radius=radius,
        )
    accuracy = location.accuracy
    if accuracy > settings.VALIDATOR_LOCATION_MAX_ACCURACY_M:
        return LocationVerdict(
            "LOCATION_INACCURATE",
            key="DEVICE_LOCATION_INACCURATE",
            params={"accuracy": format_distance(accuracy)},
            details={"accuracy_m": round(accuracy), "max_accuracy_m": settings.VALIDATOR_LOCATION_MAX_ACCURACY_M},
            distance=distance,
            radius=radius,
        )
    if distance - min(accuracy, settings.VALIDATOR_LOCATION_TOLERANCE_M) > radius:
        return LocationVerdict(
            "LOCATION_OUT_OF_RANGE",
            params={"distance": format_distance(distance), "radius": format_distance(radius)},
            details={"distance_m": round(distance), "radius_m": radius},
            distance=distance,
            radius=radius,
        )
    return LocationVerdict(distance=distance, radius=radius)


def ensure_location_allowed(user: User, location: DeviceLocation | None) -> None:
    """Valida la ubicación del dispositivo al iniciar sesión (solo validadores que la requieren)."""
    validator = user.validator if user.role == UserRole.VALIDATOR else None
    if validator is None or not validator.location_required:
        return
    verdict = check(validator, location)
    if verdict.code is not None:
        raise verdict.error(LOGIN_KEYS)
