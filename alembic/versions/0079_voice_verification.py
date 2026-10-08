"""Verificación por voz y video del registro facial (decisión del dueño del producto, 2026-10-06)

Flujo del registro facial del empleado, en este orden y sin alterarlo: foto inicial válida → 32 fotos VÁLIDAS con los
movimientos → video con tres preguntas al azar sobre sus propios datos → validación de cada respuesta → fin. La voz se
transcribe en el servidor (faster-whisper; el audio nunca sale de la plataforma) y se compara con el dato registrado; el
rostro del video, con las fotos del registro. La empresa revisa el video al validar; el ADMIN no lo ve.

- `tenancy.verification_policy.voice_verification` (nuevo, encendido por omisión): la empresa lo exige en el registro
  propio; apagarlo relaja la seguridad (regla de dos personas).
- `biometrics.face_enrollments`: `voice_required` (la política lo exigía al enviar las fotos), `voice_passed_at` (la
  última respuesta pasó: hasta entonces el registro no llega a la empresa ni el empleado a «en validación») y
  `voice_attempts` (respuestas que no pasaron, contadas aquí y no en el token del cliente). Índice parcial
  `ix_face_enrollments_voice_pending` (`submitted_at` de los que esperan su voz): la depuración de los abandonados.
- `biometrics.enrollment_voice_answers` (nueva, de empresa): una fila por respuesta ACEPTADA con la pregunta, los intentos,
  lo oído, los parecidos medidos y la REFERENCIA de su clip, que vive CIFRADO en el bucket (`STORED_IMAGES`,
  `ENROLLMENT_VOICE_CLIPS`), nunca en la base. Seguridad por fila como toda tabla de empresa; único
  `(company_id, enrollment_id, position)` (también el índice del detalle), `(company_id, employee_id)` (el borrado real
  al eliminar al empleado) y `created_at` (la retención de `FACE_VIDEO_RETENTION_DAYS`). FK compuesta al registro
  (se va con él) y a `catalog.voice_questions`.
- `catalog.voice_questions` (nuevo): las seis preguntas posibles (nombre completo, fecha de nacimiento, empresa, número
  de empleado, departamento y sitio; las tres últimas solo si el empleado tiene el dato) con su texto para la app y lo
  que la empresa compara; `catalog.enrollment_flags`: `VOICE_RETRIES` y `VIDEO_FACE_MISMATCH` (marcas para el revisor).
  Sus traducciones a cada idioma con archivo en `alembic/seed/catalogs.<idioma>.json` (la 0078 solo cargó los registros
  que existían entonces).

Compatible con la versión anterior en marcha: columnas con valor por omisión y tablas nuevas. Los permisos de la API
sobre la tabla nueva los pone `python -m app.cli db roles` en el mismo despliegue (servicio `migrate`). `downgrade`
quita la tabla (sus objetos del bucket quedan sin referencia: la regla de ciclo de vida o a mano), el catálogo, las
marcas con sus traducciones, las columnas y el índice.

Revision ID: 0079
Revises: 0078
Create Date: 2026-10-06 23:30:00
"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0079"
down_revision: str | None = "0078"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_DIR = Path(__file__).resolve().parents[1] / "seed"
CATALOG = "catalog"
BIOMETRICS = "biometrics"
TENANCY = "tenancy"
QUESTIONS = "voice_questions"
FLAGS = "enrollment_flags"
NEW_FLAGS = ("VOICE_RETRIES", "VIDEO_FACE_MISMATCH")
TABLE = "enrollment_voice_answers"
#: La seguridad por fila de la tabla nueva (la misma SQL que `row_security.policy_ddl`), en sentencias constantes.
ROW_SECURITY = (
    "ALTER TABLE biometrics.enrollment_voice_answers ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE biometrics.enrollment_voice_answers FORCE ROW LEVEL SECURITY",
    "CREATE POLICY tenant_isolation ON biometrics.enrollment_voice_answers "
    "USING (company_id = NULLIF(current_setting('app.company_id', true), '')::integer) "
    "WITH CHECK (company_id = NULLIF(current_setting('app.company_id', true), '')::integer)",
    "COMMENT ON COLUMN biometrics.enrollment_voice_answers.company_id IS 'Empresa dueña de la fila. Seguridad por fila "
    "(política tenant_isolation): solo se ve y se escribe en una transacción con app.company_id igual (o con el rol de "
    "la plataforma).'",
)
_TRANSLATIONS = sa.table(
    "translations",
    sa.column("catalog"),
    sa.column("code"),
    sa.column("locale"),
    sa.column("field"),
    sa.column("text"),
    schema=CATALOG,
)
_FLAGS = sa.table(FLAGS, sa.column("code"), schema=CATALOG)


def _seed(name: str) -> dict:
    return json.loads((SEED_DIR / name).read_text(encoding="utf-8"))


def _insert_missing(table_name: str, row: dict, keys: list[str]) -> None:
    """Inserta la fila del seed si no está (idempotente: una base nueva ya la cargó con el seed vigente)."""
    table = sa.table(table_name, *(sa.column(key) for key in row), schema=CATALOG)
    op.execute(postgresql.insert(table).values(**row).on_conflict_do_nothing(index_elements=keys))


def _catalogs(seed: dict) -> None:
    questions = op.create_table(
        QUESTIONS,
        sa.Column("code", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.String(length=300), nullable=True),
        sa.Column("sort_order", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.PrimaryKeyConstraint("code", name=op.f(f"pk_{QUESTIONS}")),
        schema=CATALOG,
    )
    op.bulk_insert(questions, seed[QUESTIONS])
    for row in (r for r in seed[FLAGS] if r["code"] in NEW_FLAGS):
        _insert_missing(FLAGS, row, ["code"])


def _translations() -> None:
    """Los textos del catálogo nuevo y de las marcas nuevas en cada idioma que tiene archivo de traducciones."""
    rows: list[dict[str, str]] = []
    for path in sorted(SEED_DIR.glob("catalogs.*.json")):
        locale = path.name.removeprefix("catalogs.").removesuffix(".json")
        texts = json.loads(path.read_text(encoding="utf-8"))
        rows += [
            {"catalog": QUESTIONS, "code": code, "locale": locale, "field": field, "text": text}
            for code, fields in texts.get(QUESTIONS, {}).items()
            for field, text in fields.items()
        ]
        rows += [
            {"catalog": FLAGS, "code": code, "locale": locale, "field": field, "text": text}
            for code in NEW_FLAGS
            for field, text in texts.get(FLAGS, {}).get(code, {}).items()
        ]
    if not rows:
        return
    statement = postgresql.insert(_TRANSLATIONS).values(rows)
    op.execute(
        statement.on_conflict_do_update(
            index_elements=["catalog", "code", "locale", "field"], set_={"text": statement.excluded.text}
        )
    )


def _answers_table() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("enrollment_id", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("question", sa.String(length=30), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("attempts", sa.SmallInteger(), nullable=False),
        sa.Column("transcript", sa.String(length=200), nullable=True),
        sa.Column("similarity", sa.Float(), nullable=True),
        sa.Column("face_similarity", sa.Float(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("uid", sa.String(length=32), nullable=False),
        sa.Column("content_type", sa.String(length=30), nullable=True),
        sa.Column("object_name", sa.String(length=300), nullable=True),
        sa.Column("byte_size", sa.Integer(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("position >= 0 AND attempts >= 1", name=op.f(f"ck_{TABLE}_position_attempts")),
        sa.ForeignKeyConstraint(
            ["enrollment_id", "company_id"],
            [f"{BIOMETRICS}.face_enrollments.id", f"{BIOMETRICS}.face_enrollments.company_id"],
            name=op.f(f"fk_{TABLE}_enrollment_company"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["question"], [f"{CATALOG}.{QUESTIONS}.code"], name=op.f(f"fk_{TABLE}_question_{QUESTIONS}")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{TABLE}")),
        sa.UniqueConstraint("company_id", "enrollment_id", "position", name=f"uq_{TABLE}_position"),
        schema=BIOMETRICS,
    )
    op.create_index(f"ix_{TABLE}_employee", TABLE, ["company_id", "employee_id"], unique=False, schema=BIOMETRICS)
    op.create_index(f"ix_{TABLE}_created", TABLE, ["created_at"], unique=False, schema=BIOMETRICS)
    for statement in ROW_SECURITY:
        op.execute(statement)


def upgrade() -> None:
    seed = _seed("catalogs.json")
    op.add_column(
        "verification_policy",
        sa.Column("voice_verification", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        schema=TENANCY,
    )
    op.add_column(
        "face_enrollments",
        sa.Column("voice_required", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        schema=BIOMETRICS,
    )
    op.add_column(
        "face_enrollments", sa.Column("voice_passed_at", sa.DateTime(timezone=True), nullable=True), schema=BIOMETRICS
    )
    op.add_column(
        "face_enrollments",
        sa.Column("voice_attempts", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        schema=BIOMETRICS,
    )
    op.create_index(
        "ix_face_enrollments_voice_pending",
        "face_enrollments",
        ["submitted_at"],
        unique=False,
        schema=BIOMETRICS,
        postgresql_where=sa.text("voice_required AND voice_passed_at IS NULL"),
    )
    _catalogs(seed)
    _answers_table()
    _translations()


def downgrade() -> None:
    op.drop_table(TABLE, schema=BIOMETRICS)
    op.execute(sa.delete(_TRANSLATIONS).where(_TRANSLATIONS.c.catalog == QUESTIONS))
    op.execute(
        sa.delete(_TRANSLATIONS).where(_TRANSLATIONS.c.catalog == FLAGS, _TRANSLATIONS.c.code.in_(NEW_FLAGS))
    )
    op.execute(sa.delete(_FLAGS).where(_FLAGS.c.code.in_(NEW_FLAGS)))
    op.drop_table(QUESTIONS, schema=CATALOG)
    op.drop_index("ix_face_enrollments_voice_pending", table_name="face_enrollments", schema=BIOMETRICS)
    op.drop_column("face_enrollments", "voice_attempts", schema=BIOMETRICS)
    op.drop_column("face_enrollments", "voice_passed_at", schema=BIOMETRICS)
    op.drop_column("face_enrollments", "voice_required", schema=BIOMETRICS)
    op.drop_column("verification_policy", "voice_verification", schema=TENANCY)
