"""Punto de entrada de la API FastAPI."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
import anyio.to_thread
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from app.core.admission import admission
from app.core.config import settings
from app.core.database import SessionLocal, wait_for_database
from app.core.exceptions import register_exception_handlers
from app.core.request_context import RequestIdLogFilter
from app.middleware.admission import AdaptiveAdmissionMiddleware
from app.middleware.request_id import register_request_id_middleware
from app.middleware.security import register_security_middlewares
from app.routers import (
    admin,
    admin_errors,
    admin_face_security,
    api_keys,
    attendance,
    auth,
    calendar,
    catalogs,
    checkpoint,
    client_errors,
    departments,
    employees,
    enrollments,
    face,
    health,
    integrations,
    realtime,
    shifts,
    sites,
    users,
    validation,
    validators,
    verification,
)
from app.routers import settings as settings_router
from app.services.bootstrap import ensure_first_admin, ensure_first_company
from app.services.error_reporter import ErrorReportFlusher, install_log_handler
from app.services.maintenance_service import MaintenanceScheduler

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
    # Registro de errores PRIMERO: todo error del log (también los del arranque) entra a
    # ops.error_reports, guardado en lotes (si la BD aún no está, se guarda cuando vuelva).
    install_log_handler()
    flusher = ErrorReportFlusher(settings.ERROR_REPORT_FLUSH_SECONDS) if settings.ERROR_REPORT_FLUSH_SECONDS else None
    if flusher:
        flusher.start()
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
        # Error (no aviso): sin modelos no hay reconocimiento facial; el ADMIN debe enterarse.
        logger.error("No se pudieron cargar los modelos faciales (se reintentará bajo demanda): %s", exc)
    # Depuración de lo vencido en segundo plano (fuera de las peticiones; una instancia a la vez).
    scheduler = (
        MaintenanceScheduler(settings.MAINTENANCE_INTERVAL_SECONDS) if settings.MAINTENANCE_INTERVAL_SECONDS else None
    )
    if scheduler:
        scheduler.start()
    yield
    if scheduler:
        scheduler.stop()
    if flusher:
        flusher.stop()  # guarda lo pendiente antes de apagar


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
# Admisión adaptativa: límite que sigue a la capacidad real y fila con prioridad por nivel y demanda
# (dentro del request-id para que los 503 lleven traceId).
app.add_middleware(AdaptiveAdmissionMiddleware, controller=admission, api_prefix=settings.API_PREFIX)
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

#: Todos los routers de la API (la prueba de autorización recorre cada una de sus rutas).
API_ROUTERS = (
    health.router,
    auth.router,
    admin.router,
    admin_errors.router,
    admin_face_security.router,
    users.router,
    employees.router,
    departments.router,
    enrollments.router,
    face.router,
    settings_router.router,
    realtime.router,
    verification.router,
    validators.router,
    checkpoint.router,
    catalogs.router,
    validation.router,
    api_keys.router,
    integrations.router,
    sites.router,
    shifts.router,
    attendance.company_router,
    attendance.employee_router,
    calendar.company_router,
    calendar.employee_router,
    client_errors.router,
)
for router in API_ROUTERS:
    app.include_router(router, prefix=settings.API_PREFIX)


def docs_redirect() -> RedirectResponse:
    """La raíz del backend y /api/docs llevan a Swagger (/docs)."""
    return RedirectResponse("/docs")


def register_docs_redirects(target: FastAPI, enabled: bool) -> None:
    """Atajos a la documentación, solo si está publicada (DOCS_ENABLED): sin ella, la raíz y /api/docs
    responden 404 como cualquier ruta inexistente (no delatan que hubo documentación)."""
    if not enabled:
        return
    for path in ("/", f"{settings.API_PREFIX}/docs"):
        target.add_api_route(path, docs_redirect, methods=["GET"], include_in_schema=False)


register_docs_redirects(app, settings.DOCS_ENABLED)
