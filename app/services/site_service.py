"""Sitios de trabajo de una empresa: dónde se checa "en sitio" (geocerca: punto en el mapa y radio).

Un sitio que alguna asignación usa no se borra (su historial lo nombra): se desactiva. Desactivado,
deja de aceptar registros en sitio.
"""

from typing import cast

from sqlalchemy.orm import Session

from app.core.clock import business_today
from app.core.exceptions import ConflictError, NotFoundError
from app.models import WorkSite
from app.repositories.shift_repository import ShiftRepository
from app.schemas.address import Address, address_of, apply_address
from app.schemas.common import PageParams
from app.schemas.shift import SiteCreate, SiteList, SiteRead, SiteRef, SiteUpdate

SITE_NAME_TAKEN = "Ya existe un sitio con ese nombre"


def site_ref(site: WorkSite) -> SiteRef:
    # Un sitio siempre tiene su punto (CHECK ck_work_sites_coordinates).
    latitude, longitude = cast(float, site.latitude), cast(float, site.longitude)
    return SiteRef(id=site.id, name=site.name, latitude=latitude, longitude=longitude, radius_m=site.radius_m)


class SiteService:
    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.repo = ShiftRepository(db, company_id)

    def _read(self, site: WorkSite, employees: int) -> SiteRead:
        return SiteRead(
            id=site.id,
            name=site.name,
            address=cast(Address, address_of(site)),  # un sitio siempre tiene domicilio
            radius_m=site.radius_m,
            active=site.active,
            employees=employees,
            created_at=site.created_at,
        )

    def get(self, site_id: int) -> WorkSite:
        site = self.repo.site(site_id)
        if site is None:
            raise NotFoundError("Sitio no encontrado", code="SITE_NOT_FOUND")
        return site

    def read(self, site_id: int) -> SiteRead:
        site = self.get(site_id)
        return self._read(site, self.repo.employees_per_site([site.id], business_today()).get(site.id, 0))

    def search(self, *, search: str | None, active: bool | None, page: PageParams) -> SiteList:
        items, total = self.repo.sites(search=search, active=active, offset=page.offset, limit=page.size)
        employees = self.repo.employees_per_site((s.id for s in items), business_today())
        return SiteList.of([self._read(s, employees.get(s.id, 0)) for s in items], total, page)

    def _ensure_name(self, name: str, exclude_id: int | None = None) -> None:
        if self.repo.site_name_exists(name, exclude_id):
            raise ConflictError(SITE_NAME_TAKEN, code="SITE_NAME_TAKEN", field="name")

    def create(self, data: SiteCreate) -> SiteRead:
        self._ensure_name(data.name)
        site = WorkSite(name=data.name, radius_m=data.radius_m)
        apply_address(site, data.address)
        self.repo.add(site)
        self.db.commit()
        return self._read(site, 0)

    def update(self, site_id: int, data: SiteUpdate) -> SiteRead:
        site = self.get(site_id)
        self._ensure_name(data.name, exclude_id=site.id)
        site.name, site.radius_m = data.name, data.radius_m
        apply_address(site, data.address)
        self.db.commit()
        return self.read(site.id)

    def set_active(self, site_id: int, active: bool) -> SiteRead:
        site = self.get(site_id)
        site.active = active
        self.db.commit()
        return self.read(site.id)

    def delete(self, site_id: int) -> None:
        site = self.get(site_id)
        if self.repo.site_in_use(site.id):
            raise ConflictError(
                "El sitio está en asignaciones de turno: desactívalo en lugar de eliminarlo", code="SITE_IN_USE"
            )
        self.repo.delete_site(site)
        self.db.commit()
