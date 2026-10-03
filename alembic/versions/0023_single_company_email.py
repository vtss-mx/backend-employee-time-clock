"""Un solo correo por empresa: se elimina tenancy.companies.contact_email

Al dar de alta una empresa se pide un solo correo, el de su administrador (auth.users), que es
también su contacto. Guardar otro correo en la empresa duplicaba el dato y podía quedar
desactualizado; el correo de la empresa son los de sus administradores.

La reversión vuelve a crear la columna y la llena con el correo del administrador más antiguo.

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-02 14:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("companies", "contact_email", schema="tenancy")


def downgrade() -> None:
    op.add_column("companies", sa.Column("contact_email", sa.String(length=255), nullable=True), schema="tenancy")
    op.execute(
        "UPDATE tenancy.companies c SET contact_email = (SELECT u.email FROM auth.users u "
        "WHERE u.company_id = c.id AND u.role = 'COMPANY' ORDER BY u.created_at, u.id LIMIT 1)"
    )
