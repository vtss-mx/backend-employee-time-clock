"""Llaves de la API de integración: cada empresa conecta sus sistemas (nómina, ERP...) a SU información.

Formato de la llave:  "tck_<43 caracteres>" (32 bytes aleatorios, 256 bits).
- Se muestra UNA sola vez (al crearla o rotarla); en la BD solo queda su SHA-256 y un prefijo para
  reconocerla. Ni la propia empresa puede volver a verla: si se pierde, se rota.
- Se presenta en la cabecera `X-API-Key` y solo sirve en la API de integración (`/integrations/v1`);
  la sesión de un usuario no sirve ahí, ni la llave en el resto de la API.
- La empresa sale SIEMPRE de la llave (nunca de la petición) y cada permiso es de solo lectura
  (catalog.api_scopes). Revocarla, que venza o que la empresa se desactive la apaga al instante.
"""

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.clock import as_utc, has_passed
from app.core.config import settings
from app.core.crypto import hash_token
from app.core.exceptions import (
    ApiKeyAuthenticationError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    UnprocessableError,
)
from app.middleware.rate_limit import enforce
from app.models import ApiKeyStatus, Company, CompanyApiKey, CompanyApiKeyScope, User
from app.repositories.api_key_repository import ApiKeyRepository, find_by_hash
from app.repositories.user_repository import UserRepository
from app.schemas.api_key import ApiKeyCreate, ApiKeyCreated, ApiKeyList, ApiKeyRead
from app.schemas.common import PageParams
from app.services.catalog_service import get_catalogs

API_KEY_PREFIX = "tck_"
SECRET_BYTES = 32
#: Caracteres visibles para reconocer una llave ("tck_" + 8).
PREFIX_LENGTH = 12
_MAX_KEY_LENGTH = 128


@dataclass(frozen=True)
class ApiClient:
    """Quién llama a la API de integración: una llave de UNA empresa, con sus permisos."""

    key_id: int
    company_id: int
    name: str
    prefix: str
    scopes: frozenset[str]
    expires_at: datetime | None


def key_status(key: CompanyApiKey, now: datetime | None = None) -> ApiKeyStatus:
    if key.revoked_at is not None:
        return ApiKeyStatus.REVOKED
    if key.expires_at is not None and has_passed(key.expires_at, now):
        return ApiKeyStatus.EXPIRED
    return ApiKeyStatus.ACTIVE


def authenticate(db: Session, raw_key: str | None, ip: str | None) -> ApiClient:
    """Valida la llave de la cabecera y devuelve a qué empresa y permisos da acceso."""
    if not raw_key or not raw_key.strip():
        raise ApiKeyAuthenticationError(
            "Falta la llave de la API: envíala en la cabecera X-API-Key", code="API_KEY_REQUIRED"
        )
    raw = raw_key.strip()
    digest = hash_token(raw)
    # El límite va ANTES de cualquier consulta: la petición nunca ocupa dos conexiones a la vez y
    # probar llaves inventadas también cuenta.
    enforce(f"api-key:{digest[:32]}", settings.RATE_LIMIT_API_KEY_PER_MINUTE)
    key = find_by_hash(db, digest) if raw.startswith(API_KEY_PREFIX) and len(raw) <= _MAX_KEY_LENGTH else None
    if key is None:
        raise ApiKeyAuthenticationError("La llave de la API no es válida", code="API_KEY_INVALID")
    status = key_status(key)
    if status == ApiKeyStatus.REVOKED:
        raise ApiKeyAuthenticationError("Esta llave de la API fue revocada", code="API_KEY_REVOKED")
    if status == ApiKeyStatus.EXPIRED:
        raise ApiKeyAuthenticationError("Esta llave de la API venció", code="API_KEY_EXPIRED")
    company = db.get(Company, key.company_id)
    if company is None or not company.active:
        raise PermissionDeniedError("La empresa de esta llave está desactivada", code="COMPANY_INACTIVE")
    if not company.api_enabled:  # el ADMIN le quitó el módulo: la llave se conserva pero no sirve
        raise PermissionDeniedError("La empresa de esta llave no tiene acceso a la API", code="API_ACCESS_DISABLED")
    _touch(db, key, ip)
    return ApiClient(
        key_id=key.id,
        company_id=key.company_id,
        name=key.name,
        prefix=key.prefix,
        scopes=frozenset(key.scope_codes),
        expires_at=key.expires_at,
    )


def _touch(db: Session, key: CompanyApiKey, ip: str | None) -> None:
    """Último uso (fecha e IP), a lo más cada API_KEY_TOUCH_SECONDS: sin una escritura por petición
    (aunque el sistema que la usa salga a Internet por varias IP)."""
    now = datetime.now(UTC)
    window = timedelta(seconds=settings.API_KEY_TOUCH_SECONDS)
    if key.last_used_at is not None and as_utc(key.last_used_at) + window > now:
        return
    key.last_used_at = now
    key.last_used_ip = ip[:45] if ip else None
    db.commit()


