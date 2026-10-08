"""Bitácora del motor (antifraude fase 3, I+D §3.5): anota cada cambio de versión de los componentes que mueven las
señales, para que la línea base de la deriva nunca mezcle versiones incompatibles.

- Lo que este proceso SABE de sí mismo: la versión del motor de riesgo (`risk_rules.ENGINE_VERSION`), la huella de los
  modelos faciales (el SHA-256 de los SHA-256 de cada modelo: cambia si cambia cualquiera) y la versión de la API.
- Lo que OBSERVA: la compilación de la aplicación web (`app_version` de la telemetría del navegador, de usuarios con
  sesión). Se anota en memoria, acotada, sin tocar la base dentro de la petición (regla 18); el mantenimiento la
  escribe al calcular la deriva. Cada réplica observa lo suyo y lo escribe cuando le toca el mantenimiento: una
  versión nueva de la aplicación queda anotada en la primera vuelta que la vea (nunca de más: `noted` dice qué ya
  escribió este proceso).

Escribir es idempotente: solo se agrega una fila cuando la última anotada del componente es distinta.
"""

import hashlib
import threading
from datetime import datetime

from sqlalchemy.orm import Session

from app.facial_recognition.model_store import BUNDLED_MODELS, MODELS
from app.repositories.drift_repository import DriftRepository
from app.services.risk_rules import ENGINE_VERSION

#: Componentes de la bitácora. Los dos primeros vuelven una ventana no comparable con la anterior.
RISK_ENGINE = "risk_engine"
FACE_MODELS = "face_models"
API = "api"
WEBAPP = "webapp"
COMPONENTS = (RISK_ENGINE, FACE_MODELS, API, WEBAPP)
INCOMPATIBLE = (RISK_ENGINE, FACE_MODELS)
#: Versión de la API (la de `app/main.py`; aquí para no importar la aplicación completa desde el mantenimiento).
API_VERSION = "1.0.0"
#: Compilaciones de la aplicación web que este proceso vio y aún no anota (acotadas: una versión es un texto corto).
_OBSERVED_MAX = 20

_lock = threading.Lock()
_observed: set[str] = set()


def face_models_fingerprint() -> str:
    """Huella de los modelos faciales: el SHA-256 de los SHA-256 declarados de cada modelo (descargados e incluidos)."""
    digest = hashlib.sha256()
    for model in (*MODELS, *BUNDLED_MODELS):
        digest.update(model.sha256.encode())
    return digest.hexdigest()[:16]


def observe_webapp(version: str | None) -> None:
    """Una compilación de la aplicación web vista en la telemetría (memoria; la escribe el mantenimiento)."""
    if not version:
        return
    with _lock:
        if len(_observed) < _OBSERVED_MAX:
            _observed.add(version[:120])


def pending_webapp() -> set[str]:
    """Las compilaciones observadas (pruebas)."""
    with _lock:
        return set(_observed)


def clear() -> None:
    """Olvida lo observado (pruebas)."""
    with _lock:
        _observed.clear()


def note(db: Session, component: str, version: str, now: datetime) -> bool:
    """Anota la versión si es distinta de la última anotada; True si agregó una fila."""
    repo = DriftRepository(db)
    if repo.latest_version(component) == version:
        return False
    repo.add_version(component, version, now)
    return True


def record_versions(db: Session, now: datetime) -> int:
    """Anota lo que este proceso sabe y lo que observó; devuelve cuántas filas nuevas (sin `commit`)."""
    added = sum(
        (
            note(db, RISK_ENGINE, ENGINE_VERSION, now),
            note(db, FACE_MODELS, face_models_fingerprint(), now),
            note(db, API, API_VERSION, now),
        )
    )
    with _lock:
        pending = set(_observed)
        _observed.clear()
    for version in sorted(pending):
        added += note(db, WEBAPP, version, now)
    return added
