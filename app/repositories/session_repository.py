from datetime import datetime

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session, joinedload, lazyload, selectinload

from app.models import AuthSession, Employee, SessionRevocationReason, User
from app.repositories.aggregates import affected_rows, paginate

#: Lo que necesita validar una sesión en CADA petición, en 2 consultas: la sesión con su usuario y
#: la empresa de este (una), y los empleos con su empresa (otra). Sin estas opciones las relaciones
#: de los modelos se cargaban en ciclo (usuario → empleos → usuario...) con 3 o 4 consultas; la
#: configuración del validador (`User.validator`) se carga solo si se usa y la referencia del empleo a
#: su usuario sale del mapa de identidad (sin consulta).
USER_AUTH_OPTIONS = (
    joinedload(User.company),
    selectinload(User.employees).options(joinedload(Employee.company), lazyload(Employee.user)),
)
_AUTH_LOAD = (joinedload(AuthSession.user).options(*USER_AUTH_OPTIONS),)


class SessionRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, session_id: str, *, for_update: bool = False) -> AuthSession | None:
        # Se bloquea solo la fila de la sesión (no la del usuario unido por el eager load).
        lock = {"of": AuthSession} if for_update else None
        return self.db.get(AuthSession, session_id, with_for_update=lock, options=_AUTH_LOAD)

    def add(self, session: AuthSession) -> None:
        self.db.add(session)

    def bind_device(self, session_id: str, key_hash: str) -> bool:
        """Liga la sesión a la llave de su dispositivo si aún no tenía ninguna (o ya era esa): UNA sentencia atómica
        por su llave primaria, así dos identificaciones simultáneas nunca la ligan a llaves distintas. False si ya
        estaba ligada a otra (antifraude 2b, `request_signing`)."""
        stmt = (
            update(AuthSession)
            .where(
                AuthSession.id == session_id,
                or_(AuthSession.device_key_hash.is_(None), AuthSession.device_key_hash == key_hash),
            )
            .values(device_key_hash=key_hash)
        )
        return affected_rows(self.db, stmt) == 1

    def active_for_user(self, user_id: int, now: datetime) -> list[AuthSession]:
        return list(
            self.db.scalars(
                select(AuthSession)
                .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None), AuthSession.expires_at > now)
                .order_by(AuthSession.created_at.desc())
            )
        )

    def page_active(self, user_id: int, now: datetime, *, offset: int, limit: int) -> tuple[list[AuthSession], int]:
        """Sesiones vigentes del usuario, la más reciente primero."""
        stmt = select(AuthSession).where(
            AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None), AuthSession.expires_at > now
        )
        return paginate(self.db, stmt, (AuthSession.created_at.desc(), AuthSession.id), offset=offset, limit=limit)

    def revoke_all(
        self,
        user_id: int,
        now: datetime,
        reason: SessionRevocationReason,
        *,
        except_id: str | None = None,
        company_id: int | None = None,
    ) -> int:
        """Sesiones activas del usuario (todas, o solo las que entraron a `company_id`)."""
        stmt = update(AuthSession).where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
        if except_id:
            stmt = stmt.where(AuthSession.id != except_id)
        if company_id is not None:
            stmt = stmt.where(AuthSession.company_id == company_id)
        return affected_rows(self.db, stmt.values(revoked_at=now, revoked_reason=reason), synchronize="fetch")

    def revoke_company(self, company_id: int, now: datetime, reason: SessionRevocationReason) -> None:
        """Las sesiones de una empresa en un solo UPDATE: las de sus cuentas (administradores y
        validadores) y las de los empleados que entraron a ELLA (un empleado que también trabaja en otra
        empresa conserva su sesión de esa otra). Sin sincronizar la sesión ORM: pueden ser miles de filas.

        Las cuentas de la empresa (pocas) se leen antes: con `user_id IN (subconsulta)` dentro del OR,
        PostgreSQL recorría TODA la tabla de sesiones; con la lista, cada rama usa su índice (BitmapOr)."""
        accounts = list(self.db.scalars(select(User.id).where(User.company_id == company_id)))
        stmt = (
            update(AuthSession)
            .where(
                AuthSession.revoked_at.is_(None),
                or_(AuthSession.company_id == company_id, AuthSession.user_id.in_(accounts)),
            )
            .values(revoked_at=now, revoked_reason=reason)
        )
        self.db.execute(stmt.execution_options(synchronize_session=False))
