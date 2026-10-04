"""Asistente de reportes (esquema reporting): lo que aprende de cada empresa y sus reportes guardados.

Todo es POR EMPRESA y nunca se comparte entre empresas: el vocabulario que aprende una empresa (sus
palabras para referirse a sus datos) puede contener nombres de su gente.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.db_schemas import AUTH, REPORTING, TENANCY


class AssistantQuery(Base):
    """Cada pregunta hecha al asistente, con cómo la interpretó y si sirvió.

    Es la memoria del asistente: de aquí salen las sugerencias (lo que más pregunta la empresa) y lo
    que aprende (una pregunta que no entendió y que la persona aclaró). Se depura con el tiempo
    (REPORT_QUERY_RETENTION_DAYS)."""

    __tablename__ = "assistant_queries"
    __table_args__ = (
        # Sugerencias y depuración: lo reciente de una empresa.
        Index("ix_assistant_queries_company_created", "company_id", "created_at"),
        Index("ix_assistant_queries_created", "created_at"),
        {"schema": REPORTING},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey(f"{TENANCY}.companies.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int | None] = mapped_column(ForeignKey(f"{AUTH}.users.id", ondelete="SET NULL"))
    question: Mapped[str] = mapped_column(String(500), nullable=False)
    #: La pregunta normalizada (sin acentos ni signos): agrupa las iguales para las sugerencias.
    normalized: Mapped[str] = mapped_column(String(500), nullable=False)
    #: Datos que se consultaron (None = no se entendió de qué datos se trataba).
    dataset: Mapped[str | None] = mapped_column(String(40))
    #: El plan del reporte en JSON (lo que se consultó exactamente).
    plan: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    #: Palabras de las que aprende: las que no reconoció (si la persona aclara) y las que entendió por
    #: lo ya aprendido (se refuerzan si sirvió, se debilitan si no).
    unknown_terms: Mapped[str | None] = mapped_column(String(500))
    rows: Mapped[int | None] = mapped_column(Integer)
    exported: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    #: La persona dijo si le sirvió (None = no dijo).
    helpful: Mapped[bool | None] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class LearnedPhrase(Base):
    """Palabra que una empresa usa para referirse a unos datos (aprendida de sus aclaraciones).

    P. ej. si en una empresa dicen «checadas» y la persona eligió «Identificaciones», la siguiente
    vez «checadas» ya lleva a esos datos. `hits` refuerza lo que se confirma y baja lo que se
    corrige; con `hits` en cero deja de usarse."""

    __tablename__ = "learned_phrases"
    __table_args__ = ({"schema": REPORTING},)

    company_id: Mapped[int] = mapped_column(ForeignKey(f"{TENANCY}.companies.id", ondelete="CASCADE"), primary_key=True)
    phrase: Mapped[str] = mapped_column(String(60), primary_key=True)
    dataset: Mapped[str] = mapped_column(String(40), primary_key=True)
    hits: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class SavedReport(Base):
    """Reporte guardado por la empresa para volver a generarlo (con datos al día) cuando quiera."""

    __tablename__ = "saved_reports"
    __table_args__ = (
        # Nombre único por empresa sin distinguir mayúsculas; también da el orden del listado.
        Index("uq_saved_reports_company_name", "company_id", text("lower(name)"), unique=True),
        {"schema": REPORTING},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey(f"{TENANCY}.companies.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    #: La pregunta con que nació (si vino del asistente).
    question: Mapped[str | None] = mapped_column(String(500))
    plan: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey(f"{AUTH}.users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    runs: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
