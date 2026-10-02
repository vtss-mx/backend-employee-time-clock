"""Sondas de salud.

- /health/live:  el proceso responde (lo usa Docker; no depende de servicios externos,
                 así una caída de la BD no provoca reinicios en cascada).
- /health/ready: dependencias listas (BD y motor facial). 503 si alguna falla.
- /health:       alias de /health/ready.
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.database import engine
from app.core.responses import ApiResponse, envelope_response, ok
from app.facial_recognition import face_engine_status

router = APIRouter(prefix="/health", tags=["Salud"])


@router.get("/live", response_model=ApiResponse[dict], summary="Liveness: el proceso responde")
def live() -> ApiResponse[dict]:
    return ok({"status": "ok"}, "El servicio está en ejecución", code="ALIVE")


@router.get("/ready", response_model=ApiResponse[dict], summary="Readiness: BD y motor facial")
@router.get("", response_model=ApiResponse[dict], summary="Estado del servicio (alias de /ready)")
def ready() -> JSONResponse:
    components: dict[str, dict] = {}
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        components["database"] = {"status": "ok"}
    except Exception as exc:
        components["database"] = {"status": "unavailable", "error": exc.__class__.__name__}
    components["face_engine"] = face_engine_status()

    database_ok = components["database"]["status"] == "ok"
    face_ok = components["face_engine"]["status"] == "ok"
    overall = "ok" if database_ok and face_ok else ("degraded" if database_ok else "unavailable")
    messages = {
        "ok": "Todos los componentes están disponibles",
        "degraded": "Servicio disponible con funciones limitadas (motor facial no disponible)",
        "unavailable": "La base de datos no está disponible",
    }
    data = {"status": overall, "components": components}
    if not database_ok:
        return envelope_response(
            503,
            "SERVICE_UNAVAILABLE",
            messages[overall],
            data=data,
            errors=[{"code": "DATABASE_UNAVAILABLE", "message": messages[overall]}],
            headers={"Retry-After": "5"},
        )
    return envelope_response(200, "READY" if overall == "ok" else "DEGRADED", messages[overall], data=data)
