from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    ColumnElement,
    Date,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    func,
    literal_column,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql.expression import false as sql_false

from app.core.database import Base
from app.core.db_schemas import AUTH, CATALOG, TENANCY, WORKFORCE
from app.models.enums import FaceStatus
from app.models.mixins import SoftDeleteMixin, TimestampMixin, live_unique, trash_index

if TYPE_CHECKING:
    from app.models.company import Company
    from app.models.employee_qr import EmployeeQr
    from app.models.face_embedding import FaceEmbedding
    from app.models.user import User


class Employee(SoftDeleteMixin, TimestampMixin, Base):
    """Empleo de una persona en UNA empresa. Con borrado lógico (migración 0068): eliminado sale de los listados, del
    tablero, de la galería facial y de los conteos (también del cobro desde ese día) y su historial se conserva; sus
    datos biométricos y fotos se borran de verdad al eliminarlo (regla 13) y no vuelven al restaurarlo."""

    __tablename__ = "employees"
    __table_args__ = (
        # Todo índice empieza por la empresa: cada consulta lee solo su porción (multiempresa).
        # Listado paginado (ORDER BY apellidos, nombre, id): se lee en orden, sin ordenar. Completo (con todos, también
        # los eliminados) porque es el índice de la FK hacia la empresa; lleva `deleted_at` en el INCLUDE para que el
        # conteo del listado (solo vigentes) siga resolviéndose sin leer la tabla (migración 0068).
        Index(
            "ix_employees_company_name",
            "company_id",
            "last_name",
            "first_name",
            "id",
            postgresql_include=["deleted_at"],
        ),
        # Únicos POR EMPRESA entre los empleados VIGENTES (parciales, migración 0068): la misma persona puede trabajar
        # en dos empresas y un empleado en la papelera no bloquea su número, RFC, CURP ni NSS a uno nuevo. Los cuatro
        # son opcionales: un único admite varios NULL (varios empleados sin número), así que no cambian.
        live_unique("uq_employees_company_number", "company_id", "employee_number"),
        live_unique("uq_employees_company_rfc", "company_id", "rfc"),
        live_unique("uq_employees_company_curp", "company_id", "curp"),
        live_unique("uq_employees_company_nss", "company_id", "nss"),
        # Una persona tiene a lo más un empleo VIGENTE por empresa (y puede tener varios en total).
        live_unique("uq_employees_company_user", "company_id", "user_id"),
        # Empleos de una persona (selector de empresa al iniciar sesión) y la FK hacia su cuenta.
        Index("ix_employees_user", "user_id"),
        # Galería facial de la empresa (identificación 1:N): solo empleados vigentes, activos y aprobados.
        Index(
            "ix_employees_company_approved",
            "company_id",
            "id",
            postgresql_where=text("active IS TRUE AND face_status = 'APPROVED' AND deleted_at IS NULL"),
            sqlite_where=text("active = 1 AND face_status = 'APPROVED' AND deleted_at IS NULL"),
        ),
        # Destino de la FK compuesta de los registros faciales: empleado y empresa van juntos.
        UniqueConstraint("id", "company_id", name="uq_employees_id_company"),
        # El departamento es de la MISMA empresa (la BD lo garantiza). No se borra uno con empleados.
        ForeignKeyConstraint(
            ["department_id", "company_id"],
            [f"{WORKFORCE}.departments.id", f"{WORKFORCE}.departments.company_id"],
            name="fk_employees_department_company",
            ondelete="RESTRICT",
        ),
        # Empleados de un departamento en el orden del listado, y su conteo por departamento. Es el índice de la FK
        # hacia el departamento (por eso guarda también a los eliminados) con `deleted_at` en el INCLUDE: el conteo
        # de los vigentes sigue sin leer la tabla.
        Index(
            "ix_employees_company_department",
            "company_id",
            "department_id",
            "last_name",
            "first_name",
            "id",
            postgresql_include=["deleted_at"],
            postgresql_where=text("department_id IS NOT NULL"),
            sqlite_where=text("department_id IS NOT NULL"),
        ),
        # Papelera de la empresa (el más reciente primero) y depuración de los eliminados.
        trash_index("employees", "company_id", "deleted_at", "id"),
        {"schema": WORKFORCE},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey(f"{AUTH}.users.id", ondelete="CASCADE"), nullable=False)
    company_id: Mapped[int] = mapped_column(ForeignKey(f"{TENANCY}.companies.id", ondelete="RESTRICT"), nullable=False)
    # Número de empleado, en mayúsculas. OPCIONAL (decisión del dueño del producto, migración 0076: «el número de
    # empleado debe ser opcional también»): sin capturar es NULL, nunca "". Con valor, su formato y único en la empresa.
    employee_number: Mapped[str | None] = mapped_column(String(30))
    # RFC de persona física, CURP y NSS (IMSS), normalizados en mayúsculas. OPCIONALES (decisión del dueño del
    # producto: la plataforma se abre a otros países, donde no existen): sin capturar es NULL, nunca "" (los índices
    # únicos por empresa admiten varios NULL). Con valor se validan completos y son únicos en la empresa.
    # El teléfono es de la persona (auth.users.phone).
    rfc: Mapped[str | None] = mapped_column(String(13))
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
    # Departamento al que está asignado (a lo más uno; de su misma empresa).
    department_id: Mapped[int | None] = mapped_column()

    user: Mapped[User] = relationship(back_populates="employees", lazy="joined")
    company: Mapped[Company] = relationship(lazy="joined")
    face_embeddings: Mapped[list[FaceEmbedding]] = relationship(
        back_populates="employee",
        primaryjoin="Employee.id == FaceEmbedding.employee_id",
        foreign_keys="FaceEmbedding.employee_id",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    qr_codes: Mapped[list[EmployeeQr]] = relationship(
        back_populates="employee",
        primaryjoin="Employee.id == EmployeeQr.employee_id",
        foreign_keys="EmployeeQr.employee_id",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    @property
    def phone(self) -> str | None:
        """Teléfono de la persona (único en la plataforma; lo comparten todos sus empleos)."""
        return self.user.phone


_SPACE = literal_column("' '", String)


def employee_search_text() -> ColumnElement[str]:
    """Texto de búsqueda del empleado: nombre, apellidos, número, RFC, CURP y NSS, en minúsculas.

    La búsqueda y el índice GIN de trigramas usan esta MISMA expresión (si cambia, el índice
    debe recrearse en una migración: la 0076 lo hizo al volver opcional el número). Los literales van en el SQL, no
    como parámetros, para que PostgreSQL reconozca la expresión del índice también en planes preparados. Cada dato
    opcional va con `coalesce`: un NULL concatenado vuelve NULL todo el texto y la persona ya no se encontraría ni por
    su nombre.
    """
    return func.lower(
        Employee.first_name
        + _SPACE
        + Employee.last_name
        + _SPACE
        + func.coalesce(Employee.employee_number, literal_column("''", String))
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
