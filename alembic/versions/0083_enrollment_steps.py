"""Los tres pasos independientes del registro facial (decisión del dueño del producto, 2026-10-07)

«Los procesos de orquestación del lado del frontend para la autenticación deben ser independientes: debe haber una
opción para tomar la foto, otra para el enrolamiento y otra para tomar el video y contestar las preguntas». El registro
facial del propio empleado deja de ser un flujo continuo en una sola pantalla y pasa a TRES opciones, cada una retomable
otro día, con el orden fijo exigido por el SERVIDOR (la 2 sin la 1 o la 3 sin la 2 responden 409 con su código):

1. **Foto inicial** (`POST /enrollment/photo`): antes se validaba (`/face/check`) y no quedaba nada. Ahora se guarda como
   BORRADOR en la tabla nueva `biometrics.face_enrollment_drafts`: la foto CIFRADA en el bucket (`STORED_IMAGES`,
   `FACE_ENROLLMENT_DRAFT_PHOTOS`; aquí solo su referencia: objeto, tipo, tamaño, SHA-256 y cuándo se subió) y su
   plantilla facial cifrada (`template_encrypted`, un vector de números como `face_embeddings.embedding_encrypted`), con
   `checked_at` y `expires_at` (`FACE_ENROLLMENT_DRAFT_HOURS`). Un empleado tiene a lo más un borrador (único
   `(company_id, employee_id)`, que también sirve a su búsqueda y a la FK compuesta); «Repetir foto» lo reemplaza.
   Índice `expires_at` para la depuración de lo vencido por lotes. Tabla de empresa: `company_id NOT NULL`, FK compuesta
   al empleado (`fk_face_enrollment_drafts_employee_company`, CASCADE) y seguridad por fila (`tenant_isolation`).
2. **Capturas y prueba de vida** (`POST /enrollment/face`): exige un borrador vigente y comprueba que las capturas son la
   MISMA persona que la foto inicial; al aceptarlas, la foto inicial pasa a ser la foto de referencia del registro
   (`face_enrollments.photo_object` toma su objeto cifrado, sin volver a subirlo) y la fila del borrador sale.
3. **Video con preguntas** (`POST /enrollment/voice/start`): emite la sesión de voz del registro pendiente cuantas veces
   haga falta, conservando las respuestas aceptadas (`enrollment_voice_answers`); sin tabla nueva.

El estado de los tres pasos lo da `GET /enrollment/progress` sin columnas nuevas (se deriva del borrador, del registro
pendiente y de sus respuestas). Compatible con la versión anterior en marcha (solo agrega una tabla). Los permisos de la
API sobre la tabla los pone `python -m app.cli db roles` en el mismo despliegue (servicio `migrate`). `downgrade` quita
la tabla (sus objetos del bucket quedan sin referencia: la regla de ciclo de vida o a mano) y sus comentarios con ella.

Revision ID: 0083
Revises: 0082
Create Date: 2026-10-07 18:00:00
"""

from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa

from alembic import op

revision: str = "0083"
down_revision: str | None = "0082"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
BIOMETRICS = "biometrics"
WORKFORCE = "workforce"
TABLE = "face_enrollment_drafts"
#: La seguridad por fila de la tabla nueva (la misma SQL que `row_security.policy_ddl`), en sentencias constantes.
ROW_SECURITY = (
    "ALTER TABLE biometrics.face_enrollment_drafts ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE biometrics.face_enrollment_drafts FORCE ROW LEVEL SECURITY",
    "CREATE POLICY tenant_isolation ON biometrics.face_enrollment_drafts "
    "USING (company_id = NULLIF(current_setting('app.company_id', true), '')::integer) "
    "WITH CHECK (company_id = NULLIF(current_setting('app.company_id', true), '')::integer)",
    "COMMENT ON COLUMN biometrics.face_enrollment_drafts.company_id IS 'Empresa dueña de la fila. Seguridad por fila "
    "(política tenant_isolation): solo se ve y se escribe en una transacción con app.company_id igual (o con el rol de "
    "la plataforma).'",
)


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("template_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("model_name", sa.String(length=50), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("detection_score", sa.Float(), nullable=False),
        sa.Column("quality_score", sa.Float(), nullable=False),
        sa.Column("photo_content_type", sa.String(length=30), nullable=True),
        sa.Column("photo_object", sa.String(length=300), nullable=True),
        sa.Column("photo_size", sa.Integer(), nullable=True),
        sa.Column("photo_sha256", sa.String(length=64), nullable=True),
        sa.Column("photo_uploaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["employee_id", "company_id"],
            [f"{WORKFORCE}.employees.id", f"{WORKFORCE}.employees.company_id"],
            name=f"fk_{TABLE}_employee_company",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{TABLE}")),
        sa.UniqueConstraint("company_id", "employee_id", name=f"uq_{TABLE}_employee"),
        schema=BIOMETRICS,
    )
    op.create_index(f"ix_{TABLE}_expires", TABLE, ["expires_at"], unique=False, schema=BIOMETRICS)
    for statement in ROW_SECURITY:
        op.execute(statement)
    op.get_bind().exec_driver_sql((SQL_DIR / "0083_comments.sql").read_text(encoding="utf-8").replace("%", "%%"))


def downgrade() -> None:
    op.drop_index(f"ix_{TABLE}_expires", table_name=TABLE, schema=BIOMETRICS)
    op.drop_table(TABLE, schema=BIOMETRICS)
