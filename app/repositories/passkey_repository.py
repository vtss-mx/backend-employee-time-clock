"""Consultas de las llaves de acceso (WebAuthn / passkeys) y de los retos sellados ya usados (sin reglas de negocio ni
`commit`). Tablas de la persona (`auth`): sin empresa ni seguridad por fila."""

from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, joinedload

from app.models import Passkey, PasskeyChallenge
from app.repositories.aggregates import affected_rows, dialect_insert, paginate
from app.repositories.session_repository import USER_AUTH_OPTIONS


class PasskeyRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def add(self, passkey: Passkey) -> None:
        self.db.add(passkey)

    def delete(self, passkey: Passkey) -> None:
        """Revocar es un borrado real (como una sesión): la llave deja de servir al instante."""
        self.db.delete(passkey)

    def for_user(self, user_id: int, limit: int) -> list[Passkey]:
        """Las llaves de una cuenta (acotadas por el tope por cuenta): las que se excluyen al registrar otra y el conteo
        del tope, por el índice `(user_id, id)`."""
        stmt = select(Passkey).where(Passkey.user_id == user_id).order_by(Passkey.id).limit(limit)
        return list(self.db.scalars(stmt))

    def page(self, user_id: int, *, offset: int, limit: int) -> tuple[list[Passkey], int]:
        """Mi perfil: las llaves de la cuenta, la más antigua primero (índice `(user_id, id)`)."""
        stmt = select(Passkey).where(Passkey.user_id == user_id)
        return paginate(self.db, stmt, (Passkey.id,), offset=offset, limit=limit)

    def get(self, passkey_id: int, user_id: int) -> Passkey | None:
        """Una llave por su id, solo si es de la cuenta (si no, None: la ruta responde 404)."""
        found = self.db.get(Passkey, passkey_id)
        return found if found is not None and found.user_id == user_id else None

    def by_credential(self, credential_id: str) -> Passkey | None:
        """Entrar: la llave por el id de su credencial (único) con su cuenta, su empresa y sus empleos cargados como la
        autenticación de una sesión (las mismas reglas que la contraseña, sin consultas de más)."""
        stmt = (
            select(Passkey)
            .where(Passkey.credential_id == credential_id)
            .options(joinedload(Passkey.user).options(*USER_AUTH_OPTIONS))
        )
        return self.db.scalars(stmt).unique().one_or_none()

    def touch(self, passkey_id: int, sign_count: int, used_at: datetime) -> None:
        """El contador de firmas y el último uso tras entrar: UNA sentencia por la llave primaria."""
        stmt = update(Passkey).where(Passkey.id == passkey_id).values(sign_count=sign_count, last_used_at=used_at)
        affected_rows(self.db, stmt)

    def claim_challenge(self, digest: str, expires_at: datetime) -> bool:
        """Anota un reto sellado como usado; False si ya se había usado (inserción atómica: dos peticiones con el mismo
        reto nunca pasan las dos, también entre réplicas). Se decide por RETURNING, como la huella anti-reenvío
        (`CaptureFingerprintRepository.claim`): en PostgreSQL el `rowcount` de un INSERT del ORM con ON CONFLICT no
        es confiable (devolvía 0 aunque insertara, medido en `quality.sh --postgres`); la fila devuelta sí lo es."""
        stmt = (
            dialect_insert(self.db, PasskeyChallenge)
            .values(digest=digest, expires_at=expires_at)
            .on_conflict_do_nothing(index_elements=["digest"])
            .returning(PasskeyChallenge.digest)
        )
        return self.db.execute(stmt).first() is not None

    def challenges_count(self) -> int:
        """Cuántos retos usados hay (pruebas y estado): la tabla es pequeña (vencen en minutos)."""
        return int(self.db.scalar(select(func.count()).select_from(PasskeyChallenge)) or 0)
