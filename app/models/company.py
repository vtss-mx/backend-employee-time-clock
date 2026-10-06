from datetime import datetime
from typing import NamedTuple

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ColumnElement,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    false,
    func,
    literal_column,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.db_schemas import CATALOG, TENANCY
from app.models.mixins import SoftDeleteMixin, TimestampMixin, live_unique, trash_index

#: Tope de validadores activos que el ADMIN puede darle a una empresa (uno por acceso: sobra para un corporativo con
#: muchos sitios). La base solo exige que no sea negativo; el tope lo valida la API.
VALIDATORS_MAX = 1_000
#: Predicado del índice único del identificador fiscal: las empresas vigentes que lo capturaron.
TAX_ID_LIVE = "deleted_at IS NULL AND tax_id IS NOT NULL"
#: El tipo que además se copia a la columna anterior `rfc` (`catalog.tax_id_types`).
RFC_TAX_ID_TYPE = "MX_RFC"


class TaxId(NamedTuple):
    """Identificador fiscal completo de una empresa: país (ISO 3166-1 alfa-2), tipo y número normalizado."""

    country: str
    type: str
    number: str


class Company(SoftDeleteMixin, TimestampMixin, Base):
    """Empresa cliente (tenant). Sus usuarios, empleados, validaciones, bitácora y política de
    verificación están aislados del resto por `company_id`.

    Datos fiscales/de contacto opcionales en la BD solo por la empresa creada al migrar desde la
    versión de una sola empresa; la API los exige al dar de alta.

    Con borrado lógico (migración 0068): la elimina el ADMIN (solo sin empleados vigentes ni movimientos de cobranza)
    junto con sus cuentas; su identificador fiscal queda libre y restaurarla regresa lo que se eliminó con ella.

    Identificador fiscal de cualquier país (migración 0074): país fiscal + tipo (`catalog.tax_id_types`) + número
    normalizado. Opcional: los tres van juntos o ninguno (CHECK `tax_id`); sin número no se guardan ni país ni tipo.
    Único entre las empresas vigentes por (país, tipo, número). `rfc` es la columna anterior: se sigue escribiendo
    (el número cuando el tipo es RFC, si no NULL) mientras la versión anterior pueda estar en marcha y se quita en una
    migración posterior (README, «Pendiente»); nada nuevo la lee.
    """

    __tablename__ = "companies"
    __table_args__ = (
        # Límite del plan: sin límite (NULL) o al menos un empleado.
        CheckConstraint("max_employees IS NULL OR max_employees > 0", name="max_employees_positive"),
        # Validadores activos permitidos: 0 (módulo apagado) o más.
        CheckConstraint("max_validators >= 0", name="max_validators"),
        # Suspendida = con fecha y motivo; activa = sin ninguno de los dos.
        CheckConstraint("(suspended_at IS NULL) = (suspension_reason IS NULL)", name="suspension"),
        # Empresas suspendidas (resumen de cobranza y su filtro): pocas, índice parcial.
        Index(
            "ix_companies_suspended_at",
            "suspended_at",
            postgresql_where=text("suspended_at IS NOT NULL"),
            sqlite_where=text("suspended_at IS NOT NULL"),
        ),
        # RFC único entre las empresas VIGENTES (columna anterior; se quita junto con ella).
        live_unique("ix_companies_rfc", "rfc"),
        # Identificador fiscal: los tres datos juntos o ninguno (sin número no hay país ni tipo que guardar).
        CheckConstraint(
            "(tax_id IS NULL) = (tax_id_type IS NULL) AND (tax_id IS NULL) = (tax_country IS NULL)", name="tax_id"
        ),
        # Único entre las empresas VIGENTES que lo capturaron, por país, tipo y número: sirve a la revisión del alta, la
        # edición, la restauración y la validación en vivo (las tres igualdades; la de lo vigente la pone el borrado
        # lógico). Parcial: las empresas sin identificador no ocupan el índice.
        Index(
            "uq_companies_tax_id",
            "tax_country",
            "tax_id_type",
            "tax_id",
            unique=True,
            postgresql_where=text(TAX_ID_LIVE),
            sqlite_where=text(TAX_ID_LIVE),
        ),
        # Papelera del ADMIN (la más reciente primero) y depuración de las eliminadas.
        trash_index("companies", "deleted_at", "id"),
        {"schema": TENANCY},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)  # nombre comercial
    legal_name: Mapped[str | None] = mapped_column(String(200))  # razón social
    #: Columna anterior al identificador fiscal (solo RFC): se escribe junto con él y se quita después (no se lee).
    rfc: Mapped[str | None] = mapped_column(String(13))
    #: País fiscal (ISO 3166-1 alfa-2) y tipo de identificador; FK a catálogos que nunca se borran: sin índice (§3.1.8).
    tax_country: Mapped[str | None] = mapped_column(String(2), ForeignKey(f"{CATALOG}.countries.code"))
    tax_id_type: Mapped[str | None] = mapped_column(String(30), ForeignKey(f"{CATALOG}.tax_id_types.code"))
    #: Número normalizado (mayúsculas, sin espacios, guiones, puntos ni diagonales): así se compara y se busca.
    tax_id: Mapped[str | None] = mapped_column(String(30))
    phone: Mapped[str | None] = mapped_column(String(16))  # E.164: +<lada><número>
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)
    #: Límite de empleados del plan (None = sin límite).
    max_employees: Mapped[int | None] = mapped_column(Integer)
    #: Acceso al módulo de Integraciones (API): lo decide el ADMIN de la plataforma. Sin él, la
    #: pantalla no aparece, no se administran llaves y las que existan dejan de funcionar (no se borran).
    api_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    #: Validadores ACTIVOS que puede tener (lo decide el ADMIN; decisión del dueño del producto). 0 = el módulo de
    #: validadores está apagado: su pantalla no aparece y sus APIs responden 403 VALIDATORS_DISABLED. Es opcional y
    #: cuesta: cada validador activo cuenta como un empleado en el cobro (`billing.headcount_days`). Nunca queda por
    #: debajo de los validadores activos que ya tiene (`CompanyService.update`): ninguno se queda sin servicio.
    max_validators: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    #: Suspensión por falta de pago (automática) o por decisión del ADMIN: nadie de la empresa inicia
    #: sesión ni opera hasta que se reactive (403 COMPANY_SUSPENDED). Vive junto a `active` porque la
    #: autenticación de CADA petición ya carga la empresa (sin consultas de más); el plan y el saldo
    #: viven en el esquema `billing`. `suspended_by` es el correo del ADMIN (vacío si fue automática).
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    suspension_reason: Mapped[str | None] = mapped_column(String(30), ForeignKey(f"{CATALOG}.suspension_reasons.code"))
    suspension_note: Mapped[str | None] = mapped_column(String(300))
    suspended_by: Mapped[str | None] = mapped_column(String(255))

    @property
    def validators_enabled(self) -> bool:
        """¿Tiene el módulo de validadores? (un límite mayor que cero)."""
        return self.max_validators > 0

    @property
    def suspended(self) -> bool:
        return self.suspended_at is not None

    @property
    def tax(self) -> TaxId | None:
        """Su identificador fiscal completo, o None si no lo capturó (los tres datos van juntos: CHECK `tax_id`)."""
        if self.tax_country and self.tax_id_type and self.tax_id:
            return TaxId(self.tax_country, self.tax_id_type, self.tax_id)
        return None

    @property
    def legacy_rfc(self) -> str | None:
        """El número si su identificador es un RFC; si no, None: lo que siguen entregando los contratos que tenían
        `rfc` (obsoleto). Sale del identificador, no de la columna anterior, que se quitará."""
        return self.tax_id if self.tax_id_type == RFC_TAX_ID_TYPE else None

    def set_tax(self, tax: TaxId | None) -> None:
        """Guarda el identificador fiscal (o lo borra) y, mientras exista, la columna anterior `rfc`: el número si es
        un RFC, si no NULL (la versión anterior en marcha durante un despliegue sigue leyendo y revisando su RFC)."""
        self.tax_country, self.tax_id_type, self.tax_id = tax if tax is not None else (None, None, None)
        self.rfc = self.legacy_rfc

    @property
    def billing_status(self) -> str:
        """Código de `catalog.billing_statuses` (se deriva de la suspensión)."""
        return "SUSPENDED" if self.suspended else "ACTIVE"


_SPACE = literal_column("' '", String)


def company_search_text() -> ColumnElement[str]:
    """Texto de búsqueda de la empresa (nombre, razón social e identificador fiscal, normalizado como se guarda). Misma
    expresión que su índice de trigramas (migración 0074: antes llevaba el RFC; los RFC pasaron a `tax_id`)."""
    return func.lower(
        Company.name
        + _SPACE
        + func.coalesce(Company.legal_name, literal_column("''", String))
        + _SPACE
        + func.coalesce(Company.tax_id, literal_column("''", String))
    )


Index("ix_companies_name", func.lower(Company.name).label("name_lower"))
Index(
    "ix_companies_search_trgm",
    company_search_text().label("search_text"),
    postgresql_using="gin",
    postgresql_ops={"search_text": "gin_trgm_ops"},
).ddl_if(dialect="postgresql")
