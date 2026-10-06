"""Motor de base de datos, sesión y clase base declarativa de SQLAlchemy."""

from collections.abc import Generator
from typing import Any

from sqlalchemy import DDL, Engine, MetaData, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings
from app.core.db_schemas import ALL_SCHEMAS
from app.core.input_guard import guard_engine
from app.core.observability import TimedQueuePool
from app.core.row_security import PLATFORM, SCOPE_KEY
from app.core.sql_safety import sql_identifier

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
        DDL(f"CREATE SCHEMA IF NOT EXISTS {sql_identifier(_schema)}").execute_if(dialect="postgresql"),
    )

# Los índices de búsqueda usan trigramas (pg_trgm) combinados con company_id (btree_gin). En
# producción las extensiones las crean las migraciones (en public); esto cubre las BD creadas con
# create_all (pruebas en PostgreSQL).
for _extension in ("pg_trgm", "btree_gin"):
    event.listen(
        Base.metadata,
        "before_create",
        DDL(f"CREATE EXTENSION IF NOT EXISTS {sql_identifier(_extension)} WITH SCHEMA public").execute_if(
            dialect="postgresql"
        ),
    )


def _enable_sqlite_fk(dbapi_connection: Any, _record: Any) -> None:
    """SQLite (solo pruebas/desarrollo) necesita activar las FK en cada conexión para respetar
    ON DELETE CASCADE y las restricciones como PostgreSQL."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


_SEARCH_PATH = f"{','.join(ALL_SCHEMAS)},public"


def session_options(pooled: bool) -> dict[str, str]:
    """Parámetros de sesión que la API manda al conectar.

    Directo a PostgreSQL: `statement_timeout` (ninguna consulta bloquea un worker indefinidamente) y el
    `search_path` con los esquemas de dominio (SQL escrito a mano sin calificar; SQLAlchemy siempre
    califica). Detrás de PgBouncer en modo transacción (`pooled`), NADA: la conexión del servidor la
    comparten todos los clientes, PgBouncer rechaza esos parámetros al conectar ("unsupported startup
    parameter") y `statement_timeout` lo fija él en cada conexión al servidor (`connect_query`); el
    `search_path` de la base ya lo dejaron las migraciones."""
    if pooled:
        return {}
    return {"options": f"-c statement_timeout={settings.DB_STATEMENT_TIMEOUT_MS} -c search_path={_SEARCH_PATH}"}


def build_engine(url: str, *, pooled: bool = False) -> Engine:
    """Motor de la URL dada. PostgreSQL (producción): pool acotado y tiempos límite en todo; `pooled` =
    la URL es de PgBouncer en modo transacción (ver `session_options`). SQLite (pruebas): sin esquemas y
    con FK activas. Crear el motor no abre conexiones. Todo motor lleva la última barrera contra el texto que la
    base no puede guardar (`input_guard.guard_engine`: 422 en lugar de un 500 de psycopg)."""
    if url.startswith("sqlite"):
        # SQLite no tiene esquemas: cada tabla se crea y consulta sin él.
        sqlite_engine = create_engine(
            url,
            hide_parameters=True,  # los errores de SQL nunca llevan datos de las personas
            connect_args={"check_same_thread": False},
            execution_options={"schema_translate_map": dict.fromkeys(ALL_SCHEMAS)},
        )
        event.listen(sqlite_engine, "connect", _enable_sqlite_fk)
        return guard_engine(sqlite_engine)
    postgres_engine = create_engine(
        url,
        hide_parameters=True,  # los errores de SQL (logs y reportes) nunca llevan datos de las personas
        # El pool de siempre que además mide la espera de una conexión (`db.acquire`, pantalla "Rendimiento").
        poolclass=TimedQueuePool,
        pool_pre_ping=True,  # descarta conexiones rotas (reinicio de BD, red)
        pool_recycle=settings.DB_POOL_RECYCLE_SECONDS,
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_timeout=settings.DB_POOL_TIMEOUT_SECONDS,
        connect_args={
            "connect_timeout": settings.DB_CONNECT_TIMEOUT_SECONDS,
            **session_options(pooled),
            # Sin sentencias preparadas del servidor: psycopg 3 las crea tras 5 ejecuciones y el plan
            # genérico ignoraba índices parciales (p. ej. empleados aprobados de la galería facial).
            # También es lo que exige PgBouncer en modo transacción (una sentencia preparada vive en una
            # conexión del servidor que la siguiente transacción quizá ya no tiene).
            "prepare_threshold": None,
        },
    )
    return guard_engine(postgres_engine)


engine = build_engine(settings.DATABASE_URL, pooled=settings.DB_POOLER == "pgbouncer")

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session]:
    """La sesión de una petición. Empieza SIN alcance (`row_security`): hasta que la autenticación declare la
    empresa (o la plataforma), las tablas de empresa no devuelven nada."""
    db = SessionLocal(info={SCOPE_KEY: None})
    try:
        yield db
    except Exception:
        # Ante cualquier error la transacción en curso se revierte: nunca quedan
        # escrituras parciales ni conexiones en estado inválido en el pool.
        db.rollback()
        raise
    finally:
        db.close()


def platform_session() -> Session:
    """Sesión del código de la PLATAFORMA fuera de una petición (mantenimiento, medidor de consumo, arranque, línea
    de comandos): cruza empresas a propósito (`row_security.use_platform`)."""
    return SessionLocal(info={SCOPE_KEY: PLATFORM})


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


def statement_timeout_missing() -> bool:
    """¿Las consultas de la API quedaron SIN tiempo límite? (regla: toda espera tiene tiempo límite).

    Directo a PostgreSQL lo garantiza `session_options`; detrás de PgBouncer depende de su configuración
    (`connect_query`): un PgBouncer mal configurado dejaría cada consulta sin límite en silencio. Se revisa al
    arrancar con la misma conexión que usan las peticiones; solo aplica a PostgreSQL."""
    from sqlalchemy import text

    if engine.dialect.name != "postgresql":
        return False
    with engine.connect() as conn:
        value = conn.execute(text("SHOW statement_timeout")).scalar_one()
    return str(value).strip() == "0"  # PostgreSQL muestra "0" cuando no hay límite


def row_security_bypassed() -> bool:
    """¿La API se conecta con un rol que se salta la seguridad por fila (superusuario o `BYPASSRLS`)? Entonces la
    última barrera del aislamiento entre empresas no existe aunque las políticas estén puestas (regla 14): se revisa
    al arrancar con la misma conexión de las peticiones. Solo PostgreSQL."""
    from sqlalchemy import text

    if engine.dialect.name != "postgresql":
        return False
    with engine.connect() as conn:
        found = conn.execute(text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"))
        return bool(found.scalar())
