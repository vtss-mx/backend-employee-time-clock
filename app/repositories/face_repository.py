from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement, Select, delete, func, select, true, update
from sqlalchemy.orm import Session

from app.models import Employee, FaceEmbedding, FaceStatus
from app.repositories.aggregates import affected_rows

#: (muestra, empleado, embedding cifrado, dimensión) de una fila de la galería.
GalleryRow = tuple[int, int, bytes, int]


@dataclass(frozen=True)
class LearningTotals:
    """Lo que la galería de una empresa aprendió del uso (muestras vigentes)."""

    approved_employees: int
    employees_learning: int
    learned_samples: int
    #: Identificaciones exitosas que decidió alguna muestra (aprobada o aprendida) vigente.
    identifications: int
    #: De ellas, las que decidió una muestra aprendida.
    learned_identifications: int
    last_learned_at: datetime | None


@dataclass(frozen=True)
class SampleStats:
    """Muestras activas de un empleado: cuántas, cuántas aprendió del uso y cuándo la última."""

    total: int = 0
    learned: int = 0
    last_learned_at: datetime | None = None


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

    def _gallery(self, stmt: Select, company_id: int, model_name: str) -> Select:
        return stmt.join(Employee, FaceEmbedding.employee_id == Employee.id).where(
            *self._gallery_filter(company_id, model_name)
        )

    def gallery_fingerprint(self, company_id: int, model_name: str) -> tuple[int, int, str]:
        """Huella barata (una consulta agregada) que cambia con cualquier alta, baja, aprobación,
        desactivación, muestra aprendida o reinicio de rostro: la caché de la galería se invalida sola."""
        stmt = select(func.count(FaceEmbedding.id), func.max(FaceEmbedding.id), func.max(Employee.updated_at))
        row = self.db.execute(self._gallery(stmt, company_id, model_name)).one()
        return int(row[0] or 0), int(row[1] or 0), str(row[2] or "")

    def gallery_index(self, company_id: int, model_name: str) -> list[tuple[int, int]]:
        """(muestra, empleado) de toda la galería, sin los vectores: para descifrar solo lo nuevo."""
        stmt = select(FaceEmbedding.id, FaceEmbedding.employee_id)
        rows = self.db.execute(self._gallery(stmt, company_id, model_name)).all()
        return [(int(sample_id), int(employee_id)) for sample_id, employee_id in rows]

    def gallery_rows(self, company_id: int, model_name: str, ids: Collection[int] | None = None) -> list[GalleryRow]:
        """Muestras de la galería de la empresa (todas o solo `ids`) con su embedding cifrado."""
        stmt = select(
            FaceEmbedding.id, FaceEmbedding.employee_id, FaceEmbedding.embedding_encrypted, FaceEmbedding.dimension
        )
        stmt = self._gallery(stmt, company_id, model_name)
        if ids is not None:
            stmt = stmt.where(FaceEmbedding.id.in_(ids))
        rows = self.db.execute(stmt.order_by(FaceEmbedding.id)).all()
        return [(int(sample_id), int(employee_id), bytes(data), int(dim)) for sample_id, employee_id, data, dim in rows]

    def approved_without_model(
        self, company_id: int, model_name: str, limit: int, exclude: Collection[int] = ()
    ) -> list[int]:
        """Empleados aprobados sin muestras del modelo actual (p. ej. tras cambiar de motor), en orden
        y sin los que ya fallaron hace poco (`exclude`): uno que no se puede migrar no frena al resto."""
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
                    Employee.id.not_in(list(exclude)) if exclude else true(),
                )
                .order_by(Employee.id)
                .limit(limit)
            )
        )

    # ---------- Muestras de un empleado ----------

    def list_active(self, employee_id: int, model_name: str) -> list[FaceEmbedding]:
        """Muestras vigentes del empleado para el modelo actual (las de otro modelo no se comparan)."""
        stmt = select(FaceEmbedding).where(
            FaceEmbedding.employee_id == employee_id,
            FaceEmbedding.active.is_(True),
            FaceEmbedding.model_name == model_name,
        )
        return list(self.db.scalars(stmt.order_by(FaceEmbedding.created_at)).all())

    def sample_stats(self, employee_ids: Iterable[int]) -> dict[int, SampleStats]:
        """Muestras activas por empleado (y lo aprendido): una consulta para toda la página."""
        ids = list(employee_ids)
        if not ids:
            return {}
        learned = FaceEmbedding.learned.is_(True)
        rows = self.db.execute(
            select(
                FaceEmbedding.employee_id,
                func.count(),
                func.count().filter(learned),
                func.max(FaceEmbedding.created_at).filter(learned),
            )
            .where(FaceEmbedding.employee_id.in_(ids), FaceEmbedding.active.is_(True))
            .group_by(FaceEmbedding.employee_id)
        ).all()
        return {int(e): SampleStats(int(total), int(taught), last) for e, total, taught, last in rows}

    def learning_totals(self, company_id: int) -> LearningTotals:
        """Evolución del reconocimiento de la empresa en una consulta (muestras de sus empleados)."""
        learned = FaceEmbedding.learned.is_(True)
        approved = (
            select(func.count())
            .where(
                Employee.company_id == company_id,
                Employee.active.is_(True),
                Employee.face_status == FaceStatus.APPROVED,
            )
            .scalar_subquery()
        )
        row = self.db.execute(
            select(
                approved,
                func.count(func.distinct(FaceEmbedding.employee_id)).filter(learned),
                func.count().filter(learned),
                func.coalesce(func.sum(FaceEmbedding.matches), 0),
                func.coalesce(func.sum(FaceEmbedding.matches).filter(learned), 0),
                func.max(FaceEmbedding.created_at).filter(learned),
            )
            .select_from(FaceEmbedding)
            .join(Employee, FaceEmbedding.employee_id == Employee.id)
            .where(Employee.company_id == company_id, FaceEmbedding.active.is_(True))
        ).one()
        return LearningTotals(int(row[0] or 0), int(row[1]), int(row[2]), int(row[3]), int(row[4]), row[5])

    def add(self, embedding: FaceEmbedding) -> FaceEmbedding:
        self.db.add(embedding)
        self.db.flush()
        return embedding

    def credit(self, sample_id: int, moment: datetime) -> None:
        """La muestra decidió una identificación exitosa: suma a su utilidad (sentencia atómica, sin
        leerla antes; dos identificaciones simultáneas no se pisan)."""
        stmt = update(FaceEmbedding).where(FaceEmbedding.id == sample_id)
        affected_rows(self.db, stmt.values(matches=FaceEmbedding.matches + 1, last_matched_at=moment))

    def delete_ids(self, ids: Collection[int]) -> None:
        """Borrado por id en una sentencia: si otra petición ya la borró, no falla."""
        affected_rows(self.db, delete(FaceEmbedding).where(FaceEmbedding.id.in_(ids)))

    def delete_learned(self, employee_id: int) -> int:
        """Olvida lo aprendido del empleado (las muestras de su registro aprobado se quedan)."""
        stmt = delete(FaceEmbedding).where(FaceEmbedding.employee_id == employee_id, FaceEmbedding.learned.is_(True))
        return affected_rows(self.db, stmt)

    def delete_for_company(self, company_id: int, employee_id: int | None = None) -> int:
        """Todas las muestras de los empleados de la empresa (o de uno): en una sola sentencia, sin
        cargar filas, aunque sean miles (nueva verificación de identidad de toda la empresa)."""
        people = select(Employee.id).where(Employee.company_id == company_id)
        if employee_id is not None:
            people = people.where(Employee.id == employee_id)
        return affected_rows(self.db, delete(FaceEmbedding).where(FaceEmbedding.employee_id.in_(people)))

    def delete_all(self, employee_id: int) -> None:
        """Borrado físico: los datos biométricos no se conservan al reemplazarse o eliminarse."""
        for item in self.db.scalars(select(FaceEmbedding).where(FaceEmbedding.employee_id == employee_id)):
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
