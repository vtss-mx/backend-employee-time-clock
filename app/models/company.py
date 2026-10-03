from sqlalchemy import Boolean, CheckConstraint, ColumnElement, Index, Integer, String, func, literal_column, true
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.db_schemas import TENANCY
from app.models.mixins import TimestampMixin


class Company(TimestampMixin, Base):
    """Empresa cliente (tenant). Sus usuarios, empleados, validaciones, bitácora y política de
    verificación están aislados del resto por `company_id`.

    Datos fiscales/de contacto opcionales en la BD solo por la empresa creada al migrar desde la
    versión de una sola empresa; la API los exige al dar de alta.
    """

    __tablename__ = "companies"
    __table_args__ = (
        # Límite del plan: sin límite (NULL) o al menos un empleado.
        CheckConstraint("max_employees IS NULL OR max_employees > 0", name="max_employees_positive"),
        {"schema": TENANCY},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)  # nombre comercial
    legal_name: Mapped[str | None] = mapped_column(String(200))  # razón social
    rfc: Mapped[str | None] = mapped_column(String(13), unique=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(16))  # E.164: +<lada><número>
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)
    #: Límite de empleados del plan (None = sin límite).
    max_employees: Mapped[int | None] = mapped_column(Integer)


_SPACE = literal_column("' '", String)


def company_search_text() -> ColumnElement[str]:
    """Texto de búsqueda de la empresa (nombre, razón social y RFC). Misma expresión que su índice."""
    return func.lower(
        Company.name
        + _SPACE
        + func.coalesce(Company.legal_name, literal_column("''", String))
        + _SPACE
        + func.coalesce(Company.rfc, literal_column("''", String))
    )


Index("ix_companies_name", func.lower(Company.name).label("name_lower"))
Index(
    "ix_companies_search_trgm",
    company_search_text().label("search_text"),
    postgresql_using="gin",
    postgresql_ops={"search_text": "gin_trgm_ops"},
).ddl_if(dialect="postgresql")
