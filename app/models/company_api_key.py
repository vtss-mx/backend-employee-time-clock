from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.db_schemas import AUTH, CATALOG, TENANCY
from app.models.mixins import company_fk


class CompanyApiKey(Base):
    """Llave de la API de integración de una empresa (nómina, ERP, control de acceso...).

    - Solo da acceso a la información de SU empresa (`company_id`), con los permisos de lectura que
      la empresa le asignó (`scopes`), y únicamente en la API de integración (`/integrations/v1`).
    - El secreto se muestra una sola vez al crearla: en la BD solo queda su SHA-256 (`key_hash`) y
      un prefijo para reconocerla (`prefix`).
    - Revocarla (o rotarla) la apaga al instante; también deja de servir al vencer (`expires_at`)
      o si la empresa se desactiva.
    """

    __tablename__ = "company_api_keys"
    __table_args__ = (
        # FKs con ON DELETE SET NULL (quién la creó o revocó): evitan recorrer la tabla al borrar un usuario.
        Index("ix_company_api_keys_created_by_id", "created_by_id", postgresql_where=text("created_by_id IS NOT NULL")),
        Index("ix_company_api_keys_revoked_by_id", "revoked_by_id", postgresql_where=text("revoked_by_id IS NOT NULL")),
        # Llaves de la empresa, las más recientes primero (listado y tope de activas).
        Index("ix_company_api_keys_company_created", "company_id", "created_at"),
        # Destino de la FK compuesta de sus permisos: la llave y su empresa van juntas.
        UniqueConstraint("id", "company_id", name="uq_company_api_keys_id_company"),
        # El último uso se anota cada minuto de uso: espacio libre para hacerlo en su página (HOT; 0055).
        {"schema": TENANCY, "postgresql_with": {"fillfactor": 90}},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey(f"{TENANCY}.companies.id", ondelete="CASCADE"), nullable=False
    )  # su índice es (company_id, created_at)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey(f"{AUTH}.users.id", ondelete="SET NULL"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by_id: Mapped[int | None] = mapped_column(ForeignKey(f"{AUTH}.users.id", ondelete="SET NULL"))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_ip: Mapped[str | None] = mapped_column(String(45))

    scopes: Mapped[list[CompanyApiKeyScope]] = relationship(
        cascade="all, delete-orphan", lazy="selectin", order_by="CompanyApiKeyScope.scope"
    )

    @property
    def scope_codes(self) -> list[str]:
        return [s.scope for s in self.scopes]


class CompanyApiKeyScope(Base):
    """Un permiso de una llave (catalog.api_scopes). Su empresa la copia el ORM de la llave (FK compuesta)."""

    __tablename__ = "company_api_key_scopes"
    __table_args__ = (
        company_fk("company_api_key_scopes", "api_key_id", f"{TENANCY}.company_api_keys"),
        {"schema": TENANCY},
    )

    api_key_id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(nullable=False)
    scope: Mapped[str] = mapped_column(ForeignKey(f"{CATALOG}.api_scopes.code"), primary_key=True)
