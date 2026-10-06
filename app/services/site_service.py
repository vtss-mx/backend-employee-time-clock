"""Sitios de trabajo de una empresa: dónde se checa "en sitio" (geocerca: punto en el mapa y radio).

Cada turno dice en qué sitios se checa (`shift_sites`). Un sitio puede activar su código (antifraude 2b, decisión D9;
apagado por omisión): la entrada y la salida en él llevan el código rotativo de su kiosco (`site_codes`,
`kiosk_service`); al activarlo por primera vez se le genera su secreto (cifrado; nunca sale del servidor).

Un sitio que algún turno usa no se elimina (409
`SITE_IN_USE` con los turnos que lo usan: se quita de ellos o se desactiva), ni uno donde ya se checó
(409 `SITE_HAS_RECORDS`: su historial lo nombra). Desactivado, deja de aceptar registros en sitio. Eliminar es un
borrado lógico (regla 20 de la raíz): va a «Eliminados», su nombre queda libre y restaurarlo revisa que siga libre.
"""

from datetime import UTC, datetime
from typing import cast

from sqlalchemy.orm import Session

from app.core.clock import business_today
from app.core.exceptions import ConflictError, NotFoundError
from app.i18n import Text
from app.models import User, WorkSite
from app.repositories.kiosk_repository import KioskRepository
from app.repositories.shift_repository import ShiftRepository
from app.schemas.address import Address, address_of, apply_address
from app.schemas.common import PageParams, deletion_of
from app.schemas.shift import SiteCreate, SiteList, SiteRead, SiteRef, SiteUpdate
from app.services import site_codes
from app.services.trash import commit_restore, ensure_deleted, ensure_live, ensure_name_free

#: Turnos que se nombran al explicar por qué un sitio no se puede borrar.
SHIFTS_NAMED = 5


def site_ref(site: WorkSite) -> SiteRef:
    # Un sitio siempre tiene su punto y su domicilio (CHECK ck_work_sites_coordinates y el alta).
    latitude, longitude = cast(float, site.latitude), cast(float, site.longitude)
    return SiteRef(
        id=site.id,
        name=site.name,
        address=cast(Address, address_of(site)),
        latitude=latitude,
        longitude=longitude,
        radius_m=site.radius_m,
        active=site.active,
        deleted=site.deleted,
    )


def _names(names: list[str]) -> list[str] | Text:
    """Los turnos en una frase (se unen como se dice en el idioma): "Matutino", "Matutino y Nocturno" o "Matutino,
    Nocturno, ... y otros"."""
    shown = names[:SHIFTS_NAMED]
    if len(names) > SHIFTS_NAMED:
        return Text("LIST_AND_OTHERS", {"items": ", ".join(shown)})
    return shown


class SiteService:
    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.repo = ShiftRepository(db, company_id)
        self.company_id = company_id

    def _read(self, site: WorkSite, employees: int, kiosks: int = 0) -> SiteRead:
        return SiteRead(
            id=site.id,
            name=site.name,
            address=cast(Address, address_of(site)),  # un sitio siempre tiene domicilio
            radius_m=site.radius_m,
            active=site.active,
            employees=employees,
            presence_code=site.presence_code,
            kiosks=kiosks,
            created_at=site.created_at,
            **deletion_of(site),
        )

    def get(self, site_id: int, *, include_deleted: bool = False) -> WorkSite:
        """El vigente (404 si no existe o está en «Eliminados»); con `include_deleted`, también uno eliminado."""
        site = self.repo.site(site_id, include_deleted=include_deleted)
        if site is None:
            raise NotFoundError(code="SITE_NOT_FOUND")
        return site

    def read(self, site_id: int, *, include_deleted: bool = False) -> SiteRead:
        site = self.get(site_id, include_deleted=include_deleted)
        employees = self.repo.employees_per_site([site.id], business_today()).get(site.id, 0)
        return self._read(site, employees, self._kiosks().per_site([site.id]).get(site.id, 0))

    def search(self, *, search: str | None, active: bool | None, page: PageParams, deleted: bool = False) -> SiteList:
        """Los vigentes o, con `deleted`, la papelera (el eliminado más reciente primero)."""
        items, total = self.repo.sites(
            search=search, active=active, offset=page.offset, limit=page.size, deleted=deleted
        )
        ids = [s.id for s in items]
        employees = self.repo.employees_per_site(ids, business_today())
        kiosks = self._kiosks().per_site(ids)
        return SiteList.of([self._read(s, employees.get(s.id, 0), kiosks.get(s.id, 0)) for s in items], total, page)

    def _kiosks(self) -> KioskRepository:
        return KioskRepository(self.db, self.company_id)

    @staticmethod
    def _presence(site: WorkSite, enabled: bool | None) -> None:
        """Activa o apaga el código del sitio; al activarlo por primera vez se genera su secreto (se conserva al
        apagarlo: volver a activarlo no cambia los kioscos ya vinculados)."""
        if enabled is None:
            return
        site.presence_code = enabled
        if enabled and not site.presence_secret:
            site.presence_secret = site_codes.new_secret()

    def _ensure_name(self, name: str, exclude_id: int | None = None) -> None:
        if self.repo.site_name_exists(name, exclude_id):
            raise ConflictError(code="SITE_NAME_TAKEN", field="name")

    def create(self, data: SiteCreate) -> SiteRead:
        self._ensure_name(data.name)
        site = WorkSite(name=data.name, radius_m=data.radius_m)
        apply_address(site, data.address)
        self._presence(site, data.presence_code)
        self.repo.add(site)
        self.db.commit()
        return self._read(site, 0)

    def update(self, site_id: int, data: SiteUpdate) -> SiteRead:
        site = self.get(site_id)
        self._ensure_name(data.name, exclude_id=site.id)
        site.name, site.radius_m = data.name, data.radius_m
        apply_address(site, data.address)
        self._presence(site, data.presence_code)
        self.db.commit()
        return self.read(site.id)

    def set_active(self, site_id: int, active: bool) -> SiteRead:
        site = self.get(site_id)
        site.active = active
        self.db.commit()
        return self.read(site.id)

    def delete(self, site_id: int, actor: User) -> None:
        """A «Eliminados», solo un sitio que ningún turno vigente usa y donde nadie ha checado (su depuración también
        lo exige: sus llaves foráneas son RESTRICT)."""
        site = self.get(site_id, include_deleted=True)
        ensure_live(site)
        shifts = self.repo.shifts_using_site(site.id, limit=SHIFTS_NAMED + 1)
        if shifts:
            raise ConflictError(
                code="SITE_IN_USE",
                params={"count": len(shifts), "shifts": _names(shifts)},
                details={"shifts": shifts[:SHIFTS_NAMED]},
            )
        if self.repo.site_has_records(site.id):
            raise ConflictError(
                code="SITE_HAS_RECORDS",
            )
        site.mark_deleted(datetime.now(UTC), actor.email)
        self.db.commit()

    def restore(self, site_id: int) -> SiteRead:
        """Regresa de «Eliminados» si su nombre sigue libre (409 `RESTORE_CONFLICT`), con su estado de antes."""
        site = self.get(site_id, include_deleted=True)
        ensure_deleted(site)
        ensure_name_free(self.repo.site_name_exists(site.name), site.name)
        site.mark_restored()
        commit_restore(self.db)
        return self.read(site.id)
