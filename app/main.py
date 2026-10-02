"""Punto de entrada de la API FastAPI."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
import anyio.to_thread
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from app.core.config import settings
from app.core.database import SessionLocal, wait_for_database
from app.core.exceptions import register_exception_handlers
from app.core.request_context import RequestIdLogFilter
from app.middleware.concurrency import ConcurrencyLimitMiddleware
from app.middleware.request_id import register_request_id_middleware
from app.middleware.security import register_security_middlewares
from app.routers import (
    admin,
    auth,
    checkpoint,
    employees,
    enrollments,
    face,
    health,
    realtime,
    users,
    validators,
    verification,
)
from app.routers import settings as settings_router
from app.services.bootstrap import ensure_first_admin, ensure_first_company

logging.basicConfig(
    level=settings.LOG_LEVEL.upper(),
    format="%(asctime)s %(levelname)s [%(name)s] [req=%(request_id)s] %(message)s",
)
for _handler in logging.getLogger().handlers:
    _handler.addFilter(RequestIdLogFilter())
logger = logging.getLogger("app")


def _configure_threadpool(face_slots: int) -> None:
    """Hilos para dependencias y endpoints síncronos.

    Debe haber MÁS hilos que peticiones admitidas a la vez (MAX_CONCURRENT_REQUESTS): cada
    petición usa como máximo un hilo a la vez, así siempre hay uno libre para que la petición
    que tiene una conexión de BD termine y la libere. Con menos hilos aparece el bloqueo
    clásico de FastAPI: todos los hilos esperan una conexión que nadie puede devolver.
    """
    limiter = anyio.to_thread.current_default_thread_limiter()
    wanted = settings.THREADPOOL_SIZE or (settings.MAX_CONCURRENT_REQUESTS + max(16, face_slots // 4))
    limiter.total_tokens = max(limiter.total_tokens, wanted)
    logger.info("Threadpool: %s hilos", limiter.total_tokens)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Arranque tolerante: si la BD aún no está lista se espera; si falla el bootstrap o la
    # carga de modelos, la API inicia igual y se recupera sola (readiness lo refleja).
    if wait_for_database(settings.DB_STARTUP_RETRIES):
        try:
            with SessionLocal() as db:
                ensure_first_admin(db)
                ensure_first_company(db)
        except Exception:
            logger.exception("No se pudieron verificar los usuarios iniciales")
    else:
        logger.error("La base de datos no respondió al iniciar; se reintentará en cada petición")
    # Precarga de modelos para que la primera verificación no sea lenta.
    try:
        from app.facial_recognition import face_pool

        stats = face_pool().stats()
        # Hilos del threadpool suficientes para workers + cola + resto de endpoints, de modo que
        # las peticiones en espera de un worker facial nunca bloqueen login, QR o administración.
        _configure_threadpool(stats.workers + stats.max_waiting)
        logger.info("Modelos de reconocimiento facial cargados")
    except Exception as exc:
        _configure_threadpool(0)
        logger.warning("No se pudieron cargar los modelos faciales (se reintentará bajo demanda): %s", exc)
    yield


app = FastAPI(
    title=settings.APP_NAME,
    version="1.0.0",
    description=(
        "API de gestión de empleados con verificación por reconocimiento facial y QR.\n\n"
        "**Autenticación**\n"
        "1. `POST /api/auth/login` → copia `data.access_token` (JWT ES256, vigencia 12 h).\n"
        "2. Pulsa **Authorize** y pégalo (sin el prefijo `Bearer`).\n"
        "3. El refresh token viaja en una cookie HttpOnly: `POST /api/auth/refresh` renueva el "
        "access token (rotación con detección de reutilización). `POST /api/auth/logout` cierra la "
        "sesión y la revoca de inmediato.\n\n"
        "**Contrato de respuesta**: todas las respuestas tienen `success`, `statusCode`, `code`, "
        "`message`, `data`, `errors`, `traceId` y `timestamp`."
    ),
    swagger_ui_parameters={"persistAuthorization": True, "displayRequestDuration": True, "filter": True},
    lifespan=lifespan,
    docs_url="/docs" if settings.DOCS_ENABLED else None,
    redoc_url="/redoc" if settings.DOCS_ENABLED else None,
    openapi_url="/openapi.json" if settings.DOCS_ENABLED else None,
)

register_security_middlewares(app)
# Admisión acotada (dentro del request-id para que los 503 lleven traceId).
app.add_middleware(
    ConcurrencyLimitMiddleware,
    max_concurrent=settings.MAX_CONCURRENT_REQUESTS,
    queue_timeout=settings.REQUEST_QUEUE_TIMEOUT_SECONDS,
)
register_request_id_middleware(app)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    # Credenciales: la cookie HttpOnly del refresh token (solo en /api/auth). Orígenes explícitos.
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "ngrok-skip-browser-warning", "X-Request-ID"],
    expose_headers=["X-Request-ID", "Retry-After"],
    max_age=600,
)
register_exception_handlers(app)

for router in (
    health.router,
    auth.router,
    admin.router,
    users.router,
    employees.router,
    enrollments.router,
    face.router,
    settings_router.router,
    realtime.router,
    verification.router,
    validators.router,
    checkpoint.router,
):
    app.include_router(router, prefix=settings.API_PREFIX)

if settings.DOCS_ENABLED:

    @app.get("/", include_in_schema=False)
    @app.get(f"{settings.API_PREFIX}/docs", include_in_schema=False)
    def docs_redirect() -> RedirectResponse:
        """La raíz del backend y /api/docs llevan a Swagger (/docs)."""
        return RedirectResponse("/docs")
