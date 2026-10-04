from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.db_schemas import AUTH, CATALOG, OPS


class ErrorReport(Base):
    """Una falla del sistema, agrupada: todas sus ocurrencias iguales en una sola fila.

    Se registran solo las fallas que alguien debe corregir: excepciones no controladas, fallas del
    servidor o de una dependencia (5xx), errores en segundo plano, fallas del canal WebSocket y fallas
    de la aplicación web (`CLIENT`). Un 4xx es un resultado normal y no llega aquí (las filas WARNING
    que ya existían se conservan como historial). Agrupar por `fingerprint` (origen, código, ruta,
    tipo de excepción...) acota la tabla aunque un error se repita millones de veces: cuenta
    `occurrences` y guarda la primera y la última vez, el último traceId y el detalle técnico. El
    ADMIN le da seguimiento con `status`; uno solucionado que vuelve a ocurrir se reabre solo como
    pendiente (`reopened`).
    """

    __tablename__ = "error_reports"
    __table_args__ = (
        Index("uq_error_reports_fingerprint", "fingerprint", unique=True),
        # Bandeja del ADMIN: por estado, lo más reciente primero (y su contador del menú).
        Index("ix_error_reports_status_seen", "status", "last_seen_at", "id"),
        Index("ix_error_reports_seen", "last_seen_at", "id"),
        Index(
            "ix_error_reports_status_changed_by_id",
            "status_changed_by_id",
            postgresql_where=text("status_changed_by_id IS NOT NULL"),
            sqlite_where=text("status_changed_by_id IS NOT NULL"),
        ),
        CheckConstraint("occurrences > 0", name="occurrences_positive"),
        CheckConstraint("source IN ('HTTP', 'LOG', 'WEBSOCKET', 'CLIENT')", name="source"),
        {"schema": OPS},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    #: HTTP (falla de una petición), LOG (registrado en segundo plano o dentro de un proceso), WEBSOCKET
    #: o CLIENT (falla de la aplicación web que reportó el navegador).
    source: Mapped[str] = mapped_column(String(10), nullable=False)
    severity: Mapped[str] = mapped_column(String(30), ForeignKey(f"{CATALOG}.error_severities.code"), nullable=False)
    status: Mapped[str] = mapped_column(
        String(30),
        ForeignKey(f"{CATALOG}.error_statuses.code"),
        default="PENDING",
        server_default="PENDING",
        nullable=False,
    )
    #: Código estable del error (p. ej. EMAIL_TAKEN, INTERNAL_ERROR) o el módulo que lo registró.
    code: Mapped[str] = mapped_column(String(120), nullable=False)
    message: Mapped[str] = mapped_column(String(1000), nullable=False)
    http_status: Mapped[int | None] = mapped_column(SmallInteger)
    method: Mapped[str | None] = mapped_column(String(10))
    #: Ruta sin ids (`GET /api/employees/{id}`, o la pantalla de la app: `/company/employees/{id}`) o
    #: archivo:línea de quien lo registró.
    location: Mapped[str | None] = mapped_column(String(255))
    exception_type: Mapped[str | None] = mapped_column(String(255))
    #: Detalle técnico de la última ocurrencia (stack trace), solo para el ADMIN.
    detail: Mapped[str | None] = mapped_column(Text)
    occurrences: Mapped[int] = mapped_column(BigInteger, default=1, server_default=text("1"), nullable=False)
    reopened: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_trace_id: Mapped[str | None] = mapped_column(String(64))
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status_changed_by_id: Mapped[int | None] = mapped_column(ForeignKey(f"{AUTH}.users.id", ondelete="SET NULL"))


class ErrorOccurrence(Base):
    """Ocurrencias recientes de un error (pocas por vuelta de guardado; el mantenimiento depura las
    viejas): quién, cuándo, con qué traceId y en qué empresa, para investigar."""

    __tablename__ = "error_occurrences"
    __table_args__ = (
        Index("ix_error_occurrences_report", "report_id", "id"),
        Index("ix_error_occurrences_occurred_at", "occurred_at"),
        {"schema": OPS},
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey(f"{OPS}.error_reports.id", ondelete="CASCADE"), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(64))
    # Ids informativos SIN llave foránea a propósito: si la cuenta o la empresa se borra justo antes de
    # guardar el lote, una FK haría fallar el lote completo y se perderían los reportes.
    user_id: Mapped[int | None] = mapped_column(Integer)
    company_id: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str] = mapped_column(String(1000), nullable=False)
    #: Contexto literal en JSON: quién (cuenta, correo, rol, empresa), la petición (método, URL,
    #: encabezados, cuerpo) y la respuesta (estado y cuerpo). Sin secretos ni archivos.
    context: Mapped[str | None] = mapped_column(Text)
