from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import ColumnElement, func, select, update
from sqlalchemy.orm import Session

from app.core.soft_delete import with_deleted
from app.models import Company, SiteKiosk, WorkSite
from app.repositories.aggregates import affected_rows, get_scoped, group_counts, paginate, trash_page


class KioskRepository:
    """Kioscos de los sitios de UNA empresa (antifraude 2b): su administración y lo que pide la tableta."""

    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id

    def get(self, site_id: int, kiosk_id: int, *, include_deleted: bool = False) -> SiteKiosk | None:
        """El kiosco de ESE sitio de la empresa (otro sitio u otra empresa: None, la ruta responde 404)."""
        kiosk = get_scoped(self.db, SiteKiosk, kiosk_id, self.company_id, include_deleted=include_deleted)
        return kiosk if kiosk is not None and kiosk.site_id == site_id else None

    def add(self, kiosk: SiteKiosk) -> SiteKiosk:
        kiosk.company_id = self.company_id
        self.db.add(kiosk)
        self.db.flush()
        return kiosk

    def page(self, site_id: int, *, offset: int, limit: int, deleted: bool = False) -> tuple[list[SiteKiosk], int]:
        """Los vigentes del sitio, el más reciente primero (`ix_site_kiosks_site` hacia atrás) o, con `deleted`, su
        papelera (`ix_site_kiosks_deleted`)."""
        stmt = select(SiteKiosk).where(SiteKiosk.company_id == self.company_id, SiteKiosk.site_id == site_id)
        if deleted:
            return trash_page(self.db, stmt, SiteKiosk, offset=offset, limit=limit)
        return paginate(self.db, stmt, (SiteKiosk.id.desc(),), offset=offset, limit=limit)

    def per_site(self, site_ids: Iterable[int]) -> dict[int, int]:
        """Kioscos vigentes de cada sitio de una página (UN `GROUP BY` por el índice del sitio, sin leer la tabla)."""
        ids = list(site_ids)
        if not ids:
            return {}
        return group_counts(
            self.db,
            select(SiteKiosk.site_id, func.count())
            .where(SiteKiosk.company_id == self.company_id, SiteKiosk.site_id.in_(ids))
            .group_by(SiteKiosk.site_id),
        )

    def seen(self, kiosk_id: int, now: datetime) -> None:
        """Anota la última vez que la tableta pidió su código (una sentencia por su llave primaria; quien llama la hace
        solo cada `SITE_KIOSK_SEEN_SECONDS`)."""
        affected_rows(
            self.db,
            update(SiteKiosk)
            .where(SiteKiosk.id == kiosk_id, SiteKiosk.company_id == self.company_id)
            .values(last_seen_at=now),
        )


class KioskLookup:
    """Lo que la tableta de un kiosco pide sin sesión: se busca en toda la PLATAFORMA (aún no se sabe de qué empresa
    es) y después la petición sigue solo con la empresa del kiosco (`kiosk_service`). Cada búsqueda trae el kiosco, su
    sitio y su empresa en UNA consulta (también en «Eliminados»: quien llama decide qué hacer con cada uno)."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def _find(self, condition: ColumnElement[bool]) -> tuple[SiteKiosk, WorkSite, Company] | None:
        stmt = (
            select(SiteKiosk, WorkSite, Company)
            .join(WorkSite, (WorkSite.id == SiteKiosk.site_id) & (WorkSite.company_id == SiteKiosk.company_id))
            .join(Company, Company.id == SiteKiosk.company_id)
            .where(condition)
        )
        row = self.db.execute(with_deleted(stmt)).first()
        return (row[0], row[1], row[2]) if row is not None else None

    def by_pairing(self, pairing_hash: str) -> tuple[SiteKiosk, WorkSite, Company] | None:
        """Por el hash de su código de vinculación (`uq_site_kiosks_pairing`; uno eliminado ya no tiene código)."""
        return self._find(SiteKiosk.pairing_hash == pairing_hash)

    def by_id(self, kiosk_id: int) -> tuple[SiteKiosk, WorkSite, Company] | None:
        """Por su id (la llave primaria; un kiosco eliminado ya no existe para su tableta: lo decide quien llama)."""
        return self._find(SiteKiosk.id == kiosk_id)
