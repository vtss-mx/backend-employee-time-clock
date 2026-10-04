"""Sondas de salud (públicas: sin detalle interno; el detalle lo ve el ADMIN en
`GET /api/admin/errors/server`).

- /health/live:  el proceso responde (lo usa Docker; no depende de servicios externos,
                 así una caída de la BD no provoca reinicios en cascada).
- /health/ready: dependencias listas (BD y motor facial). 503 si la BD falla.
- /health:       alias de /health/ready.
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.core.responses import ApiResponse, envelope_response, ok
from app.services import health_service

router = APIRouter(prefix="/health", tags=["Salud"])


@router.get("/live", response_model=ApiResponse[dict], summary="Liveness: el proceso responde")
def live() -> ApiResponse[dict]:
    return ok({"status": "ok"}, "El servicio está en ejecución", code="ALIVE")


@router.get("/ready", response_model=ApiResponse[dict], summary="Readiness: BD y motor facial")
@router.get("", response_model=ApiResponse[dict], summary="Estado del servicio (alias de /ready)")
def ready() -> JSONResponse:
    data = health_service.readiness()
    status = data["status"]
    message = health_service.MESSAGES[status]
    if status == "unavailable":
        return envelope_response(
            503,
            "SERVICE_UNAVAILABLE",
            message,
            data=data,
            errors=[{"code": "DATABASE_UNAVAILABLE", "message": message}],
            headers={"Retry-After": "5"},
        )
    return envelope_response(200, "READY" if status == "ok" else "DEGRADED", message, data=data)
