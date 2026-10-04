"""Estado del servicio: dependencias (BD y motor facial) y capacidad de este proceso.

Dos vistas de lo mismo:
- `readiness()`: lo que ven las sondas públicas (Docker, balanceador): solo si cada componente está
  listo. Sin textos de error ni qué APIs se usan más (eso describe el sistema a cualquiera).
- `server_status()`: el detalle para el ADMIN de la plataforma (pantalla "Errores del sistema"):
  el error de cada componente, la fila del motor facial y el control de admisión adaptativo.
"""

import threading
import time
from typing import Any

from sqlalchemy import text

from app.core.admission import admission
from app.core.database import engine
from app.facial_recognition import face_engine_status

#: La revisión de dependencias se reutiliza unos segundos: muchas sondas (balanceador, orquestador,
#: monitoreo) no abren una conexión a la BD cada una.
_READY_CACHE_SECONDS = 2.0
_ready_lock = threading.Lock()
_ready_cache: tuple[float, dict[str, dict[str, Any]]] | None = None

MESSAGES = {
    "ok": "Todos los componentes están disponibles",
    "degraded": "Servicio disponible con funciones limitadas (motor facial no disponible)",
    "unavailable": "La base de datos no está disponible",
}


def components() -> dict[str, dict[str, Any]]:
    """BD y motor facial, con su detalle (en caché unos segundos)."""
    global _ready_cache
    with _ready_lock:
        if _ready_cache and time.monotonic() - _ready_cache[0] < _READY_CACHE_SECONDS:
            return _ready_cache[1]
    found: dict[str, dict[str, Any]] = {}
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        found["database"] = {"status": "ok"}
    except Exception as exc:  # cualquier falla de la BD es "no disponible", nunca un 500 de la sonda
        found["database"] = {"status": "unavailable", "error": exc.__class__.__name__}
    found["face_engine"] = face_engine_status()
    with _ready_lock:
        _ready_cache = (time.monotonic(), found)
    return found


def clear_cache() -> None:
    """La siguiente consulta revisa las dependencias de nuevo (pruebas)."""
    global _ready_cache
    with _ready_lock:
        _ready_cache = None


def overall(found: dict[str, dict[str, Any]]) -> str:
    """ok, degraded (sin motor facial: QR y lo administrativo siguen) o unavailable (sin BD)."""
    if found["database"]["status"] != "ok":
        return "unavailable"
    return "ok" if found["face_engine"]["status"] == "ok" else "degraded"


def readiness() -> dict[str, Any]:
    """Vista pública: estado general y de cada componente, sin detalle."""
    found = components()
    return {
        "status": overall(found),
        "components": {name: {"status": item["status"]} for name, item in found.items()},
    }


def server_status() -> dict[str, Any]:
    """Vista del ADMIN: todo el detalle de los componentes y la capacidad adaptativa del proceso."""
    found = components()
    return {"status": overall(found), "components": found, "admission": admission.snapshot()}
