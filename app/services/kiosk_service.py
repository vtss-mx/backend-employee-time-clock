"""Kioscos de los sitios: la tableta que muestra el código rotativo de un sitio (antifraude 2b, decisión D9).

Por qué no es un validador: un validador es una CUENTA (inicia sesión, cuenta contra el límite de la empresa y se cobra
como un empleado) que identifica personas; el kiosco solo MUESTRA el código de un sitio. Así que es un dispositivo de la
empresa ligado a un sitio, con una credencial propia y mínima, sin sesión ni pantallas:

1. La empresa lo da de alta en el sitio (Sitios de trabajo › Kioscos, con confirmación) y recibe UNA vez un código de
   vinculación (`XXXXX-XXXXX`, ≈49 bits, vence en `SITE_KIOSK_PAIRING_HOURS`; en la base solo su SHA-256).
2. La tableta abre `/kiosk` (o escanea el QR con `#pair=…`: el fragmento nunca llega a un servidor) y canjea el código
   con la llave pública de su llave WebCrypto NO exportable (`POST /kiosk/pair`): el código ya no sirve otra vez.
3. Cada petición del código (`POST /kiosk/code`) lleva la firma de esa llave sobre `"{reto}.kiosk.{id}"`. El reto es el
   HMAC sin estado de `device_service` en el espacio de nombres de los kioscos y cada respuesta trae el siguiente: sin
   ida y vuelta de más; sin reto o con uno vencido, 403 `KIOSK_PROOF_REQUIRED` con uno nuevo (sin tocar la base).

Copiar lo que guarda la tableta (solo el id del kiosco) a otro equipo no sirve: falta la llave. Eliminar el kiosco
(borrado lógico, regla 20) apaga la tableta al momento; un código de vinculación nuevo desliga la anterior.

Sin estado en el servidor (N réplicas, PgBouncer): el reto es un HMAC, el código se calcula del secreto del sitio
(`site_codes`) y la petición cuesta UNA consulta (kiosco, sitio y empresa) más una escritura cada
`SITE_KIOSK_SEEN_SECONDS` (la última vez que se vio, para que la empresa sepa si su tableta sigue encendida). La
petición es pública: se busca el kiosco en toda la plataforma (aún no se sabe de qué empresa es) y después solo con la
empresa del kiosco (seguridad por fila), como la llave de la API de integración.
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.clock import as_utc
from app.core.config import settings
from app.core.devices import classify_device, device_label
from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ServiceUnavailableError,
    UnprocessableError,
)
from app.core.row_security import use_company, use_platform
from app.i18n import stored
from app.models import SiteKiosk, User
from app.repositories.kiosk_repository import KioskLookup, KioskRepository
from app.schemas.common import PageParams, deletion_of
from app.schemas.kiosk import (
    KioskCode,
    KioskCodeIn,
    KioskCreate,
    KioskCreated,
    KioskList,
    KioskPairIn,
    KioskRead,
    KioskSession,
)
from app.services import site_codes
from app.services.device_service import (
    NonceState,
    issue_nonce,
    key_hash,
    nonce_state,
    public_key_is_valid,
    signature_is_valid,
)
from app.services.site_service import SiteService
from app.services.trash import ensure_deleted, ensure_live

#: Espacio de nombres de los retos de los kioscos (el del kiosco 5 nunca sirve como el de la cuenta 5).
KIOSK_PURPOSE = "kiosk"
#: Letras y números sin ambigüedad al leerlos o escribirlos (sin 0/O, 1/I/L ni U): 30 símbolos.
PAIRING_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
#: Dos grupos de 5 (≈49 bits): se escribe una sola vez y vence; el canje es público pero va limitado por IP.
PAIRING_GROUP = 5
#: Llave del nombre de una tableta que el navegador no permite reconocer (se traduce al leerse).
_DEVICE_KINDS = {"phone": "DEVICE_PHONE", "tablet": "DEVICE_TABLET", "desktop": "DEVICE_DESKTOP"}


def new_pairing_code() -> str:
    def group() -> str:
        return "".join(secrets.choice(PAIRING_ALPHABET) for _ in range(PAIRING_GROUP))

    return f"{group()}-{group()}"


def pairing_hash(code: str) -> str:
    """El SHA-256 del código sin guiones, espacios ni minúsculas (se escribe como se pueda)."""
    normalized = "".join(char for char in code.upper() if char.isalnum())
    return hashlib.sha256(normalized.encode()).hexdigest()


def signed_message(nonce: str, kiosk_id: int) -> str:
    return f"{nonce}.{KIOSK_PURPOSE}.{kiosk_id}"


def kiosk_read(kiosk: SiteKiosk) -> KioskRead:
    return KioskRead(
        id=kiosk.id,
        site_id=kiosk.site_id,
        name=kiosk.name,
        paired=kiosk.key_hash is not None,
        paired_at=kiosk.paired_at,
        device_name=kiosk.device_name,
        last_seen_at=kiosk.last_seen_at,
        pairing_expires_at=kiosk.pairing_expires_at,
        created_at=kiosk.created_at,
        **deletion_of(kiosk),
    )


class KioskService:
    """La empresa administra los kioscos de sus sitios (pantalla «Sitios de trabajo»)."""

    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.sites = SiteService(db, company_id)
        self.kiosks = KioskRepository(db, company_id)

    def list(self, site_id: int, page: PageParams, *, deleted: bool = False) -> KioskList:
        self.sites.get(site_id)
        items, total = self.kiosks.page(site_id, offset=page.offset, limit=page.size, deleted=deleted)
        return KioskList.of([kiosk_read(kiosk) for kiosk in items], total, page)

    def _get(self, site_id: int, kiosk_id: int, *, include_deleted: bool = False) -> SiteKiosk:
        self.sites.get(site_id)
        kiosk = self.kiosks.get(site_id, kiosk_id, include_deleted=include_deleted)
        if kiosk is None:
            raise NotFoundError(code="KIOSK_NOT_FOUND")
        return kiosk

    def create(self, site_id: int, data: KioskCreate) -> KioskCreated:
        self.sites.get(site_id)
        kiosk = self.kiosks.add(SiteKiosk(site_id=site_id, name=data.name, created_at=datetime.now(UTC)))
        return self._with_pairing(kiosk)

    def new_pairing(self, site_id: int, kiosk_id: int) -> KioskCreated:
        """Un código de vinculación nuevo: la tableta que estaba vinculada deja de mostrar el código al momento."""
        kiosk = self._get(site_id, kiosk_id)
        kiosk.key_hash = kiosk.public_key = kiosk.device_name = kiosk.paired_at = None
        return self._with_pairing(kiosk)

    def _with_pairing(self, kiosk: SiteKiosk) -> KioskCreated:
        code = new_pairing_code()
        expires = datetime.now(UTC) + timedelta(hours=settings.SITE_KIOSK_PAIRING_HOURS)
        kiosk.pairing_hash, kiosk.pairing_expires_at = pairing_hash(code), expires
        self.db.commit()
        return KioskCreated(kiosk=kiosk_read(kiosk), pairing_code=code, pairing_expires_at=expires)

    def delete(self, site_id: int, kiosk_id: int, actor: User) -> None:
        """A «Eliminados»: su tableta deja de mostrar el código y su código de vinculación pendiente ya no sirve."""
        kiosk = self._get(site_id, kiosk_id, include_deleted=True)
        ensure_live(kiosk)
        kiosk.pairing_hash = kiosk.pairing_expires_at = None
        kiosk.mark_deleted(datetime.now(UTC), actor.email)
        self.db.commit()

    def restore(self, site_id: int, kiosk_id: int) -> KioskRead:
        """Regresa de «Eliminados» con su tableta vinculada (si la tenía): vuelve a mostrar el código."""
        kiosk = self._get(site_id, kiosk_id, include_deleted=True)
        ensure_deleted(kiosk)
        kiosk.mark_restored()
        self.db.commit()
        return kiosk_read(kiosk)


class KioskDevice:
    """Lo que pide la tableta de un kiosco (sin sesión): vincularse y el código vigente de su sitio."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def pair(self, data: KioskPairIn, user_agent: str | None) -> KioskSession:
        """Canjea el código de vinculación con la llave pública de la tableta (un solo uso)."""
        use_platform(self.db)  # aún no se sabe de qué empresa es: se busca por el hash del código
        found = KioskLookup(self.db).by_pairing(pairing_hash(data.pairing_code))
        now = datetime.now(UTC)
        expires = as_utc(found[0].pairing_expires_at) if found is not None else None
        if found is None or expires is None or expires < now:
            raise UnprocessableError(code="KIOSK_PAIRING_INVALID", field="pairing_code")
        if not public_key_is_valid(data.public_key):
            raise UnprocessableError(code="DEVICE_KEY_INVALID", field="public_key")
        kiosk, site, company = found
        use_company(self.db, kiosk.company_id)  # desde aquí, solo la empresa del kiosco
        name = (data.name or "").strip()[:120] or device_label(user_agent)
        kiosk.key_hash, kiosk.public_key = key_hash(data.public_key), data.public_key
        kiosk.device_name = name or stored(_DEVICE_KINDS[classify_device(user_agent)])
        kiosk.paired_at, kiosk.last_seen_at = now, now
        kiosk.pairing_hash = kiosk.pairing_expires_at = None
        self.db.commit()
        return KioskSession(
            kiosk_id=kiosk.id, site_name=site.name, company_name=company.name, device_nonce=self.nonce(kiosk.id)
        )

    def nonce(self, kiosk_id: int) -> str:
        return issue_nonce(kiosk_id, purpose=KIOSK_PURPOSE)

    def code(self, data: KioskCodeIn) -> KioskCode:
        """El código vigente del sitio para una tableta que prueba su llave (ver el módulo)."""
        self._ensure_nonce(data)
        use_platform(self.db)  # aún no se sabe de qué empresa es: se busca por su id
        found = KioskLookup(self.db).by_id(data.kiosk_id)
        if found is None or found[0].deleted or found[0].public_key is None:
            raise NotFoundError(code="KIOSK_NOT_FOUND", key="KIOSK_UNPAIRED")
        kiosk, site, company = found
        public_key, message = str(kiosk.public_key), signed_message(str(data.nonce), kiosk.id)
        if not signature_is_valid(public_key, message, data.signature or ""):
            raise PermissionDeniedError(code="KIOSK_PROOF_INVALID", details={"nonce": self.nonce(kiosk.id)})
        use_company(self.db, kiosk.company_id)
        if site.deleted or not site.active or not site.presence_code:
            raise ConflictError(code="SITE_CODE_DISABLED")
        secret = site_codes.secret_of(site)
        if secret is None:
            raise ServiceUnavailableError(code="SITE_CODE_UNAVAILABLE", retry_after=settings.SITE_CODE_PERIOD_SECONDS)
        now = datetime.now(UTC)
        seen = as_utc(kiosk.last_seen_at)
        if seen is None or now - seen >= timedelta(seconds=settings.SITE_KIOSK_SEEN_SECONDS):
            KioskRepository(self.db, kiosk.company_id).seen(kiosk.id, now)
        self.db.commit()
        current = site_codes.current(site, secret, now.timestamp())
        return KioskCode(
            site_name=site.name,
            company_name=company.name,
            code=current.code,
            qr=current.qr,
            period_seconds=settings.SITE_CODE_PERIOD_SECONDS,
            expires_in=current.expires_in,
            device_nonce=self.nonce(kiosk.id),
        )

    def _ensure_nonce(self, data: KioskCodeIn) -> None:
        """Sin reto o con uno vencido: que firme uno nuevo; alterado o sin firma: inválido. Sin tocar la base."""
        state = nonce_state(data.kiosk_id, data.nonce, purpose=KIOSK_PURPOSE) if data.nonce else NonceState.EXPIRED
        if state == NonceState.EXPIRED:
            raise PermissionDeniedError(code="KIOSK_PROOF_REQUIRED", details={"nonce": self.nonce(data.kiosk_id)})
        if state == NonceState.INVALID or not data.signature:
            raise PermissionDeniedError(code="KIOSK_PROOF_INVALID", details={"nonce": self.nonce(data.kiosk_id)})
