from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import EnrollmentStatus, FaceEnrollment


class FaceEnrollmentRepository:
    """Registros faciales de UNA empresa (company_id en cada consulta)."""

    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id

    def get(self, enrollment_id: int) -> FaceEnrollment | None:
        enrollment = self.db.get(FaceEnrollment, enrollment_id)
        return enrollment if enrollment is not None and enrollment.company_id == self.company_id else None

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

    def search(self, *, status: EnrollmentStatus | None, offset: int, limit: int) -> tuple[list[FaceEnrollment], int]:
        stmt = select(FaceEnrollment).where(FaceEnrollment.company_id == self.company_id)
        if status is not None:
            stmt = stmt.where(FaceEnrollment.status == status)
        total = self.db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        # Pendientes: los más antiguos primero (cola de revisión); resto: los más recientes.
        # Pendientes: el más antiguo primero; resto: el más reciente. El desempate por id va en
        # la misma dirección para que el índice (status, submitted_at, id) entregue el orden.
        if status == EnrollmentStatus.PENDING:
            order = (FaceEnrollment.submitted_at.asc(), FaceEnrollment.id.asc())
        else:
            order = (FaceEnrollment.submitted_at.desc(), FaceEnrollment.id.desc())
        items = self.db.scalars(stmt.order_by(*order).offset(offset).limit(limit)).unique().all()
        return list(items), int(total)

    def pending_for_employee(self, employee_id: int) -> list[FaceEnrollment]:
        return list(
            self.db.scalars(
                select(FaceEnrollment).where(
                    FaceEnrollment.company_id == self.company_id,
                    FaceEnrollment.employee_id == employee_id,
                    FaceEnrollment.status == EnrollmentStatus.PENDING,
                )
            ).unique()
        )
