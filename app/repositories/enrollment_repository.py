from datetime import UTC, datetime

from sqlalchemy import and_, select, update
from sqlalchemy.orm import Session

from app.models import EnrollmentStatus, FaceEnrollment
from app.repositories.aggregates import affected_rows, get_scoped, paginate
from app.repositories.storage_repository import StorageRepository


class FaceEnrollmentRepository:
    """Registros faciales de UNA empresa (company_id en cada consulta)."""

    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id

    def get(self, enrollment_id: int, *, for_update: bool = False) -> FaceEnrollment | None:
        """`for_update`: bloquea la fila hasta el commit (aprobar y rechazar a la vez se serializan:
        la segunda petición ve el registro ya revisado)."""
        # FOR UPDATE OF solo esta tabla: el empleado se carga con un JOIN externo que no se bloquea.
        return get_scoped(self.db, FaceEnrollment, enrollment_id, self.company_id, lock=for_update)

    def add(self, enrollment: FaceEnrollment) -> FaceEnrollment:
        enrollment.company_id = self.company_id
        self.db.add(enrollment)
        self.db.flush()
        return enrollment

    def latest_for_employee(self, employee_id: int) -> FaceEnrollment | None:
        return self.db.scalar(
            select(FaceEnrollment)
            .where(FaceEnrollment.company_id == self.company_id, FaceEnrollment.employee_id == employee_id)
            .order_by(FaceEnrollment.submitted_at.desc(), FaceEnrollment.id.desc())
            .limit(1)
        )

    def latest_id_for_employee(self, employee_id: int) -> int | None:
        """Solo el id del registro más reciente (sin cargar el registro ni su foto)."""
        return self.db.scalar(
            select(FaceEnrollment.id)
            .where(FaceEnrollment.company_id == self.company_id, FaceEnrollment.employee_id == employee_id)
            .order_by(FaceEnrollment.submitted_at.desc(), FaceEnrollment.id.desc())
            .limit(1)
        )

    def search(self, *, status: EnrollmentStatus | None, offset: int, limit: int) -> tuple[list[FaceEnrollment], int]:
        stmt = select(FaceEnrollment).where(FaceEnrollment.company_id == self.company_id)
        if status is not None:
            stmt = stmt.where(FaceEnrollment.status == status)
        # Pendientes: el más antiguo primero (cola de revisión); resto: el más reciente. El desempate
        # por id va en la misma dirección para que el índice (status, submitted_at, id) entregue el orden.
        if status == EnrollmentStatus.PENDING:
            order = (FaceEnrollment.submitted_at.asc(), FaceEnrollment.id.asc())
        else:
            order = (FaceEnrollment.submitted_at.desc(), FaceEnrollment.id.desc())
        return paginate(self.db, stmt, order, offset=offset, limit=limit)

    def reject_pending(self, reason: str, employee_id: int | None = None) -> int:
        """Rechaza los registros en validación de la empresa (o de un empleado) y borra su fotografía
        (minimización de datos: su objeto pasa a la cola de borrado del bucket y la fila queda sin
        referencia), en unas cuantas sentencias aunque sean miles."""
        conditions = [FaceEnrollment.company_id == self.company_id, FaceEnrollment.status == EnrollmentStatus.PENDING]
        if employee_id is not None:
            conditions.append(FaceEnrollment.employee_id == employee_id)
        StorageRepository(self.db).enqueue_from(FaceEnrollment.photo_object, and_(*conditions), datetime.now(UTC))
        values = {
            "status": EnrollmentStatus.REJECTED,
            "rejection_reason": reason,
            "photo_object": None,
            "photo_size": None,
            "photo_sha256": None,
            "photo_uploaded_at": None,
        }
        return affected_rows(self.db, update(FaceEnrollment).where(*conditions).values(**values))
