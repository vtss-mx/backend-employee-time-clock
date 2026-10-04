from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.db_schemas import AUTH, CATALOG, TENANCY, WORKFORCE
from app.models.enums import DeviceStatus


class ValidatorDevice(Base):
    """Dispositivo (tableta o teléfono) en el que opera un validador de identidad.

    Al iniciar sesión por primera vez en un dispositivo, este genera una llave criptográfica propia
    (ECDSA P-256, no exportable) y firma un reto del servidor; aquí se guarda su llave pública. El
    dispositivo queda PENDING hasta que la empresa lo autorice: solo con un dispositivo APPROVED (que
    vuelve a firmar en cada inicio de sesión) el validador puede operar.
    """

    __tablename__ = "validator_devices"
    __table_args__ = (
        # La misma tableta puede usarse con varias cuentas de validador: una fila por par.
        UniqueConstraint("validator_id", "key_hash"),
        # Dispositivos por autorizar de la empresa.
        Index("ix_validator_devices_company_status", "company_id", "status"),
        Index(
            "ix_validator_devices_reviewed_by_id",
            "reviewed_by_id",
            postgresql_where=text("reviewed_by_id IS NOT NULL"),
            sqlite_where=text("reviewed_by_id IS NOT NULL"),
        ),
        {"schema": WORKFORCE},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    validator_id: Mapped[int] = mapped_column(
        ForeignKey(f"{WORKFORCE}.validators.id", ondelete="CASCADE"), nullable=False
    )  # su índice es el único (validator_id, key_hash)
    company_id: Mapped[int] = mapped_column(ForeignKey(f"{TENANCY}.companies.id", ondelete="CASCADE"), nullable=False)
    #: SHA-256 de la llave pública (identifica al dispositivo sin comparar llaves completas).
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Llave pública ECDSA P-256 (SPKI DER en base64) con la que se verifican sus firmas.
    public_key: Mapped[str] = mapped_column(String(300), nullable=False)
    #: Nombre descriptivo (navegador y sistema) que ve la empresa al autorizarlo.
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    user_agent: Mapped[str | None] = mapped_column(String(400))
    status: Mapped[DeviceStatus] = mapped_column(
        Enum(DeviceStatus, native_enum=False, length=20, validate_strings=True),
        ForeignKey(f"{CATALOG}.device_statuses.code"),
        default=DeviceStatus.PENDING,
        server_default=DeviceStatus.PENDING.value,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_ip: Mapped[str | None] = mapped_column(String(64))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey(f"{AUTH}.users.id", ondelete="SET NULL"))
