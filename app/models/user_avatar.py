"""Foto de perfil de una persona: la REFERENCIA de cada tamaño de su imagen, cifrada en el bucket.

Decisiones (README, "Foto de perfil"):

- La foto es de la PERSONA (`auth.users`), no de un empleo: una persona trabaja en varias empresas con la
  misma cuenta y todas ven la misma foto (si pueden verla: `AvatarService.read`). Por eso vive en `auth`, sin
  `company_id` (no es una tabla de empresa ni lleva seguridad por fila), y su objeto se nombra solo con ids:
  `<prefijo>/people/users/<cuenta>/avatar/<versión>/<tamaño>.webp.enc`.
- Una fila por tamaño (512 y 96 px): cada una es la referencia de UN objeto del registro `STORED_IMAGES`
  (`USER_AVATARS` en `app/services/image_storage.py`). Los bytes nunca están en la BD.
- `version` cambia en cada foto nueva (va en la URL: la caché del navegador nunca sirve una foto anterior) y
  `users.avatar_version` repite la vigente para que `/users/me`, el inicio de sesión y los listados de
  empleados armen la URL sin otra consulta (ya cargan la cuenta). Ambas cambian en la misma transacción.
- La llave primaria `(user_id, size_px)` es el índice de las dos únicas consultas (la foto de una persona en
  un tamaño y sus tamaños al reemplazarla o quitarla) y de la FK `ON DELETE CASCADE` (§3.1.8): ningún índice
  más. Antes de borrar una cuenta sus objetos se encolan para salir del bucket (`release_user_avatars`).
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.db_schemas import AUTH


class UserAvatar(Base):
    """Un tamaño de la foto de perfil vigente de una persona (la referencia de su objeto cifrado)."""

    __tablename__ = "user_avatars"
    __table_args__ = (
        # Los tamaños del contrato de la API (`?size=96|512`); otro tamaño exige una migración que los genere.
        CheckConstraint("size_px IN (96, 512)", name="size_px"),
        {"schema": AUTH},
    )

    user_id: Mapped[int] = mapped_column(ForeignKey(f"{AUTH}.users.id", ondelete="CASCADE"), primary_key=True)
    #: Lado del cuadrado en píxeles.
    size_px: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    #: Versión de la foto (aleatoria, segura en una URL): la misma en todos sus tamaños.
    version: Mapped[str] = mapped_column(String(24), nullable=False)
    #: Tipo del contenido descifrado (siempre `image/webp`: se vuelve a codificar al recibirla).
    content_type: Mapped[str] = mapped_column(String(30), nullable=False)
    #: La referencia (`StoredImage`): nombre del objeto, tamaño del contenido, SHA-256 del objeto cifrado y
    #: cuándo se subió (verificado).
    object_name: Mapped[str] = mapped_column(String(300), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
