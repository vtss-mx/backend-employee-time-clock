"""Observabilidad de rendimiento de la plataforma (esquema `ops`, pantalla "Rendimiento" del ADMIN).

Datos de la PLATAFORMA, no de una empresa: no llevan `company_id` (se mide cada ruta, función y pantalla de toda la
plataforma) y por eso tampoco seguridad por fila; solo los escriben el guardado en lotes y el mantenimiento, y solo
los lee el ADMIN. Ninguna fila guarda datos de una persona: tiempos, conteos y la plantilla de la ruta o pantalla.

- `perf_minutes`: lo que suman los observadores (`app/core/perf_meter.py`) por minuto, tipo (`PerfKind`) y nombre
  (`GET /api/employees/{employee_id}`, `face.detect`, `/admin/companies/{id}`). Cada lote SUMA con un UPSERT
  atómico: varias réplicas escriben la misma fila sin perder ni duplicar. Crece con cada minuto: particionada por mes
  en `minute` (`app/core/partitions.py`) y depurada a los `PERF_MINUTE_RETENTION_DAYS`.
- `perf_hours` y `perf_days`: los resúmenes que arma el mantenimiento (`perf_rollup`) para los periodos largos (una
  semana por horas, tres meses por días) sin leer millones de minutos. Su retención las acota (no se particionan).
- `slow_request_alerts` (regla 18): UNA fila por ruta con peticiones de más de `SLOW_REQUEST_THRESHOLD_MS`, su
  contador, tiempos, el último traceId, una muestra del contexto y su seguimiento (abierta, en atención, resuelta;
  una resuelta que vuelve a ocurrir se reabre sola). Nunca una fila por petición.

Índices: la llave primaria `(kind, <tiempo>, name)` sirve a TODAS las consultas (la igualdad del tipo primero, el
rango de tiempo después y el nombre al final: la serie de un nombre filtra dentro del índice) y a la depuración (que
recorre los tipos con `kind IN (...)`). Sin índices de más: cada lote actualiza estas filas.
"""

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.db_schemas import CATALOG, OPS
from app.core.partitions import partitioned

_ZERO = text("0")
#: Espacio libre en cada página: cada lote (o cada vuelta del mantenimiento) actualiza las filas recientes sin
#: cambiar columnas de su índice (actualización HOT).
_HOT = {"postgresql_with": {"fillfactor": 80}}


def _count() -> Mapped[int]:
    return mapped_column(BigInteger, default=0, server_default=_ZERO, nullable=False)


def _ms() -> Mapped[float]:
    return mapped_column(Float, default=0.0, server_default=_ZERO, nullable=False)


class PerfCounters:
    """Contadores de una fila (los mismos en los tres granos). `errors`: 5xx de una petición, excepción de una
    función o, desde el navegador, 5xx / sin red / tiempo agotado; `client_errors`: 4xx. Tiempos en ms (los del
    navegador `WEB_CLS`, el puntaje × 1000). `h_<límite>`: cuántas mediciones cayeron en cada cubeta del histograma
    (`app/core/histogram.py`; `h_inf`: más de 10 s). La llave primaria de cada grano es `(kind, <tiempo>, name)`,
    declarada en su tabla (en ese orden: es el índice de todas sus consultas)."""

    #: Qué se mide (`PerfKind`) y su nombre: `GET /api/employees/{employee_id}`, `face.detect`, `/admin/companies/{id}`.
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    count: Mapped[int] = _count()
    errors: Mapped[int] = _count()
    client_errors: Mapped[int] = _count()
    total_ms: Mapped[float] = _ms()
    max_ms: Mapped[float] = _ms()
    #: Solo peticiones (HTTP): tiempo dentro de la base y sentencias, y bytes recibidos y enviados.
    db_ms: Mapped[float] = _ms()
    db_queries: Mapped[int] = _count()
    bytes_in: Mapped[int] = _count()
    bytes_out: Mapped[int] = _count()
    h_1: Mapped[int] = _count()
    h_2: Mapped[int] = _count()
    h_5: Mapped[int] = _count()
    h_10: Mapped[int] = _count()
    h_20: Mapped[int] = _count()
    h_35: Mapped[int] = _count()
    h_50: Mapped[int] = _count()
    h_75: Mapped[int] = _count()
    h_100: Mapped[int] = _count()
    h_150: Mapped[int] = _count()
    h_250: Mapped[int] = _count()
    h_400: Mapped[int] = _count()
    h_600: Mapped[int] = _count()
    h_1000: Mapped[int] = _count()
    h_2000: Mapped[int] = _count()
    h_5000: Mapped[int] = _count()
    h_10000: Mapped[int] = _count()
    h_inf: Mapped[int] = _count()