def require_scope(client: ApiClient, scope: str) -> None:
    if scope not in client.scopes:
        name = get_catalogs().name("api_scopes", scope)
        raise PermissionDeniedError(
            f"Esta llave no tiene el permiso «{name}». Pídelo a tu empresa (Integraciones).",
            code="API_SCOPE_REQUIRED",
            details={"scope": scope},
        )


class ApiKeyService:
    """La empresa administra sus llaves: crear, rotar y revocar (solo las suyas)."""

    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id
        self.keys = ApiKeyRepository(db, company_id)

    def list_keys(self, page: PageParams) -> ApiKeyList:
        items, total = self.keys.page(offset=page.offset, limit=page.size)
        return ApiKeyList.of(self._read_many(items), total, page)

    def create(self, data: ApiKeyCreate, actor: User) -> ApiKeyCreated:
        scopes = self._valid_scopes(data.scopes)
        if self.keys.count_usable() >= settings.API_KEYS_MAX_ACTIVE:
            raise ConflictError(
                f"Tu empresa ya tiene {settings.API_KEYS_MAX_ACTIVE} llaves sin revocar. Revoca las que no uses.",
                code="API_KEY_LIMIT",
            )
        expires_at = datetime.now(UTC) + timedelta(days=data.expires_in_days) if data.expires_in_days else None
        key, secret = self._issue(data.name, scopes, expires_at, actor)
        self.db.commit()
        return self._created(key, secret)

    def rotate(self, key_id: int, actor: User) -> ApiKeyCreated:
        """Llave nueva con el mismo nombre, permisos y vigencia; la anterior se revoca al instante."""
        old = self._get(key_id)
        if old.revoked_at is not None:
            raise ConflictError("Una llave revocada no se puede rotar: crea una nueva", code="API_KEY_REVOKED")
        expires_at = None
        if old.expires_at is not None:  # misma vigencia en días completos (como se creó)
            days = round((as_utc(old.expires_at) - as_utc(old.created_at)).total_seconds() / 86_400)
            expires_at = datetime.now(UTC) + timedelta(days=max(days, 1))
        key, secret = self._issue(old.name, old.scope_codes, expires_at, actor)
        self._revoke(old, actor)
        self.db.commit()
        return self._created(key, secret)

    def revoke(self, key_id: int, actor: User) -> ApiKeyRead:
        key = self._get(key_id)
        if key.revoked_at is None:
            self._revoke(key, actor)
            self.db.commit()
        return self._read_many([key])[0]

    # ---------- Internos ----------

    def _get(self, key_id: int) -> CompanyApiKey:
        key = self.keys.get(key_id)
        if key is None:
            raise NotFoundError("Llave no encontrada", code="API_KEY_NOT_FOUND")
        return key

    @staticmethod
    def _revoke(key: CompanyApiKey, actor: User) -> None:
        key.revoked_at = datetime.now(UTC)
        key.revoked_by_id = actor.id

    def _issue(
        self, name: str, scopes: list[str], expires_at: datetime | None, actor: User
    ) -> tuple[CompanyApiKey, str]:
        secret = API_KEY_PREFIX + secrets.token_urlsafe(SECRET_BYTES)
        key = CompanyApiKey(
            company_id=self.company_id,
            name=name,
            prefix=secret[:PREFIX_LENGTH],
            key_hash=hash_token(secret),
            created_by_id=actor.id,
            expires_at=expires_at,
            scopes=[CompanyApiKeyScope(scope=scope) for scope in scopes],
        )
        return self.keys.add(key), secret

    @staticmethod
    def _valid_scopes(requested: list[str]) -> list[str]:
        catalogs = get_catalogs()
        scopes = sorted(set(requested))
        unknown = [s for s in scopes if not catalogs.is_active("api_scopes", s)]
        if unknown:
            raise UnprocessableError(
                f"Permisos no válidos: {', '.join(unknown)}", code="API_SCOPE_INVALID", field="scopes"
            )
        return scopes

    def _created(self, key: CompanyApiKey, secret: str) -> ApiKeyCreated:
        self.db.refresh(key)
        return ApiKeyCreated(**self._read_many([key])[0].model_dump(), secret=secret)

    def _read_many(self, keys: list[CompanyApiKey]) -> list[ApiKeyRead]:
        user_ids = {i for k in keys for i in (k.created_by_id, k.revoked_by_id) if i is not None}
        emails = UserRepository(self.db).emails_by_ids(user_ids)
        now = datetime.now(UTC)
        return [
            ApiKeyRead(
                id=k.id,
                name=k.name,
                prefix=k.prefix,
                scopes=k.scope_codes,
                status=key_status(k, now),
                created_at=as_utc(k.created_at),
                created_by=emails.get(k.created_by_id) if k.created_by_id else None,
                expires_at=as_utc(k.expires_at),
                last_used_at=as_utc(k.last_used_at),
                last_used_ip=k.last_used_ip,
                revoked_at=as_utc(k.revoked_at),
                revoked_by=emails.get(k.revoked_by_id) if k.revoked_by_id else None,
            )
            for k in keys
        ]
