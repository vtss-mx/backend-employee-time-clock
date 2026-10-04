"""Fallas de la aplicación web en "Errores del sistema" (origen CLIENT).

El navegador reporta solo lo que es una falla de la app y nadie más que el equipo puede corregir: una
pantalla que se rompió, un error inesperado sin capturar o una configuración de la plataforma que la
persona no puede arreglar (p. ej. una API de Google Maps sin habilitar). Lo que la persona resuelve
(permisos de cámara o ubicación, sin conexión) y las respuestas de la API (el servidor ya registra
sus fallas) no se reportan: lo filtra la app.

El reporte llega sin sesión (el inicio de sesión también puede romperse) o con ella: si el token es
válido se anota quién y de qué empresa; uno vencido o inválido, o la BD sin responder, nunca hacen
fallar el reporte (se guarda sin cuenta). Se encola como cualquier error (`error_reporter`): la
petición no espera a la BD para guardarlo.
"""

import logging
import re
from collections.abc import Mapping
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.error_context import redact_text
from app.core.error_events import ErrorEvent, route_of
from app.core.exceptions import AppError
from app.core.request_context import request_id_var
from app.models import User
from app.schemas.client_error import ClientErrorIn, ClientErrorKind
from app.services.error_reporter import error_reporter
from app.services.session_service import SessionService

logger = logging.getLogger(__name__)

#: Código y gravedad de cada tipo: una pantalla rota o un error sin capturar es CRITICAL (la persona
#: no pudo seguir); una configuración faltante es ERROR (la app sigue, a mano).
_KINDS: dict[ClientErrorKind, tuple[str, str]] = {
    "CRASH": ("CLIENT_CRASH", "CRITICAL"),
    "UNHANDLED": ("CLIENT_UNHANDLED", "CRITICAL"),
    "CONFIG": ("CLIENT_CONFIG", "ERROR"),
}
#: `TypeError: x is not a function` → `TypeError`: el tipo agrupa los reportes; el mensaje no.
_EXCEPTION_TYPE = re.compile(r"^([\w$]*(?:Error|Exception)):\s")
_USER_AGENT_LIMIT = 500


def exception_type_of(message: str) -> str | None:
    """El tipo del error si el mensaje empieza con él (`Tipo: mensaje`, como lo escribe JavaScript)."""
    found = _EXCEPTION_TYPE.match(message)
    return found.group(1) if found else None


def _clean(text: str | None) -> str | None:
    return redact_text(text) if text else None


class ClientErrorService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def record(self, report: ClientErrorIn, *, token: str | None, headers: Mapping[str, str], ip: str) -> None:
        """Encola la falla para el siguiente lote de "Errores del sistema", con su contexto literal
        (sin secretos): qué pantalla, qué versión de la app, qué navegador y, si hay sesión, quién."""
        user = self._reporter(token, headers)
        company = user.current_company if user else None
        company_id = company.id if company else None
        code, severity = _KINDS[report.kind]
        context: dict[str, Any] = {
            "client": {
                "kind": report.kind,
                "path": report.path,
                "component": _clean(report.component),
                "detail": _clean(report.detail),
                "app_version": report.app_version,
                "user_agent": headers.get("user-agent", "")[:_USER_AGENT_LIMIT] or None,
                "ip": ip,
            },
            "user": {"id": user.id, "email": user.email, "role": user.role.value} if user else None,
            "company_id": company_id,
        }
        error_reporter.report(
            ErrorEvent(
                source="CLIENT",
                severity=severity,
                code=code,
                message=redact_text(report.message),
                location=route_of(report.path),
                exception_type=exception_type_of(report.message),
                detail=_clean(report.stack),
                trace_id=request_id_var.get(),
                user_id=user.id if user else None,
                company_id=company_id,
                context=context,
            )
        )

    def _reporter(self, token: str | None, headers: Mapping[str, str]) -> User | None:
        """Quién reporta, con las mismas reglas que cualquier petición (`authenticate_access`). Es
        accesorio: sin sesión válida o sin BD, el reporte se guarda sin cuenta."""
        if not token:
            return None
        try:
            user, _session_id = SessionService(self.db).authenticate_access(token, headers)
        except AppError as exc:  # sesión vencida o cerrada: la falla de la app se registra igual
            logger.info("Reporte de la app con una sesión no válida (%s): se guarda sin cuenta", exc.code)
            return None
        except SQLAlchemyError:
            logger.warning("Sin BD para saber quién reportó la falla de la app: se guarda sin cuenta", exc_info=True)
            self.db.rollback()
            return None
        self.db.commit()  # fin de la lectura: la conexión vuelve al pool de inmediato
        return user
