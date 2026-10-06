from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Boolean, CheckConstraint, DateTime, Enum, ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.db_schemas import AUTH, CATALOG, TENANCY
from app.models.enums import UserRole
from app.models.mixins import SoftDeleteMixin, TimestampMixin, live_unique, trash_index

if TYPE_CHECKING:
    from app.models.company import Company
    from app.models.employee import Employee
    from app.models.validator import Validator


class User(SoftDeleteMixin, TimestampMixin, Base):
    """Cuenta de una persona. Con borrado lógico (migración 0068): una cuenta eliminada (validador, administradores de
    una empresa eliminada, empleado sin otro empleo vigente) no inicia sesión y su correo y su teléfono quedan libres
    para otra; su foto de perfil se borra de verdad al eliminarla (regla 13)."""

    __tablename__ = "users"
    __table_args__ = (
        # Administradores de una empresa (y conteos por rol en la consola de la plataforma). Completo (sin parcial): es
        # también el índice de la FK hacia la empresa (la depuración de una empresa revisa TODAS sus cuentas).
        Index("ix_users_company_role", "company_id", "role"),
        # Correo y teléfono únicos entre las cuentas VIGENTES (inicio de sesión por correo, validación en vivo).
        live_unique("ix_users_email", "email"),
        live_unique("uq_users_phone", "phone"),
        # Depuración de las cuentas eliminadas hace más de SOFT_DELETE_RETENTION_DAYS (no tienen papelera propia: se
        # ven en la de su empleado, su validador o su empresa).
        trash_index("users", "deleted_at"),
        # Conteos por rol de toda la plataforma (indicadores del ADMIN) sin recorrer la tabla.
        Index("ix_users_role", "role"),
        # Destino de la FK compuesta de los validadores: la cuenta y su empresa van juntas.
        UniqueConstraint("id", "company_id", name="uq_users_id_company"),
        # COMPANY y VALIDATOR pertenecen a UNA empresa por su cuenta. ADMIN es de la plataforma y
        # EMPLOYEE trabaja en una o varias empresas a través de sus registros de empleado.
        CheckConstraint("(role IN ('COMPANY', 'VALIDATOR')) = (company_id IS NOT NULL)", name="role_company"),
        {"schema": AUTH},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Solo roles del catálogo (catalog.roles): la BD lo garantiza aunque se escriba fuera de la API.
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, native_enum=False, length=20, validate_strings=True),
        ForeignKey(f"{CATALOG}.roles.code"),
        nullable=False,
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Teléfono celular de la persona (E.164). Único en la plataforma, igual que el correo.
    phone: Mapped[str | None] = mapped_column(String(16))
    #: Empresa del administrador (COMPANY) o del validador (VALIDATOR). None para ADMIN y EMPLOYEE.
    company_id: Mapped[int | None] = mapped_column(ForeignKey(f"{TENANCY}.companies.id", ondelete="RESTRICT"))
    #: Preferencias de la interfaz (p. ej. menú contraído): viajan con el usuario a cualquier
    #: dispositivo. Se validan con `UserPreferences` (schemas/user.py).
    preferences: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False, default=dict, server_default=text("'{}'")
    )
    #: Versión de su foto de perfil vigente (None = sin foto). La referencia de cada tamaño vive en
    #: `auth.user_avatars`; aquí se repite la versión para armar la URL sin otra consulta (la cuenta ya se
    #: carga en `/users/me`, el inicio de sesión y los listados de empleados).
    avatar_version: Mapped[str | None] = mapped_column(String(24))

    #: Empleos de la persona: uno por empresa (EMPLOYEE puede trabajar en varias).
    employees: Mapped[list[Employee]] = relationship(
        back_populates="user", lazy="selectin", order_by="Employee.id", passive_deletes=True
    )
    # joined: al validar la sesión en cada petición se sabe si la empresa sigue activa sin otra consulta.
    company: Mapped[Company | None] = relationship(lazy="joined")
    #: Configuración del validador de identidad (rol VALIDATOR). Se lee solo cuando se usa (la cuenta
    #: del validador que opera): con "selectin", cada listado que cargaba cuentas (empleados, tablero,
    #: historial, administradores) hacía una consulta más a `validators` que nadie leía.
    validator: Mapped[Validator | None] = relationship(
        back_populates="user",
        foreign_keys="Validator.user_id",
        uselist=False,
        lazy="select",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    # ---------- Empresa en la que opera (la elige la sesión) ----------

    def use_company(self, company_id: int | None) -> None:
        """Empresa elegida en la sesión actual (EMPLOYEE). La fija la autenticación en cada petición."""
        vars(self)["_session_company_id"] = company_id

    @property
    def session_company_id(self) -> int | None:
        value = vars(self).get("_session_company_id")
        return value if isinstance(value, int) else None

    def use_device_key(self, key_hash: str | None) -> None:
        """La llave de dispositivo a la que está ligada la sesión actual (validadores, antifraude 2b). La fija la
        autenticación en cada petición, con la sesión ya cargada: firmar una identificación no vuelve a leerla."""
        vars(self)["_session_device_key"] = key_hash

    @property
    def session_device_key(self) -> str | None:
        value = vars(self).get("_session_device_key")
        return value if isinstance(value, str) else None

    @property
    def employee(self) -> Employee | None:
        """Empleo en la empresa elegida en la sesión; si la persona solo tiene uno, ese."""
        if self.role != UserRole.EMPLOYEE:
            return None
        chosen = self.session_company_id
        if chosen is None:
            return self.employees[0] if len(self.employees) == 1 else None
        return next((e for e in self.employees if e.company_id == chosen), None)

    @property
    def usable_employees(self) -> list[Employee]:
        """Empleos con los que puede entrar: activos y en una empresa activa y no suspendida."""
        return [e for e in self.employees if e.active and e.company.active and not e.company.suspended]

    @property
    def current_company(self) -> Company | None:
        """Empresa en la que opera: la suya (COMPANY, VALIDATOR) o la elegida en la sesión (EMPLOYEE)."""
        if self.role in (UserRole.COMPANY, UserRole.VALIDATOR):
            return self.company
        employee = self.employee
        return employee.company if employee else None


# Búsqueda de empleados por fragmento de correo (LIKE '%texto%'). El correo ya se guarda en
# minúsculas, así que no hace falta lower() ni en el índice ni en la consulta.
Index("ix_users_email_trgm", User.email, postgresql_using="gin", postgresql_ops={"email": "gin_trgm_ops"}).ddl_if(
    dialect="postgresql"
)
