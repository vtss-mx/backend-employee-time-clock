from collections.abc import Iterable

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from app.models import Employee, FaceEmbedding, FaceStatus


class FaceEmbeddingRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ---------- Galería de una empresa (identificación 1:N del validador) ----------

    @staticmethod
    def _gallery_filter(company_id: int, model_name: str) -> tuple[ColumnElement[bool], ...]:
        """Muestras activas del modelo actual de los empleados activos y con identidad aprobada."""
        return (
            Employee.company_id == company_id,
            Employee.active.is_(True),
            Employee.face_status == FaceStatus.APPROVED,
            FaceEmbedding.active.is_(True),
            FaceEmbedding.model_name == model_name,
        )

    def gallery_fingerprint(self, company_id: int, model_name: str) -> tuple[int, int, str]:
        """Huella barata (una consulta agregada) que cambia con cualquier alta, baja, aprobación,
        desactivación o reinicio de rostro: la caché de la galería se invalida sola."""
        row = self.db.execute(
            select(func.count(FaceEmbedding.id), func.max(FaceEmbedding.id), func.max(Employee.updated_at))
            .join(Employee, FaceEmbedding.employee_id == Employee.id)
            .where(*self._gallery_filter(company_id, model_name))
        ).one()
        return int(row[0] or 0), int(row[1] or 0), str(row[2] or "")

    def gallery_rows(self, company_id: int, model_name: str) -> list[tuple[int, bytes, int]]:
        """(empleado, embedding cifrado, dimensión) de toda la galería de la empresa."""
        rows = self.db.execute(
            select(FaceEmbedding.employee_id, FaceEmbedding.embedding_encrypted, FaceEmbedding.dimension)
            .join(Employee, FaceEmbedding.employee_id == Employee.id)
            .where(*self._gallery_filter(company_id, model_name))
            .order_by(FaceEmbedding.employee_id, FaceEmbedding.id)
        ).all()
        return [(int(employee_id), bytes(data), int(dimension)) for employee_id, data, dimension in rows]

    def approved_without_model(self, company_id: int, model_name: str, limit: int) -> list[int]:
        """Empleados aprobados sin muestras del modelo actual (p. ej. tras cambiar de motor)."""
        has_current = (
            select(FaceEmbedding.id)
            .where(
                FaceEmbedding.employee_id == Employee.id,
                FaceEmbedding.active.is_(True),
                FaceEmbedding.model_name == model_name,
            )
            .exists()
        )
        return list(
            self.db.scalars(
                select(Employee.id)
                .where(
                    Employee.company_id == company_id,
                    Employee.active.is_(True),
                    Employee.face_status == FaceStatus.APPROVED,
                    ~has_current,
                )
                .limit(limit)
            )
        )

    def list_active(self, employee_id: int, model_name: str | None = None) -> list[FaceEmbedding]:
        stmt = select(FaceEmbedding).where(FaceEmbedding.employee_id == employee_id, FaceEmbedding.active.is_(True))
        if model_name:
            stmt = stmt.where(FaceEmbedding.model_name == model_name)
        return list(self.db.scalars(stmt.order_by(FaceEmbedding.created_at)).all())

    def count_active(self, employee_id: int) -> int:
        return int(
            self.db.scalar(
                select(func.count()).where(FaceEmbedding.employee_id == employee_id, FaceEmbedding.active.is_(True))
            )
            or 0
        )

    def count_active_by_employee(self, employee_ids: Iterable[int]) -> dict[int, int]:
        ids = list(employee_ids)
        if not ids:
            return {}
        rows = self.db.execute(
            select(FaceEmbedding.employee_id, func.count())
            .where(FaceEmbedding.employee_id.in_(ids), FaceEmbedding.active.is_(True))
            .group_by(FaceEmbedding.employee_id)
        ).all()
        return {employee_id: int(count) for employee_id, count in rows}

    def add(self, embedding: FaceEmbedding) -> FaceEmbedding:
        self.db.add(embedding)
        self.db.flush()
        return embedding

    def delete_all(self, employee_id: int) -> None:
        """Borrado físico: los datos biométricos no se conservan al reemplazarse o eliminarse."""
        for item in self.db.scalars(select(FaceEmbedding).where(FaceEmbedding.employee_id == employee_id)):
            self.db.delete(item)
        self.db.flush()

    def delete_oldest_beyond(self, employee_id: int, keep: int) -> None:
        active = self.list_active(employee_id)
        for item in active[: max(0, len(active) - keep)]:
            self.db.delete(item)
        self.db.flush()

    def activate_enrollment(self, enrollment_id: int) -> None:
        for item in self.db.scalars(select(FaceEmbedding).where(FaceEmbedding.enrollment_id == enrollment_id)):
            item.active = True
        self.db.flush()

    def delete_enrollment(self, enrollment_id: int) -> None:
        for item in self.db.scalars(select(FaceEmbedding).where(FaceEmbedding.enrollment_id == enrollment_id)):
            self.db.delete(item)
        self.db.flush()
