"""Entorno de Alembic: migraciones SIEMPRE por la conexión directa a PostgreSQL y una réplica a la vez.

- **Conexión directa** (`DATABASE_DIRECT_URL`, nunca PgBouncer): el candado de abajo es de SESIÓN y las
  migraciones de índices usan `CREATE INDEX CONCURRENTLY` fuera de una transacción (`autocommit_block`);
  ninguna de las dos cosas funciona a través de un pool en modo transacción.
- **Una réplica a la vez** (defensa en profundidad): docker compose migra con el servicio único `migrate`
  antes de arrancar la API, pero si varias réplicas migran a la vez (`RUN_MIGRATIONS=1`, otro orquestador)
  solo una trabaja; las demás esperan el candado, con tiempo límite, y luego no encuentran nada pendiente.
"""

import logging
import time
from logging.config import fileConfig

from sqlalchemy import Connection, engine_from_config, pool, text

import app.models  # noqa: F401  (registra los modelos en Base.metadata)
from alembic import context
from app.core.config import settings
from app.core.database import Base

config = context.config
config.set_main_option("sqlalchemy.url", settings.DATABASE_DIRECT_URL.replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
#: Candado de sesión (pg_advisory_lock) que serializa las migraciones entre réplicas.
MIGRATION_LOCK_KEY = 7_420_031
#: Cuánto espera una réplica a que otra termine de migrar (una migración de índices con volumen tarda
#: segundos o minutos; más que esto es una migración atorada y se avisa en lugar de esperar para siempre).
MIGRATION_LOCK_WAIT_SECONDS = 900
log = logging.getLogger("alembic.env")


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def take_migration_lock(connection: Connection) -> None:
    """Candado de sesión de las migraciones, esperando por turnos a que otra réplica termine (con tope)."""
    deadline = time.monotonic() + MIGRATION_LOCK_WAIT_SECONDS
    while not connection.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": MIGRATION_LOCK_KEY}).scalar():
        connection.commit()
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Otra réplica lleva más de {MIGRATION_LOCK_WAIT_SECONDS} s migrando: se cancela")
        log.info("Otra réplica está migrando; se espera su turno")
        time.sleep(2)
    connection.commit()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        if connection.dialect.name == "postgresql":
            take_migration_lock(connection)
        context.configure(
            connection=connection, target_metadata=target_metadata, render_as_batch=True, include_schemas=True
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
