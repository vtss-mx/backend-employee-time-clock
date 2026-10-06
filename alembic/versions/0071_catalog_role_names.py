"""Nombres de los roles en español, «Panel» como inicio de la empresa y la gravedad WARNING como historial

Seguimiento del repaso de textos «simple pero profesional» (`backend-employee-time-clock/AGENTS.md` §11.5):

- **Roles** (`catalog.roles.name`, es-MX): el español mostraba los nombres en inglés («Admin», «Company», «Employee»,
  «Validator») en el menú, la insignia de Mi perfil, el popup de cerrar sesión, el consumo por usuario y el «correo
  (Rol)» de los errores del sistema. Pasan a «Administrador», «Empresa», «Empleado» y «Validador». Para el ADMIN se
  elige «Administrador» y no «Administrador de la plataforma»: el menú lo muestra en mayúsculas bajo «Consola de la
  plataforma» (el nombre largo no cabe en una línea y repite «plataforma») y es el mismo nombre que "Admin" en en-US.
  El inglés no cambia.
- **Inicio de la empresa** (`catalog.screens`, `COMPANY_DASHBOARD`): «Dashboard» → «Panel», como el del ADMIN. En
  en-US los dos ya son "Dashboard".
- **Gravedad WARNING** (`catalog.error_severities`): su descripción decía que se registran los 4xx, y la regla 4 del
  `AGENTS.md` raíz dice lo contrario (solo las fallas se registran). Las filas WARNING que ya existen quedan solo como
  historial; la descripción lo dice en los dos idiomas.

El español va en la columna de cada catálogo y el inglés en `catalog.translations` (§11.3). Cada texto lleva aquí su
versión anterior y la nueva (no se leen del seed: una migración posterior que vuelva a cambiar el mismo texto no altera
lo que esta aplica), así que `downgrade` deja exactamente lo que había. Sin SQL escrita: las sentencias son expresiones
de SQLAlchemy y los textos y códigos viajan como parámetros (regla 21 de la raíz).

Revision ID: 0071
Revises: 0070
Create Date: 2026-10-06 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0071"
down_revision: str | None = "0070"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CATALOG = "catalog"
LOCALE = "en-US"

#: (catálogo, código, columna) → ((español anterior, español nuevo), (inglés anterior, inglés nuevo)). Un idioma que no
#: cambia lleva el mismo texto en los dos lados (no se toca su fila).
TEXTS: dict[tuple[str, str, str], tuple[tuple[str, str], tuple[str, str]]] = {
    ("roles", "ADMIN", "name"): (("Admin", "Administrador"), ("Admin", "Admin")),
    ("roles", "COMPANY", "name"): (("Company", "Empresa"), ("Company", "Company")),
    ("roles", "EMPLOYEE", "name"): (("Employee", "Empleado"), ("Employee", "Employee")),
    ("roles", "VALIDATOR", "name"): (("Validator", "Validador"), ("Validator", "Validator")),
    ("screens", "COMPANY_DASHBOARD", "name"): (("Dashboard", "Panel"), ("Dashboard", "Dashboard")),
    ("error_severities", "WARNING", "description"): (
        (
            "Solicitud rechazada: validación, permisos o reglas de negocio (4xx).",
            "Solo historial: solicitudes rechazadas (4xx) registradas antes. Ya no se registran.",
        ),
        (
            "Request rejected: validation, permissions, or business rules (4xx).",
            "History only: rejected requests (4xx) recorded in the past. They're no longer recorded.",
        ),
    ),
}

_TRANSLATIONS = sa.table(
    "translations",
    sa.column("catalog"),
    sa.column("code"),
    sa.column("locale"),
    sa.column("field"),
    sa.column("text"),
    schema=CATALOG,
)


def _apply(new: bool) -> None:
    """Cada texto en español (columna del catálogo) y en inglés (`catalog.translations`); una sentencia por texto."""
    pick = 1 if new else 0
    for (catalog, code, column), (spanish, english) in TEXTS.items():
        if spanish[0] != spanish[1]:
            table = sa.table(catalog, sa.column("code"), sa.column(column), schema=CATALOG)
            op.execute(sa.update(table).where(table.c.code == code).values({column: spanish[pick]}))
        if english[0] != english[1]:
            op.execute(
                sa.update(_TRANSLATIONS)
                .where(
                    _TRANSLATIONS.c.catalog == catalog,
                    _TRANSLATIONS.c.code == code,
                    _TRANSLATIONS.c.locale == LOCALE,
                    _TRANSLATIONS.c.field == column,
                )
                .values(text=english[pick])
            )


def upgrade() -> None:
    _apply(new=True)


def downgrade() -> None:
    _apply(new=False)
