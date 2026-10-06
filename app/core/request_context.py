"""Contexto de cada petición: su identificador (X-Request-ID = traceId) y lo que se sabe de ella
para registrar sus errores (quién la hizo y con qué error respondió)."""

import logging
from contextvars import ContextVar
from dataclasses import dataclass, field

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


@dataclass
class RequestInfo:
    """Mutable a propósito: los hilos del threadpool reciben una COPIA del contexto, pero el mismo
    objeto; lo que anotan (usuario, error) lo ve el middleware al terminar la petición."""

    user_id: int | None = None
    company_id: int | None = None
    #: Correo y rol tal cual al momento del error (aunque la cuenta cambie después).
    user_email: str | None = None
    user_role: str | None = None
    #: Método y ruta de la petición (también para errores del log que ocurren dentro de ella).
    method: str | None = None
    path: str | None = None
    #: (estado HTTP, código, mensaje) del error con que se respondió.
    error: tuple[int, str, str] | None = None
    #: Datos libres para el detalle técnico (p. ej. el campo inválido).
    extra: dict[str, str] = field(default_factory=dict)


request_info_var: ContextVar[RequestInfo | None] = ContextVar("request_info", default=None)


def note_error(status_code: int, code: str, message: str) -> None:
    """Anota el error con que responde la petición en curso (lo registra el middleware al final)."""
    info = request_info_var.get()
    if info is not None:
        info.error = (status_code, code, message)


def note_actor(user_id: int, company_id: int | None, *, email: str | None = None, role: str | None = None) -> None:
    """Quién hizo la petición (por si termina en error): su cuenta, correo, rol y empresa."""
    info = request_info_var.get()
    if info is not None:
        info.user_id, info.company_id, info.user_email, info.user_role = user_id, company_id, email, role


def note_company(company_id: int) -> None:
    """La empresa de una petición sin sesión de usuario (la llave de la API de integración): su consumo
    se le cuenta a ella (y sus fallas dicen de qué empresa fueron)."""
    info = request_info_var.get()
    if info is not None:
        info.company_id = company_id


class RequestIdLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get() or "-"
        return True
