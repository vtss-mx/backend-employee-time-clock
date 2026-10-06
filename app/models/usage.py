"""Consumo de la plataforma por empresa y por usuario (esquema `ops`): la base para monitorear y para
fijar precios. Lo escribe el medidor de peticiones (`usage_meter`) en lotes y el mantenimiento
(almacenamiento y tareas diarias); el ADMIN lo consulta en "Consumo".

- Tres granos con los MISMOS contadores (`UsageCounters`): por empresa y día (`usage_daily`, para la
  plataforma completa y las series diarias), por empresa, día y ruta (`usage_routes`) y por empresa,
  día y usuario (`usage_users`). Cada lote del medidor suma sus contadores con un UPSERT atómico
  (`ON CONFLICT ... DO UPDATE SET requests = requests + EXCLUDED.requests ...`): varias instancias
  escriben las mismas filas sin perder ni duplicar nada.
- `company_id = 0` agrupa lo que no es de una empresa (ADMIN, inicio de sesión, salud, anónimos) y
  `user_id = 0` lo anónimo. Por eso estos ids van SIN llave foránea: el medidor nunca falla porque
  una empresa o cuenta se haya borrado entre lotes; lo de una empresa borrada sale con la retención.
- La ruta es la PLANTILLA (`GET /api/employees/{employee_id}`): las filas están acotadas por las rutas
  de la API, no por cada URL; una URL que no corresponde a ninguna ruta cuenta como `OTHER`.
- Llave primaria con la empresa primero (las consultas son de una empresa en un rango de días) y un
  índice por día para el resumen de toda la plataforma y la depuración (`USAGE_RETENTION_DAYS`).
- El detalle por ruta y por cuenta y las fotos del almacenamiento crecen con cada empresa y cada día:
  particionados por mes en `day` (`app/core/partitions.py`); lo que vence sale con su partición. El total por
  empresa y día (`usage_daily`, una fila por empresa y día) no se particiona: su retención lo acota.
- Datos de una empresa con seguridad por fila (`app/core/row_security.py`): solo los lee y escribe el código de
  la plataforma (medidor, mantenimiento, pantalla del ADMIN).
"""

from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.db_schemas import CATALOG, OPS
from app.core.partitions import partitioned

_ZERO = text("0")


class UsageCounters:
    """Contadores de consumo de un grano (peticiones, datos, tiempo de proceso y errores)."""

    requests: Mapped[int] = mapped_column(BigInteger, default=0, server_default=_ZERO, nullable=False)
    #: Bytes del cuerpo de las peticiones (lo que la API leyó) y de las respuestas (lo que envió).
    bytes_in: Mapped[int] = mapped_column(BigInteger, default=0, server_default=_ZERO, nullable=False)
    bytes_out: Mapped[int] = mapped_column(BigInteger, default=0, server_default=_ZERO, nullable=False)
    #: Tiempo total de proceso (ms) y la petición más lenta.
    duration_ms: Mapped[int] = mapped_column(BigInteger, default=0, server_default=_ZERO, nullable=False)
    max_ms: Mapped[int] = mapped_column(Integer, default=0, server_default=_ZERO, nullable=False)
    #: Respuestas 5xx (fallas del servidor) y 4xx (rechazos normales: validación, permisos...).
    server_errors: Mapped[int] = mapped_column(BigInteger, default=0, server_default=_ZERO, nullable=False)
    client_errors: Mapped[int] = mapped_column(BigInteger, default=0, server_default=_ZERO, nullable=False)


class UsageDaily(UsageCounters, Base):
    """Consumo de una empresa en un día del negocio."""

    __tablename__ = "usage_daily"
    __table_args__ = (
        # Resumen de toda la plataforma en un rango de días (por empresa) y depuración por antigüedad.
        Index("ix_usage_daily_day", "day"),
        # Cada lote del medidor suma a la fila del día: espacio libre para actualizarla en su página (HOT; 0055).
        {"schema": OPS, "postgresql_with": {"fillfactor": 80}},
    )

    company_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)


class UsageRoute(UsageCounters, Base):
    """Consumo de una empresa en un día por ruta de la API (qué usa y cuánto le cuesta cada cosa)."""

    __tablename__ = "usage_routes"
    __table_args__ = (Index("ix_usage_routes_day", "day"), {"schema": OPS, **partitioned("day")})

    company_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    #: Método y plantilla de la ruta (`POST /api/attendance/sessions`) u `OTHER`.
    route: Mapped[str] = mapped_column(String(160), primary_key=True)


class UsageUser(UsageCounters, Base):
    """Consumo de una empresa en un día por cuenta de usuario (quién consume)."""

    __tablename__ = "usage_users"
    __table_args__ = (Index("ix_usage_users_day", "day"), {"schema": OPS, **partitioned("day")})

    company_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)


class StorageSnapshot(Base):
    """Almacenamiento de una empresa por grupo (personal, biometría, asistencia...) en un día: filas y
    bytes estimados (filas × tamaño promedio por fila de cada tabla, con sus índices). Lo toma el
    mantenimiento una vez al día."""

    __tablename__ = "storage_snapshots"
    __table_args__ = (
        # La foto más reciente de toda la plataforma (resumen y listado de empresas) y la depuración.
        Index("ix_storage_snapshots_day", "day"),
        {"schema": OPS, **partitioned("day")},
    )

    company_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    category: Mapped[str] = mapped_column(
        String(30), ForeignKey(f"{CATALOG}.storage_categories.code"), primary_key=True
    )
    rows: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)


class DailyTask(Base):
    """Tareas diarias del mantenimiento ya hechas (plantilla del día, foto del almacenamiento,
    pronósticos): con varias instancias y vueltas cada pocos minutos, cada una se hace UNA vez por día
    y la que faltó (instancia caída) se repone en la siguiente vuelta."""

    __tablename__ = "daily_tasks"
    __table_args__ = ({"schema": OPS},)

    task: Mapped[str] = mapped_column(String(30), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    done_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
