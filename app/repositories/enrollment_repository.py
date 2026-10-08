from datetime import UTC, datetime

from sqlalchemy import and_, delete, not_, or_, select, update
from sqlalchemy.orm import Session

from app.models import (
    Company,
    EnrollmentStatus,
    EnrollmentVoiceAnswer,
    FaceEnrollment,
    FaceEnrollmentFlag,
)
from app.repositories.aggregates import affected_rows, dialect_insert, get_scoped, paginate
from app.repositories.storage_repository import StorageRepository

#: Un registro TERMINADO: sin verificación por voz, o con ella ya superada. Lo que aún espera sus respuestas en video no
#: existe para la empresa (ni en la bandeja, ni en el historial, ni en el detalle): decisión del dueño, 2026-10-06.
#: La columna booleana va tal cual (`voice_required` / `NOT voice_required`), nunca `IS true`: PostgreSQL no deduce el
#: predicado del índice parcial `ix_face_enrollments_voice_pending` (`voice_required AND voice_passed_at IS NULL`) desde
#: `voice_required IS TRUE` y recorría la tabla completa (medido con volumen: 89 540 filas descartadas).
FINISHED = or_(not_(FaceEnrollment.voice_required), FaceEnrollment.voice_passed_at.is_not(None))
#: Un registro que espera sus respuestas en video (implica el predicado del índice parcial).
UNFINISHED = and_(FaceEnrollment.voice_required, FaceEnrollment.voice_passed_at.is_(None))


