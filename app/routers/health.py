"""Sondas de salud (públicas: sin detalle interno; el detalle lo ve el ADMIN en
`GET /api/admin/errors/server`).

- /health/live:  el proceso responde (lo usa Docker; no depende de servicios externos,
                 así una caída de la BD no provoca reinicios en cascada).
- /health/ready: dependencias listas (BD y motor facial). 503 si la BD falla, y 503 `SHUTTING_DOWN` en
                 cuanto el proceso empieza a apagarse (drenado, `app/core/lifecycle.py`): el balanceador
                 deja de mandarle tráfico mientras termina lo que lleva.
- /health:       alias de /health/ready.
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.core.lifecycle import draining
from app.core.responses import ApiResponse, envelope_response, ok
from app.i18n import t
from app.services import health_service

router = APIRouter(prefix="/health", tags=["Salud"])


@router.get("/live", response_model=ApiResponse[dict], summary="Liveness: el proceso responde")
def live() -> ApiResponse[dict]:
    return ok({"status": "ok"}, code="ALIVE")


@router.get("/ready", response_model=ApiResponse[dict], summary="Readiness: BD y motor facial")
@router.get("", response_model=ApiResponse[dict], summary="Estado del servicio (alias de /ready)")
def ready() -> JSONResponse:
    if draining():  # sin consultar dependencias: lo único que importa es dejar de recibir tráfico
        message = t(health_service.MESSAGES["shutting_down"])
        return envelope_response(
            503,
            "SHUTTING_DOWN",
            message,
            data={"status": "shutting_down"},
            errors=[{"code": "SHUTTING_DOWN", "message": message}],
            headers={"Retry-After": "1"},
        )
    data = health_service.readiness()
    status = data["status"]
    message = t(health_service.MESSAGES[status])
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
