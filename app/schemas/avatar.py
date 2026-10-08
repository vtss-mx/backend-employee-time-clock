"""Foto de perfil: contrato de la API (subir, quitar y leer) y la ruta versionada que viaja con cada cuenta."""

from typing import TYPE_CHECKING, Final

from pydantic import BaseModel

if TYPE_CHECKING:
    from app.models import Employee

#: Tamaños que se guardan de cada foto (lado del cuadrado en px): 512 para el perfil y vistas grandes, 96 para
#: menús y listados. Son parte del contrato (`?size=96|512`) y del nombre del objeto en el bucket: cambiarlos
#: exige volver a generar las fotos existentes (no es un ajuste del `.env`).
AVATAR_SIZES: Final = (512, 96)


def avatar_path(user_id: int, version: str | None) -> str | None:
    """Ruta de la foto de una persona dentro de la API (como las demás, sin el prefijo `/api`), con su versión:
    una foto nueva cambia la URL y la caché del navegador nunca sirve la anterior. None si no tiene foto."""
    return None if version is None else f"/users/{user_id}/avatar?v={version}"


def person_avatar(user_id: int, version: str | None, *, deleted: bool = False) -> str | None:
    """La foto de una persona en una referencia de una respuesta (empleado, administrador, validador...): su ruta
    versionada o None si no tiene o si está en «Eliminados» (su foto se borró de verdad al eliminarla, regla 13; un
    empleo eliminado de alguien que trabaja en otra empresa tampoco la muestra aquí). La versión viaja con la cuenta
    que la consulta ya cargó: ninguna consulta de más. Quién puede verla la decide `AvatarRepository.visible`
    (decisión del dueño, 2026-10-06: la empresa ve a su gente; el ADMIN, a todos; el empleado, la suya)."""
    return None if deleted else avatar_path(user_id, version)


def employee_avatar(employee: Employee) -> str | None:
    """`person_avatar` de un empleado cargado con su cuenta (la carga normal del modelo la trae con un JOIN)."""
    return person_avatar(employee.user_id, employee.user.avatar_version, deleted=employee.deleted)


class AvatarRead(BaseModel):
    """La foto vigente de la persona tras subirla o quitarla."""

    #: Ruta versionada (`/users/{id}/avatar?v=...`; se le agrega `&size=96|512`) o None sin foto.
    avatar: str | None = None
    version: str | None = None


class AvatarImage(BaseModel):
    """Un tamaño de la foto de una persona, descifrado. Cabeceras: `ETag` (versión y tamaño) y `Cache-Control`
    privado (la URL lleva la versión); con `If-None-Match` igual responde 304 sin cuerpo."""

    user_id: int
    #: Lado del cuadrado en px (96 o 512).
    size_px: int
    version: str
    content_type: str
    #: La imagen en base64 (el contrato único de respuesta es JSON, igual que el comprobante de un pago).
    data: str
