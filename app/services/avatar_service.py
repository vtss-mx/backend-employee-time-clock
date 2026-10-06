"""Foto de perfil de cada persona: subirla, quitarla y leerla con los permisos de quien pregunta.

Decisiones del dueño del producto (README, "Foto de perfil"):

- **Es de la persona** (`auth.users`): todos los roles con "Mi perfil" cambian o quitan la SUYA; la misma cuenta
  en varias empresas muestra la misma foto.
- **Nunca en la BD**: cada tamaño (512 y 96 px, `avatar_image.render`) va cifrado al bucket por `STORED_IMAGES`
  (`USER_AVATARS`); la BD guarda la referencia (`auth.user_avatars`) y la versión vigente (`users.avatar_version`).
  Sin bucket: 503 `STORAGE_UNAVAILABLE` y nada cambia (el perfil sigue funcionando sin foto).
- **Transacción corta**: primero se procesa la imagen (CPU) y se sube cada tamaño (red) SIN transacción abierta;
  después, en una sola y breve, se bloquea la cuenta, se encolan los objetos de la foto anterior para salir del
  bucket, se reemplazan las filas y se confirma. Si algo falla antes, lo subido se borra (`image_storage.abandon`);
  si falla la transacción, `image_storage` lo descarta al revertirse.
- **Quién ve a quién** (`read`): la propia; la empresa (administrador o validador) solo a sus empleados activos y
  a sus cuentas activas; la plataforma (ADMIN) solo cuentas que no son de empleados (regla 13: el ADMIN nunca ve
  fotos de los empleados). Cualquier otro caso es 404, igual que una foto que no existe: nada dice que una
  persona de otra empresa tiene foto.
"""

import base64
import secrets
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.core.object_storage import StorageError
from app.models import User, UserAvatar, UserRole
from app.repositories.avatar_repository import AvatarRepository
from app.schemas.avatar import AvatarImage, AvatarRead, avatar_path
from app.services import image_storage
from app.services.avatar_image import CONTENT_TYPE, CropBox, render
from app.services.image_storage import USER_AVATARS

#: Bytes aleatorios de cada versión (12 caracteres seguros en una URL).
_VERSION_BYTES = 9


@dataclass(frozen=True)
class AvatarDownload:
    """Lo que responde la lectura: la imagen o, si el navegador ya la tiene (`If-None-Match`), solo su `etag`."""

    etag: str
    version: str
    image: AvatarImage | None


def etag_of(version: str, size_px: int) -> str:
    """ETag fuerte de un tamaño de una versión (inmutable: una foto nueva es otra versión)."""
    return f'"{version}-{size_px}"'


def matches(if_none_match: str | None, etag: str) -> bool:
    """`If-None-Match` (una lista, débil o `*`) incluye la versión que se tiene."""
    if not if_none_match:
        return False
    tags = {tag.strip().removeprefix("W/") for tag in if_none_match.split(",")}
    return etag in tags or "*" in tags


class AvatarService:
    """Foto de perfil de las personas (tablas de la plataforma: sin `company_id` ni seguridad por fila)."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.avatars = AvatarRepository(db)

    def replace(self, user: User, data: bytes, crop: CropBox | None) -> AvatarRead:
        """Sube (o reemplaza) la foto de la persona: 422 si no es una imagen válida o el recorte no cabe, 503 si el
        bucket no responde (nada cambia)."""
        images = render(data, crop)  # CPU, sin BD ni red
        version = secrets.token_urlsafe(_VERSION_BYTES)
        rows = [
            UserAvatar(user_id=user.id, size_px=side, version=version, content_type=CONTENT_TYPE) for side in images
        ]
        try:  # red, sin transacción abierta: ninguna conexión de la BD espera al bucket
            for row in rows:
                image_storage.store(self.db, USER_AVATARS, row, images[row.size_px])
        except Exception:
            image_storage.abandon(self.db)
            raise
        self._clear(user)
        self.db.add_all(rows)
        user.avatar_version = version
        self.db.commit()
        return AvatarRead(avatar=avatar_path(user.id, version), version=version)

    def remove(self, user: User) -> AvatarRead:
        """Quita la foto (idempotente): sus objetos salen del bucket por la cola de borrado."""
        self._clear(user)
        user.avatar_version = None
        self.db.commit()
        return AvatarRead()

    def _clear(self, user: User) -> None:
        """Con la cuenta bloqueada: la foto vigente se encola para salir del bucket y se borra su referencia."""
        self.avatars.lock_person(user.id)
        image_storage.release_user_avatars(self.db, UserAvatar.user_id == user.id)
        self.avatars.remove(user.id)

    def read(self, viewer: User, user_id: int, size_px: int, if_none_match: str | None = None) -> AvatarDownload:
        """Un tamaño de la foto de `user_id` si `viewer` puede verla (si no, 404). Si el navegador ya tiene esa
        versión no se lee el bucket. 503 `STORAGE_UNAVAILABLE` si el bucket no responde."""
        row = self._visible(viewer, user_id, size_px)
        # Fin de la transacción de lectura ANTES de esperar al bucket: la conexión vuelve al pool (los atributos
        # leídos siguen disponibles: expire_on_commit=False).
        self.db.commit()
        if row is None:
            raise NotFoundError(code="AVATAR_NOT_FOUND")
        etag = etag_of(row.version, size_px)
        if matches(if_none_match, etag):
            return AvatarDownload(etag=etag, version=row.version, image=None)
        try:  # del bucket, verificado contra su SHA-256 y descifrado en memoria
            data = image_storage.read(USER_AVATARS, row)
        except StorageError as exc:
            raise image_storage.unavailable(exc) from exc
        image = AvatarImage(
            user_id=user_id,
            size_px=size_px,
            version=row.version,
            content_type=row.content_type,
            # `read` solo da None a una fila sin objeto; las de la foto siempre lo tienen (NOT NULL).
            data=base64.b64encode(data or b"").decode(),
        )
        return AvatarDownload(etag=etag, version=row.version, image=image)

    def _visible(self, viewer: User, user_id: int, size_px: int) -> UserAvatar | None:
        """La regla de quién ve a quién (ver el docstring del módulo), en una consulta."""
        if viewer.id == user_id:
            return self.avatars.visible(user_id, size_px)
        if viewer.role == UserRole.ADMIN:
            return self.avatars.visible(user_id, size_px, staff_only=True)
        company = viewer.current_company
        if viewer.role in (UserRole.COMPANY, UserRole.VALIDATOR) and company is not None:
            return self.avatars.visible(user_id, size_px, company_id=company.id)
        return None  # un empleado solo ve la suya
