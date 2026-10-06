"""Rendimiento visto desde el navegador (`POST /api/telemetry/web`): Web Vitals por pantalla, tareas largas y el
tiempo de cada petición tal como la vivió la persona.

Lo que llega se normaliza otra vez (el navegador ya manda plantillas, pero el servidor no confía): los ids de una
ruta o pantalla se vuelven `{id}`, cada petición se relaciona con su ruta real de la API (método + plantilla, la
misma llave que mide el servidor; lo que no corresponde a ninguna es `OTHER`) y todo se suma en memoria
(`perf_meter`) para el siguiente lote: el lote del navegador nunca escribe en la BD.

Sin una sesión válida solo se conserva lo de la pantalla de inicio de sesión (`PUBLIC_SCREENS`) y sus APIs
(`PUBLIC_API_PREFIX`): así nadie sin cuenta llena las estadísticas de otras pantallas.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from fastapi import APIRouter
from fastapi.routing import APIRoute

from app.core.config import settings
from app.core.error_events import route_of
from app.core.exceptions import UnprocessableError
from app.core.perf_meter import OTHER, perf_meter
from app.models import PerfKind
from app.schemas.performance import WebPerfBatch, WebPerfResult, WebSample

#: Pantallas de la aplicación web que existen sin sesión.
PUBLIC_SCREENS: Final = frozenset({"/login"})
#: Las APIs que usa una pantalla sin sesión (iniciar sesión, cuenta recordada, renovar).
PUBLIC_API_PREFIX: Final = f"{settings.API_PREFIX}/auth/"
#: Tipo de cada muestra del navegador.
KINDS: Final = {
    "LCP": PerfKind.WEB_LCP,
    "INP": PerfKind.WEB_INP,
    "CLS": PerfKind.WEB_CLS,
    "FCP": PerfKind.WEB_FCP,
    "TTFB": PerfKind.WEB_TTFB,
    "LONG_TASK": PerfKind.WEB_LONG_TASK,
    "API": PerfKind.WEB_API,
}
#: El CLS es un puntaje sin unidad (0.1 es bueno): se guarda × 1000 para usar el mismo histograma que los tiempos.
CLS_SCALE: Final = 1000
_SCREEN = re.compile(r"^/[A-Za-z0-9._~/{}-]*$")
_API = re.compile(r"^(GET|POST|PUT|PATCH|DELETE) (/[A-Za-z0-9._~/{}-]*)$")
_PARAM = re.compile(r"\{[^}/]*\}")
#: Errores de una petición vista desde el navegador: sin red (0), tiempo agotado del cliente (408) y 5xx.
_NETWORK, _TIMEOUT = 0, 408


@dataclass(frozen=True)
class RouteIndex:
    """{"MÉTODO /api/plantilla-con-{id}": "MÉTODO /api/plantilla-real"}: la ruta de la API de cada petición del
    navegador sin recorrer las rutas en cada muestra (las plantillas reales tienen nombres: `{employee_id}`)."""

    routes: dict[str, str]

    def resolve(self, method: str, path: str) -> str:
        return self.routes.get(f"{method} {_template(path)}", OTHER)


def _template(path: str) -> str:
    """Ids → `{id}` y cualquier parámetro con nombre → `{id}` (`/api/employees/{employee_id}` y
    `/api/employees/12` dan lo mismo)."""
    return _PARAM.sub("{id}", route_of(path))


def route_index(routers: Iterable[APIRouter], prefix: str) -> RouteIndex:
    """El índice de las rutas de la API (los routers de `API_ROUTERS`, montados en `prefix`): lo arma `app/main.py`
    una vez al cargar (las rutas no cambian) y lo guarda en el estado de la aplicación."""
    found: dict[str, str] = {}
    for router in routers:
        for route in router.routes:
            if isinstance(route, APIRoute):
                path = f"{prefix}{route.path}"
                for method in (route.methods or set()) - {"HEAD"}:
                    found.setdefault(f"{method} {_template(path)}", f"{method} {path}")
    return RouteIndex(found)


def _screen(sample: WebSample, authenticated: bool) -> str | None:
    name = sample.name.split("?")[0].split("#")[0]
    if not _SCREEN.match(name):
        return None
    screen = _template(name)
    return screen if authenticated or screen in PUBLIC_SCREENS else None


def _api(sample: WebSample, authenticated: bool, index: RouteIndex) -> str | None:
    match = _API.match(sample.name.split("?")[0])
    if match is None:
        return None
    method, path = match.groups()
    if not authenticated and not path.startswith(PUBLIC_API_PREFIX):
        return None
    return index.resolve(method, path)


def _failure(status: int | None) -> tuple[bool, bool]:
    """(falla, rechazo) de una petición vista desde el navegador: sin red (0), tiempo agotado del cliente (408) y 5xx
    son fallas; cualquier otro 4xx, un rechazo normal. Sin código, ninguna de las dos."""
    if status is None:
        return False, False
    if status in (_NETWORK, _TIMEOUT) or status >= 500:
        return True, False
    return False, status >= 400


def record(batch: WebPerfBatch, *, authenticated: bool, index: RouteIndex) -> WebPerfResult:
    """Suma las muestras del lote al acumulador (memoria) y dice cuántas se tomaron en cuenta."""
    if len(batch.samples) > settings.PERF_WEB_MAX_SAMPLES:
        raise UnprocessableError(
            code="TOO_MANY_SAMPLES", params={"count": settings.PERF_WEB_MAX_SAMPLES}, field="samples"
        )
    accepted = 0
    for sample in batch.samples:
        kind = KINDS[sample.kind]
        if kind == PerfKind.WEB_API:
            name = _api(sample, authenticated, index)
            error, client_error = _failure(sample.status)
        else:
            name, error, client_error = _screen(sample, authenticated), False, False
        if name is None:
            continue
        value = sample.value * CLS_SCALE if kind == PerfKind.WEB_CLS else sample.value
        perf_meter.record(kind.value, name, value, error=error, client_error=client_error)
        accepted += 1
    return WebPerfResult(accepted=accepted, dropped=len(batch.samples) - accepted)
