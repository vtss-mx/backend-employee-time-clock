from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.db_schemas import TENANCY, WORKFORCE
from app.core.soft_delete import SoftDeleteMixin
from app.models.mixins import company_fk, trash_index

#: Largo máximo del nombre de un kiosco ("Recepción planta 1").
KIOSK_NAME_MAX = 80


class SiteKiosk(SoftDeleteMixin, Base):
    """Kiosco de un sitio de trabajo (antifraude 2b, decisión D9): la tableta que muestra el código rotativo del sitio.

    No es una cuenta (no inicia sesión, no cuenta como validador ni se cobra): es un dispositivo de la empresa ligado a
    UN sitio. La empresa lo da de alta y recibe un código de vinculación de un solo uso (aquí solo su SHA-256 y su
    vencimiento); la tableta lo canjea con la llave pública de su llave WebCrypto NO exportable (`utils/deviceKey.ts`)
    y desde entonces cada petición del código lleva la firma de esa llave sobre un reto del servidor
    (`kiosk_service`): copiar lo que guarda la tableta a otro equipo no sirve. Se guardan solo el hash y la llave
    pública (nunca la privada).

    Con borrado lógico (regla 20 de la raíz): eliminarlo apaga la tableta al momento y restaurarlo la vuelve a
    encender (conserva su vinculación). Generar un código de vinculación nuevo desliga la tableta anterior.
    """

    __tablename__ = "site_kiosks"
    __table_args__ = (
        # Los kioscos vigentes de un sitio (el más reciente primero: ORDER BY id DESC), su conteo por sitio en el
        # listado de sitios y la FK compuesta hacia el sitio (borrar un sitio depurado recorre solo sus filas: por eso
        # es completo y lleva `deleted_at` en el INCLUDE, que filtra lo vigente sin leer la tabla).
        Index("ix_site_kiosks_site", "company_id", "site_id", "id", postgresql_include=["deleted_at"]),
        # Canjear un código de vinculación (petición pública, sin empresa: se busca por su hash en toda la plataforma).
        Index(
            "uq_site_kiosks_pairing",
            "pairing_hash",
            unique=True,
            postgresql_where=text("pairing_hash IS NOT NULL"),
            sqlite_where=text("pairing_hash IS NOT NULL"),
        ),
        # Papelera de cada sitio (el más reciente primero) y depuración de lo eliminado.
        trash_index("site_kiosks", "company_id", "site_id", "deleted_at", "id"),
        # El sitio es de la MISMA empresa del kiosco (FK compuesta).
        company_fk("site_kiosks", "site_id", f"{WORKFORCE}.work_sites"),
        # Cada tanto anota la última vez que pidió su código (SITE_KIOSK_SEEN_SECONDS): espacio para hacerlo en su
        # página (HOT).
        {"schema": WORKFORCE, "postgresql_with": {"fillfactor": 90}},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey(f"{TENANCY}.companies.id", ondelete="CASCADE"), nullable=False)
    site_id: Mapped[int] = mapped_column(nullable=False)  # su índice es ix_site_kiosks_site
    name: Mapped[str] = mapped_column(String(KIOSK_NAME_MAX), nullable=False)
    #: SHA-256 del código de vinculación vigente (None: ya se canjeó o se eliminó) y hasta cuándo vale.
    pairing_hash: Mapped[str | None] = mapped_column(String(64))
    pairing_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: La llave de la tableta vinculada: su SHA-256 y la llave pública (SPKI DER, base64) que verifica sus firmas.
    key_hash: Mapped[str | None] = mapped_column(String(64))
    public_key: Mapped[str | None] = mapped_column(String(300))
    #: Nombre amigable del navegador de la tableta ("iPad · Safari") o la llave de su tipo (se traduce al leerse).
    device_name: Mapped[str | None] = mapped_column(String(120))
    paired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
