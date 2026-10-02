from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ColumnElement, Date, Enum, ForeignKey, Index, String, func, literal_column
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql.expression import false as sql_false

from app.core.database import Base
from app.core.db_schemas import AUTH, CATALOG, TENANCY, WORKFORCE
from app.models.enums import FaceStatus
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.company import Company
    from app.models.employee_qr import EmployeeQr
    from app.models.face_embedding import FaceEmbedding
    from app.models.user import User


class Employee(TimestampMixin, Base):
    __tablename__ = "employees"
    __table_args__ = (
        # Todo índice empieza por la empresa: cada consulta lee solo su porción (multiempresa).
        # Listado paginado (ORDER BY apellidos, nombre, id): se lee en orden, sin ordenar.
        Index("ix_employees_company_name", "company_id", "last_name", "first_name", "id"),
        # Únicos POR EMPRESA: la misma persona puede trabajar en dos empresas de la plataforma.
        Index("uq_employees_company_number", "company_id", "employee_number", unique=True),
        Index("uq_employees_company_rfc", "company_id", "rfc", unique=True),
        Index("uq_employees_company_curp", "company_id", "curp", unique=True),
        Index("uq_employees_company_nss", "company_id", "nss", unique=True),
        # Una persona tiene a lo más un empleo por empresa (y puede tener varios en total).
        Index("uq_employees_company_user", "company_id", "user_id", unique=True),
        # Empleos de una persona (selector de empresa al iniciar sesión).
        Index("ix_employees_user", "user_id"),
        {"schema": WORKFORCE},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey(f"{AUTH}.users.id", ondelete="CASCADE"), nullable=False)
    company_id: Mapped[int] = mapped_column(ForeignKey(f"{TENANCY}.companies.id", ondelete="RESTRICT"), nullable=False)
    employee_number: Mapped[str] = mapped_column(String(30), nullable=False)
    # RFC de persona física, normalizado en mayúsculas. Opcional en la BD solo por los empleados
    # dados de alta antes de existir el campo; la API lo exige al registrar.
    rfc: Mapped[str | None] = mapped_column(String(13))
    # CURP y NSS (IMSS). Igual que el RFC: obligatorios al registrar; opcionales en la BD solo por
    # los empleados dados de alta antes. El teléfono es de la persona (auth.users.phone).
    curp: Mapped[str | None] = mapped_column(String(18))
    nss: Mapped[str | None] = mapped_column(String(11))
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    birth_date: Mapped[date] = mapped_column(Date, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Excepción autorizada por COMPANY (motivos religiosos o médicos): no se exige
    # retirar prendas de cabeza. Lentes y cubrebocas se exigen siempre.
    headwear_exempt: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sql_false(), nullable=False)
    # Registro facial: lo hace el propio empleado y lo valida COMPANY.
    face_status: Mapped[FaceStatus] = mapped_column(
        Enum(FaceStatus, native_enum=False, length=20, validate_strings=True),
        ForeignKey(f"{CATALOG}.face_statuses.code"),
        default=FaceStatus.NOT_ENROLLED,
        server_default=FaceStatus.NOT_ENROLLED.value,
        nullable=False,
    )
    face_rejection_reason: Mapped[str | None] = mapped_column(String(500))

    user: Mapped["User"] = relationship(back_populates="employees", lazy="joined")
    company: Mapped["Company"] = relationship(lazy="joined")
    face_embeddings: Mapped[list["FaceEmbedding"]] = relationship(
        back_populates="employee", cascade="all, delete-orphan", passive_deletes=True
    )
    qr_codes: Mapped[list["EmployeeQr"]] = relationship(
        back_populates="employee", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    @property
    def phone(self) -> str | None:
        """Teléfono de la persona (único en la plataforma; lo comparten todos sus empleos)."""
        return self.user.phone

    @property
    def shared_account(self) -> bool:
        """La persona también trabaja en otra empresa con la misma cuenta."""
        return len(self.user.employees) > 1


_SPACE = literal_column("' '", String)


def employee_search_text() -> ColumnElement[str]:
    """Texto de búsqueda del empleado: nombre, apellidos, número, RFC, CURP y NSS, en minúsculas.

    La búsqueda y el índice GIN de trigramas usan esta MISMA expresión (si cambia, el índice
    debe recrearse en una migración). Los literales van en el SQL, no como parámetros, para
    que PostgreSQL reconozca la expresión del índice también en planes preparados.
    """
    return func.lower(
        Employee.first_name
        + _SPACE
        + Employee.last_name
        + _SPACE
        + Employee.employee_number
        + _SPACE
        + func.coalesce(Employee.rfc, literal_column("''", String))
        + _SPACE
        + func.coalesce(Employee.curp, literal_column("''", String))
        + _SPACE
        + func.coalesce(Employee.nss, literal_column("''", String))
    )


# Búsqueda por fragmentos (LIKE '%texto%') dentro de una empresa: GIN compuesto (btree_gin para
# company_id + trigramas de pg_trgm para el texto) — un solo índice resuelve ambas condiciones.
Index(
    "ix_employees_company_search_trgm",
    Employee.company_id,
    employee_search_text().label("search_text"),
    postgresql_using="gin",
    postgresql_ops={"search_text": "gin_trgm_ops"},
).ddl_if(dialect="postgresql")
