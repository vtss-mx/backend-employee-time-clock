"""Repertorio ampliado de la verificación por voz del registro facial (decisión del dueño del producto, 2026-10-07)

«Las respuestas que se hacen en el video deben ser aleatorias»: además de las seis preguntas de la 0079 (nombre
completo, fecha de nacimiento, empresa, número, departamento, sitio) ahora hay más variedad, toda validada en el
servidor (`app/speech/matching.py`):

- Nombre: `FIRST_NAME` (solo el nombre), `SURNAMES` (los apellidos completos) y —solo cuando los apellidos se separan en
  EXACTAMENTE dos palabras— `FIRST_SURNAME` y `SECOND_SURNAME` (un apellido compuesto no se parte con certeza).
- Fecha: `BIRTH_MONTH` (mes por nombre o cifra), `BIRTH_YEAR` (año) y `BIRTH_DAY` (día), derivados de la fecha de
  nacimiento.
- `ARITHMETIC_SUM`: la suma de dos números pequeños al azar que genera el servidor (prueba cognitiva y antirreplay, no
  un dato de identidad; el texto de la pregunta con los números viaja en el catálogo de mensajes `VOICE_SUM_PROMPT`, por
  eso el `name` del catálogo es genérico y sin marcadores: es el rótulo que ve la empresa al revisar).

Esta migración solo agrega filas al catálogo `catalog.voice_questions` y sus traducciones a `catalog.translations`, para
las bases que ya corrieron la 0079 (con las seis preguntas de entonces). Es idempotente: una base nueva ya las cargó con
el seed vigente (`seed_catalogs` lee `catalogs.json` y los archivos por idioma), así que los INSERT no hacen nada
(`on_conflict_do_nothing` / `on_conflict_do_update`). Compatible con la versión anterior en marcha: el código viejo
ignora los códigos nuevos (nunca los elige) y el nuevo los usa en cuanto están. `downgrade` quita las ocho preguntas y
sus traducciones (si no quedan respuestas aceptadas que las referencien).

Revision ID: 0086
Revises: 0085
Create Date: 2026-10-07 12:00:00
"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0086"
down_revision: str | None = "0085"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_DIR = Path(__file__).resolve().parents[1] / "seed"
CATALOG = "catalog"
QUESTIONS = "voice_questions"
#: Las preguntas nuevas (las seis de la 0079 ya están); las carga esta migración en las bases existentes.
NEW_CODES = (
    "FIRST_NAME",
    "SURNAMES",
    "FIRST_SURNAME",
    "SECOND_SURNAME",
    "BIRTH_MONTH",
    "BIRTH_YEAR",
    "BIRTH_DAY",
    "ARITHMETIC_SUM",
)

_QUESTIONS = sa.table(
    QUESTIONS,
    sa.column("code"),
    sa.column("name"),
    sa.column("description"),
    sa.column("sort_order"),
    sa.column("active"),
    schema=CATALOG,
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


def _seed(name: str) -> dict:
    return json.loads((SEED_DIR / name).read_text(encoding="utf-8"))


def _new_question_rows() -> list[dict]:
    """Las filas del catálogo (es-MX) de las preguntas nuevas, en el orden del seed."""
    return [row for row in _seed("catalogs.json")[QUESTIONS] if row["code"] in NEW_CODES]


def _new_translation_rows() -> list[dict[str, str]]:
    """Las traducciones de las preguntas nuevas en cada idioma que tiene archivo."""
    rows: list[dict[str, str]] = []
    for path in sorted(SEED_DIR.glob("catalogs.*.json")):
        locale = path.name.removeprefix("catalogs.").removesuffix(".json")
        texts = json.loads(path.read_text(encoding="utf-8"))
        rows += [
            {"catalog": QUESTIONS, "code": code, "locale": locale, "field": field, "text": text}
            for code, fields in texts.get(QUESTIONS, {}).items()
            if code in NEW_CODES
            for field, text in fields.items()
        ]
    return rows


def upgrade() -> None:
    questions = _new_question_rows()
    if questions:
        op.execute(postgresql.insert(_QUESTIONS).values(questions).on_conflict_do_nothing(index_elements=["code"]))
    translations = _new_translation_rows()
    if translations:
        statement = postgresql.insert(_TRANSLATIONS).values(translations)
        op.execute(
            statement.on_conflict_do_update(
                index_elements=["catalog", "code", "locale", "field"], set_={"text": statement.excluded.text}
            )
        )


def downgrade() -> None:
    op.execute(
        sa.delete(_TRANSLATIONS).where(
            _TRANSLATIONS.c.catalog == QUESTIONS, _TRANSLATIONS.c.code.in_(NEW_CODES)
        )
    )
    op.execute(sa.delete(_QUESTIONS).where(_QUESTIONS.c.code.in_(NEW_CODES)))