class PerfMinute(PerfCounters, Base):
    """Rendimiento por minuto (lo que guarda cada lote de los observadores). Particionada por mes en `minute`; el
    espacio libre para las actualizaciones lo pone cada partición (`ops.ensure_partitions`)."""

    __tablename__ = "perf_minutes"
    __table_args__ = (PrimaryKeyConstraint("kind", "minute", "name"), {"schema": OPS, **partitioned("minute")})

    #: Inicio del minuto (UTC) en que terminó la medición.
    minute: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PerfHour(PerfCounters, Base):
    """Rendimiento por hora (UTC): el mantenimiento vuelve a sumar las horas recientes desde `perf_minutes`."""

    __tablename__ = "perf_hours"
    __table_args__ = (PrimaryKeyConstraint("kind", "hour", "name"), {"schema": OPS, **_HOT})

    hour: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PerfDay(PerfCounters, Base):
    """Rendimiento por día del negocio (hora del Centro): el mantenimiento lo suma desde `perf_hours`."""

    __tablename__ = "perf_days"
    __table_args__ = (PrimaryKeyConstraint("kind", "day", "name"), {"schema": OPS, **_HOT})

    day: Mapped[date] = mapped_column(Date, nullable=False)


class SlowRequestAlert(Base):
    """Las peticiones de una ruta que tardaron más que el umbral (regla 18), agrupadas: una fila por ruta."""

    __tablename__ = "slow_request_alerts"
    __table_args__ = (
        # El guardado en lotes suma a la fila de su ruta (ON CONFLICT (route)).
        Index("uq_slow_request_alerts_route", "route", unique=True),
        # Bandeja del ADMIN por seguimiento (y su contador del menú), lo más reciente primero; depuración de las
        # resueltas viejas.
        Index("ix_slow_request_alerts_status_seen", "status", "last_seen_at", "id"),
        # Bandeja sin filtro, lo más reciente primero.
        Index("ix_slow_request_alerts_seen", "last_seen_at", "id"),
        CheckConstraint("count > 0", name="count_positive"),
        {"schema": OPS, **_HOT},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Método y plantilla de la ruta (`GET /api/employees/{employee_id}`) u `OTHER`.
    route: Mapped[str] = mapped_column(String(160), nullable=False)
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    path: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(
        String(30),
        ForeignKey(f"{CATALOG}.slow_alert_statuses.code"),
        default="OPEN",
        server_default="OPEN",
        nullable=False,
    )
    #: Peticiones lentas acumuladas, su tiempo total (para el promedio), la última y la más lenta (ms).
    count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    total_ms: Mapped[float] = mapped_column(Float, nullable=False)
    last_ms: Mapped[float] = mapped_column(Float, nullable=False)
    max_ms: Mapped[float] = mapped_column(Float, nullable=False)
    #: Umbral vigente cuando ocurrió la última (SLOW_REQUEST_THRESHOLD_MS puede cambiar).
    threshold_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Cuándo se abrió por última vez (al crearse o al reabrirse): el aviso en vivo del ADMIN.
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_trace_id: Mapped[str | None] = mapped_column(String(64))
    last_status: Mapped[int | None] = mapped_column(SmallInteger)
    #: Contexto de la última (JSON): método, ruta, query sin secretos, estado, tiempos, BD, bytes, rol y empresa.
    sample: Mapped[str | None] = mapped_column(Text)
    reopened: Mapped[int] = mapped_column(Integer, default=0, server_default=_ZERO, nullable=False)
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Correo literal de quien cambió el seguimiento (como la cobranza: sigue legible aunque la cuenta se borre).
    status_changed_by: Mapped[str | None] = mapped_column(String(254))
