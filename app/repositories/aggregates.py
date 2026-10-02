from sqlalchemy import Select
from sqlalchemy.orm import Session


def group_counts(db: Session, stmt: Select[int | None, int]) -> dict[int, int]:
    """{clave: conteo} de un GROUP BY (las claves nulas se omiten)."""
    return {int(key): int(count) for key, count in db.execute(stmt) if key is not None}
