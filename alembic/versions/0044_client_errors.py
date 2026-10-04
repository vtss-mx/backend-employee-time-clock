"""Fallas de la aplicación web en "Errores del sistema" (origen CLIENT)

El dueño del producto cambió qué se registra en `ops.error_reports`: solo lo que alguien tiene que
corregir (señal, no ruido). Las fallas del servidor siguen igual y se suman las de la aplicación web
que el navegador reporta en `POST /api/client-errors`: una pantalla que se rompió (ErrorBoundary),
un error inesperado sin capturar o una configuración de la plataforma que la persona no puede
arreglar (p. ej. una API de Google Maps sin habilitar). Para eso el CHECK del origen acepta `CLIENT`.

Los 4xx dejan de registrarse (es código, no estructura). Las filas WARNING que ya existen son
historial real: no se borran ni se modifican, y el catálogo `error_severities` se conserva (el ADMIN
las sigue filtrando y marcando).

Revision ID: 0044
Revises: 0043
Create Date: 2026-10-04 23:30:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Nombre completo del CHECK: con `op.f` la convención de nombres no le vuelve a poner el prefijo.
SOURCE_CHECK = "ck_error_reports_source"
SOURCES = "source IN ('HTTP', 'LOG', 'WEBSOCKET', 'CLIENT')"
SOURCES_0034 = "source IN ('HTTP', 'LOG', 'WEBSOCKET')"


def _replace_check(condition: str) -> None:
    op.drop_constraint(op.f(SOURCE_CHECK), "error_reports", schema="ops", type_="check")
    op.create_check_constraint(op.f(SOURCE_CHECK), "error_reports", condition, schema="ops")


def upgrade() -> None:
    _replace_check(SOURCES)


def downgrade() -> None:
    # El CHECK anterior rechaza CLIENT. Las fallas de la app web no se borran (son historial real):
    # quedan como LOG, el origen genérico de "lo registró un proceso".
    op.execute("UPDATE ops.error_reports SET source = 'LOG' WHERE source = 'CLIENT'")
    _replace_check(SOURCES_0034)
