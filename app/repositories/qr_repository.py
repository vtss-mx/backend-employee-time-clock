from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.models import EmployeeQr


class EmployeeQrRepository:
    """Consultas de los QR dinámicos. Marcar un QR como usado es UNA sentencia condicionada
    (`... WHERE used_at IS NULL RETURNING id`): con dos lecturas simultáneas, solo una gana."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def get_by_token_hash(self, token_hash: str) -> EmployeeQr | None:
        return self.db.scalar(select(EmployeeQr).where(EmployeeQr.token_hash == token_hash))

    def get_for_employee(self, employee_id: int, qr_id: int) -> EmployeeQr | None:
        return self.db.scalar(select(EmployeeQr).where(EmployeeQr.id == qr_id, EmployeeQr.employee_id == employee_id))

    def latest_issued(self, employee_id: int) -> EmployeeQr | None:
        return self.db.scalar(
            select(EmployeeQr)
            .where(EmployeeQr.employee_id == employee_id, EmployeeQr.token_encrypted.is_(None))
            .order_by(EmployeeQr.id.desc())
            .limit(1)
        )

    def latest_used(self, employee_id: int) -> EmployeeQr | None:
        return self.db.scalar(
            select(EmployeeQr)
            .where(EmployeeQr.employee_id == employee_id, EmployeeQr.used_at.is_not(None))
            .order_by(EmployeeQr.used_at.desc(), EmployeeQr.id.desc())
            .limit(1)
        )

    def add(self, qr: EmployeeQr) -> EmployeeQr:
        self.db.add(qr)
        self.db.flush()
        return qr

    def revoke_all_for_employee(self, employee_id: int) -> None:
        """Apaga el QR vigente del empleado (uno a la vez: al emitir otro o al invalidarlo)."""
        self.db.execute(
            update(EmployeeQr)
            .where(EmployeeQr.employee_id == employee_id, EmployeeQr.active.is_(True))
            .values(active=False, revoked_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )

    def mark_used(self, qr_id: int, actor_id: int, *, complete: bool) -> bool:
        """Lo marca usado si nadie lo usó antes (atómico). `complete=False` lo aparta para el paso
        del rostro (QR + rostro)."""
        now = datetime.now(UTC)
        used = self.db.scalar(
            update(EmployeeQr)
            .where(EmployeeQr.id == qr_id, EmployeeQr.used_at.is_(None), EmployeeQr.active.is_(True))
            .values(active=False, used_at=now, used_by_id=actor_id, completed_at=now if complete else None)
            .returning(EmployeeQr.id)
            .execution_options(synchronize_session=False)
        )
        return used is not None

    def mark_completed(self, qr_id: int, actor_id: int) -> bool:
        """Cierra un QR apartado por ESTE validador (una sola vez, atómico)."""
        done = self.db.scalar(
            update(EmployeeQr)
            .where(EmployeeQr.id == qr_id, EmployeeQr.used_by_id == actor_id, EmployeeQr.completed_at.is_(None))
            .values(completed_at=datetime.now(UTC))
            .returning(EmployeeQr.id)
            .execution_options(synchronize_session=False)
        )
        return done is not None

    def purge_expired(self, before: datetime) -> None:
        """Depura los QR vencidos hace tiempo (uno depurado tampoco se acepta: ya no existe)."""
        self.db.execute(
            delete(EmployeeQr)
            .where(EmployeeQr.expires_at < before, EmployeeQr.active.is_(False))
            .execution_options(synchronize_session=False)
        )
