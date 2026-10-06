"""SQL que los modelos de SQLAlchemy no expresan (funciones, comentarios): `alembic/sql/<revisión>_<tema>.sql`.

Cada archivo lo ejecuta SU migración (y ya no cambia, como ella); una base que sale de los modelos
(`create_all` de las pruebas y de `scripts/quality.sh`) ejecuta todos, en orden, al final de crear las tablas.
Así las dos bases quedan iguales sin escribir la misma SQL dos veces (`scripts/schema_signature.sql` compara
también las funciones y los comentarios). Un cambio es un archivo nuevo con el número de su migración.
"""

from pathlib import Path
from typing import Final

from sqlalchemy import DDL, MetaData, event

#: Carpeta de los archivos SQL de las migraciones.
SQL_DIR: Final = Path(__file__).resolve().parents[2] / "alembic" / "sql"


def sql_files() -> list[Path]:
    """Los archivos en el orden de sus migraciones (el nombre empieza con el número de revisión)."""
    return sorted(SQL_DIR.glob("*.sql"))


def register_sql_files(metadata: MetaData) -> None:
    """Después de `create_all` (solo PostgreSQL), cada archivo en orden. `DDL` usa `%` para sus sustituciones:
    el `%` literal de la SQL (p. ej. `format('%I')`) se escribe doble."""
    for path in sql_files():
        statement = DDL(path.read_text(encoding="utf-8").replace("%", "%%"))
        event.listen(metadata, "after_create", statement.execute_if(dialect="postgresql"))