class FaceEnrollmentRepository:
    """Registros faciales de UNA empresa (company_id en cada consulta)."""

    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id

    def get(self, enrollment_id: int, *, for_update: bool = False) -> FaceEnrollment | None:
        """Un registro terminado (lo que ve la empresa). `for_update`: bloquea la fila hasta el commit (aprobar y
        rechazar a la vez se serializan: la segunda petición ve el registro ya revisado)."""
        enrollment = self.get_any(enrollment_id, for_update=for_update)
        return enrollment if enrollment is not None and not enrollment.voice_pending else None

    def get_any(self, enrollment_id: int, *, for_update: bool = False) -> FaceEnrollment | None:
        """Un registro, terminado o no (la verificación por voz trabaja con uno sin terminar)."""
        # FOR UPDATE OF solo esta tabla: el empleado se carga con un JOIN externo que no se bloquea.
        return get_scoped(self.db, FaceEnrollment, enrollment_id, self.company_id, lock=for_update)

    def add(self, enrollment: FaceEnrollment) -> FaceEnrollment:
        enrollment.company_id = self.company_id
        self.db.add(enrollment)
        self.db.flush()
        return enrollment

    def company_name(self) -> str:
        """El nombre de la empresa (una pregunta de la verificación por voz)."""
        return str(self.db.scalar(select(Company.name).where(Company.id == self.company_id)) or "")

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
        """Los registros terminados de la empresa (índice `(company_id, status, submitted_at, id)`; la condición de
        terminado es un filtro residual sobre esa página: los que esperan su voz son muy pocos y duran minutos)."""
        stmt = select(FaceEnrollment).where(FaceEnrollment.company_id == self.company_id, FINISHED)
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
        referencia), en unas cuantas sentencias aunque sean miles. Sus videos salen también (`release_voice_clips`)."""
        conditions = [FaceEnrollment.company_id == self.company_id, FaceEnrollment.status == EnrollmentStatus.PENDING]
        if employee_id is not None:
            conditions.append(FaceEnrollment.employee_id == employee_id)
        StorageRepository(self.db).enqueue_from(FaceEnrollment.photo_object, and_(*conditions), datetime.now(UTC))
        self._release_clips(
            EnrollmentVoiceAnswer.enrollment_id.in_(select(FaceEnrollment.id).where(*conditions).scalar_subquery())
        )
        values = {
            "status": EnrollmentStatus.REJECTED,
            "rejection_reason": reason,
            "photo_object": None,
            "photo_size": None,
            "photo_sha256": None,
            "photo_uploaded_at": None,
        }
        return affected_rows(self.db, update(FaceEnrollment).where(*conditions).values(**values))

    # ------------------------------------------------------------------ verificación por voz

    def delete_unfinished(self, employee_id: int) -> int:
        """Un envío nuevo reemplaza el registro del empleado que aún esperaba sus respuestas en video (la persona
        cerró la app o se agotaron sus intentos): su foto y sus clips a la cola del bucket y sus filas fuera (las
        muestras se van en cascada). Tres sentencias, sin cargar filas."""
        ids = select(FaceEnrollment.id).where(
            FaceEnrollment.company_id == self.company_id, FaceEnrollment.employee_id == employee_id, UNFINISHED
        )
        now = datetime.now(UTC)
        StorageRepository(self.db).enqueue_from(FaceEnrollment.photo_object, FaceEnrollment.id.in_(ids), now)
        self._release_clips(EnrollmentVoiceAnswer.enrollment_id.in_(ids.scalar_subquery()))
        return affected_rows(self.db, delete(FaceEnrollment).where(FaceEnrollment.id.in_(ids.scalar_subquery())))

    def release_voice_clips(self, enrollment_id: int) -> None:
        """Los videos de un registro que se rechaza dejan de conservarse (minimización de datos): sus objetos a la cola
        del bucket y sus filas fuera, en esta misma transacción."""
        self._release_clips(
            and_(
                EnrollmentVoiceAnswer.company_id == self.company_id,
                EnrollmentVoiceAnswer.enrollment_id == enrollment_id,
            )
        )

    def _release_clips(self, condition: object) -> None:
        clause = and_(EnrollmentVoiceAnswer.company_id == self.company_id, condition)  # type: ignore[arg-type]
        StorageRepository(self.db).enqueue_from(EnrollmentVoiceAnswer.object_name, clause, datetime.now(UTC))
        affected_rows(self.db, delete(EnrollmentVoiceAnswer).where(clause))

    def count_failed_attempt(self, enrollment_id: int) -> int:
        """Suma un intento fallido de la verificación por voz y devuelve el total (sentencia atómica: dos respuestas a
        la vez no se pisan; el tope vive aquí y no en el token del cliente)."""
        stmt = (
            update(FaceEnrollment)
            .where(FaceEnrollment.id == enrollment_id, FaceEnrollment.company_id == self.company_id)
            .values(voice_attempts=FaceEnrollment.voice_attempts + 1)
            .returning(FaceEnrollment.voice_attempts)
        )
        return int(self.db.scalar(stmt) or 0)

    def add_flag(self, enrollment_id: int, flag_code: str) -> None:
        """Una marca para el revisor (catalog.enrollment_flags), una sola vez (inserción que ignora la repetida)."""
        stmt = dialect_insert(self.db, FaceEnrollmentFlag).values(
            enrollment_id=enrollment_id, company_id=self.company_id, flag_code=flag_code
        )
        self.db.execute(stmt.on_conflict_do_nothing(index_elements=["enrollment_id", "flag_code"]))

    def add_voice_answer(self, answer: EnrollmentVoiceAnswer) -> EnrollmentVoiceAnswer:
        answer.company_id = self.company_id
        self.db.add(answer)
        self.db.flush()
        return answer

    def voice_answers(self, enrollment_id: int) -> list[EnrollmentVoiceAnswer]:
        """Las respuestas aceptadas de un registro, en el orden de las preguntas (índice único de la posición)."""
        stmt = (
            select(EnrollmentVoiceAnswer)
            .where(
                EnrollmentVoiceAnswer.company_id == self.company_id,
                EnrollmentVoiceAnswer.enrollment_id == enrollment_id,
            )
            .order_by(EnrollmentVoiceAnswer.position)
        )
        return list(self.db.scalars(stmt))

    def voice_answer(self, enrollment_id: int, answer_id: int) -> EnrollmentVoiceAnswer | None:
        stmt = select(EnrollmentVoiceAnswer).where(
            EnrollmentVoiceAnswer.company_id == self.company_id,
            EnrollmentVoiceAnswer.enrollment_id == enrollment_id,
            EnrollmentVoiceAnswer.id == answer_id,
        )
        return self.db.scalar(stmt)
