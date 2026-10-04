"""Un error tal como se registra en ops.error_reports (lo usan el registro y su repositorio), y qué
se registra.

Qué se guarda (decisión del dueño del producto): solo las fallas que alguien tiene que corregir,
para que la bandeja del ADMIN sea señal y no ruido.

- Sí: excepciones no controladas (500, CRITICAL), fallas controladas del servidor o de una
  dependencia (5xx, ERROR: BD caída o lenta, motor facial, saturación), los `logger.error` /
  `logger.exception` de cualquier proceso, las fallas del canal WebSocket del lado del servidor y las
  fallas de la aplicación web que reporta el navegador (`CLIENT`).
- No: los 4xx (validación, permisos, reglas de negocio, 404, 401, 429...). Son resultados normales
  que se responden con su código estable y quedan en el log del proceso; registrarlos llenaba la
  bandeja de lo que nadie debe corregir y escondía lo que sí.
"""

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

#: Tope del detalle técnico (stack trace) por reporte.
DETAIL_LIMIT = 20_000
#: Tope del contexto (petición y respuesta) de cada ocurrencia, ya en JSON.
CONTEXT_LIMIT = 200_000
#: Las únicas gravedades que se guardan: fallas del servidor (y de la app). WARNING (un 4xx) no.
RECORDED_SEVERITIES = frozenset({"CRITICAL", "ERROR"})
_ID_SEGMENT = re.compile(r"^(\d+|[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f-]{27})$")


def clip(text: str | None, limit: int) -> str | None:
    """Texto literal que cabe en su columna y sin caracteres nulos (PostgreSQL los rechaza en `text`).

    Los mensajes y el detalle se guardan tal cual: el ADMIN necesita el contexto completo para
    resolver (a quién le pasó, con qué datos). Lo único que nunca se guarda son secretos y archivos
    (`app/core/error_context.py`)."""
    return None if text is None else text.replace("\x00", "")[:limit]


@dataclass(frozen=True)
class ErrorEvent:
    source: str  # HTTP | LOG | WEBSOCKET | CLIENT (la aplicación web)
    severity: str  # CRITICAL | ERROR (WARNING ya no se guarda: ver RECORDED_SEVERITIES)
    code: str
    message: str
    http_status: int | None = None
    method: str | None = None
    location: str | None = None
    exception_type: str | None = None
    detail: str | None = None
    trace_id: str | None = None
    user_id: int | None = None
    company_id: int | None = None
    #: Contexto literal (petición, respuesta, usuario...). No cuenta para la huella.
    context: dict[str, Any] | None = None
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def fingerprint(self) -> str:
        """Qué hace "el mismo error": origen, código, estado, método, ruta y tipo de excepción (no el
        mensaje, que puede llevar datos de cada caso)."""
        parts = (self.source, self.code, self.http_status, self.method, self.location, self.exception_type)
        return hashlib.sha256("|".join(str(p or "") for p in parts).encode()).hexdigest()


def severity_for(status_code: int) -> str:
    if status_code >= 500:
        return "CRITICAL" if status_code == 500 else "ERROR"
    return "WARNING"


def is_recorded(severity: str) -> bool:
    """¿Va a la bandeja del ADMIN? Solo las fallas (CRITICAL y ERROR); un 4xx (WARNING) se queda en
    el log del proceso."""
    return severity in RECORDED_SEVERITIES


def route_of(path: str) -> str:
    """`/api/employees/12/qr` → `/api/employees/{id}/qr`: el mismo error en otro registro es el mismo
    (también para las rutas de la aplicación web: `/company/employees/12/edit`)."""
    return "/".join("{id}" if _ID_SEGMENT.match(part) else part for part in path.split("/"))[:255]
