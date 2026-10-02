"""Motor de base de datos, sesión y clase base declarativa de SQLAlchemy."""

from collections.abc import Generator
from typing import Any

from sqlalchemy import DDL, MetaData, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings
from app.core.db_schemas import ALL_SCHEMAS

# Convención de nombres: la misma que usan las migraciones de Alembic (los modelos y la base deben
# coincidir; lo verifica scripts/quality.sh). Sin el esquema en el nombre.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# La base se organiza por esquemas de dominio (ver db_schemas.py). Las migraciones los crean en
# producción; esto cubre las BD creadas con create_all (pruebas en PostgreSQL).
for _schema in ALL_SCHEMAS:
    event.listen(
        Base.metadata,
        "before_create",
        DDL(f"CREATE SCHEMA IF NOT EXISTS {_schema}").execute_if(dialect="postgresql"),
    )

# Los índices de búsqueda usan trigramas (pg_trgm) combinados con company_id (btree_gin). En
# producción las extensiones las crean las migraciones (en public); esto cubre las BD creadas con
# create_all (pruebas en PostgreSQL).
for _extension in ("pg_trgm", "btree_gin"):
    event.listen(
        Base.metadata,
        "before_create",
        DDL(f"CREATE EXTENSION IF NOT EXISTS {_extension} WITH SCHEMA public").execute_if(dialect="postgresql"),
    )


_is_sqlite = settings.DATABASE_URL.startswith("sqlite")

if _is_sqlite:
    # SQLite no tiene esquemas: cada tabla se crea y consulta sin él.
    engine = create_engine(
        settings.DATABASE_URL,
        connect_args={"check_same_thread": False},
        execution_options={"schema_translate_map": dict.fromkeys(ALL_SCHEMAS)},
    )
else:
    engine = create_engine(
        settings.DATABASE_URL,
        pool_pre_ping=True,  # descarta conexiones rotas (reinicio de BD, red)
        pool_recycle=1800,
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_timeout=settings.DB_POOL_TIMEOUT_SECONDS,
        connect_args={
            "connect_timeout": settings.DB_CONNECT_TIMEOUT_SECONDS,
            # Ninguna consulta puede bloquear un worker indefinidamente. El search_path incluye los
            # esquemas de dominio (SQL escrito a mano sin calificar; SQLAlchemy siempre califica).
            "options": (
                f"-c statement_timeout={settings.DB_STATEMENT_TIMEOUT_MS} -c search_path={','.join(ALL_SCHEMAS)},public"
            ),
        },
    )

if _is_sqlite:
    # SQLite (solo pruebas/desarrollo) necesita activar las FK para respetar ON DELETE CASCADE.
    @event.listens_for(engine, "connect")
    def _enable_sqlite_fk(dbapi_connection: Any, _record: Any) -> None:  # pragma: no cover
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    except Exception:
        # Ante cualquier error la transacción en curso se revierte: nunca quedan
        # escrituras parciales ni conexiones en estado inválido en el pool.
        db.rollback()
        raise
    finally:
        db.close()


def wait_for_database(retries: int, delay: float = 2.0) -> bool:
    """Espera a que la BD acepte conexiones (arranque tolerante a BD aún no lista)."""
    import logging
    import time

    from sqlalchemy import text

    log = logging.getLogger(__name__)
    for attempt in range(1, retries + 1):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception as exc:
            log.warning("BD no disponible (intento %s/%s): %s", attempt, retries, exc.__class__.__name__)
            time.sleep(min(delay * attempt, 10))
    return False
