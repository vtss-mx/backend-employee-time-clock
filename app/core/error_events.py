"""Un error del backend tal como se registra en ops.error_reports (lo usan el registro y su repositorio)."""

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

#: Tope del detalle técnico (stack trace) por reporte.
DETAIL_LIMIT = 20_000
#: Tope del contexto (petición y respuesta) de cada ocurrencia, ya en JSON.
CONTEXT_LIMIT = 200_000


def clip(text: str | None, limit: int) -> str | None:
    """Texto literal que cabe en su columna y sin caracteres nulos (PostgreSQL los rechaza en `text`).

    Los mensajes y el detalle se guardan tal cual: el ADMIN necesita el contexto completo para
    resolver (a quién le pasó, con qué datos). Lo único que nunca se guarda son secretos y archivos
    (`app/core/error_context.py`)."""
    return None if text is None else text.replace("\x00", "")[:limit]


@dataclass(frozen=True)
class ErrorEvent:
    source: str  # HTTP | LOG | WEBSOCKET
    severity: str  # CRITICAL | ERROR | WARNING
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
