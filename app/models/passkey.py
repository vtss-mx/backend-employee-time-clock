"""Llaves de acceso (WebAuthn / passkeys; antifraude fase 3, decisión del dueño del producto del 2026-10-06).

Un inicio de sesión FUERTE contra contraseñas compartidas o robadas (I+D §2.6, P6): la persona registra una llave en su
dispositivo (Face ID, huella o PIN) y el servidor solo guarda la llave PÚBLICA. La privada nunca sale del dispositivo
(o de la nube de Apple/Google si es una llave sincronizada): aquí no hay nada que robar.

- `Passkey` es de la PERSONA (`auth.users`), no de un empleo: como la foto de perfil, vive en `auth` sin `company_id`
  (los ADMIN y los empleados no tienen empresa en su cuenta; un COMPANY o VALIDATOR sí, pero su llave sigue siendo de su
  cuenta). No es una tabla de empresa ni lleva seguridad por fila. Revocar es un borrado REAL, como una sesión (regla
  20: revocar no es eliminar).
- `sign_count`: el contador que el autenticador incrementa en cada firma. Si llega uno menor o igual al guardado
  (cuando alguno es mayor que cero), la llave privada se copió: se revoca y se avisa al ADMIN. Las llaves
  sincronizadas suelen mandar siempre 0 (sin detección, por diseño de Apple y Google).
- `PasskeyChallenge`: la huella (SHA-256) de cada reto SELLADO (`app/core/sealed.py`) ya aceptado. El reto no vive en
  memoria ni en la base mientras viaja (N réplicas); aquí se anota al usarse, en la misma transacción que la llave
  nueva o la sesión, con una inserción atómica: un reto vale una sola vez. Las vencidas las depura el mantenimiento.
"""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, String, false, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.db_schemas import AUTH

if TYPE_CHECKING:
    from app.models.user import User


class Passkey(Base):
    """Una llave de acceso registrada por una persona: su llave pública y el nombre con que la reconoce."""

    __tablename__ = "passkeys"
    __table_args__ = (
        # Entrar con la llave: el dispositivo manda el id de la credencial y se busca por él (único en la plataforma).
        Index("uq_passkeys_credential", "credential_id", unique=True),
        # Las llaves de una cuenta (lista de Mi perfil, tope por cuenta, las que se excluyen al registrar otra) y la
        # FK a la cuenta (CASCADE: depurar una cuenta no recorre la tabla).
        Index("ix_passkeys_user", "user_id", "id"),
        {"schema": AUTH},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey(f"{AUTH}.users.id", ondelete="CASCADE"), nullable=False)
    #: Id de la credencial (base64url; hasta 1023 bytes según WebAuthn) y su llave pública COSE (base64url).
    credential_id: Mapped[str] = mapped_column(String(1400), nullable=False)
    public_key: Mapped[str] = mapped_column(String(2048), nullable=False)
    #: Contador de firmas del autenticador (0 en las llaves sincronizadas).
    sign_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    #: Cómo se conecta el autenticador (`internal`, `hybrid`, `usb`...), separados por comas; informativo.
    transports: Mapped[str | None] = mapped_column(String(100))
    #: Nombre que le dio la persona («Mi teléfono»).
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    #: Modelo del autenticador según WebAuthn (ceros en las llaves sincronizadas, que no dan attestation).
    aaguid: Mapped[str | None] = mapped_column(String(36))
    #: La llave puede sincronizarse en la nube de la plataforma y si de hecho está respaldada.
    backup_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    backed_up: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: La cuenta (se carga con sus empleos al entrar: las mismas reglas que la contraseña).
    user: Mapped[User] = relationship(lazy="select", innerjoin=True)


class PasskeyChallenge(Base):
    """Un reto sellado que ya se aceptó (su SHA-256): no vale una segunda vez."""

    __tablename__ = "passkey_challenges"
    __table_args__ = ({"schema": AUTH},)

    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: Hasta cuándo valdría el reto: después, la fila sobra (depuración del mantenimiento).
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
