"""Consumo de la plataforma (esquema ops): guardado en lotes del medidor y consultas del ADMIN.

Toda lectura es de un rango de días acotado (a lo más `MAX_RANGE_DAYS`): por empresa con la llave
primaria (`company_id, day, ...`) y de toda la plataforma con el índice por día. Los conteos y sumas
se hacen en la base (GROUP BY), nunca en Python.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import ColumnElement, case, func, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import InstrumentedAttribute, Session

from app.core.soft_delete import with_deleted
from app.models import (
    AttendanceEvent,
    AuthSession,
    CaptureFingerprint,
    Charge,
    Company,
    CompanyDocument,
    Department,
    Employee,
    EmployeeAbsence,
    FaceAttemptMetric,
    FaceEmbedding,
    FaceEnrollment,
    Payment,
    StorageCategory,
    StorageSnapshot,
    UsageDaily,
    UsageRoute,
    UsageUser,
    User,
    Validator,
    ValidatorDevice,
    VerificationLog,
    WorkBreak,
    WorkSession,
)
from app.models.company import company_search_text
from app.models.usage import UsageCounters
from app.repositories.search import contains_text, search_term

#: Columnas de los contadores (las mismas en los tres granos).
COUNTERS = ("requests", "bytes_in", "bytes_out", "duration_ms", "server_errors", "client_errors")


def _upsert(db: Session, model: Any) -> Any:
    """INSERT ... ON CONFLICT del motor en uso (PostgreSQL en producción, SQLite en las pruebas)."""
    dialect = postgresql if db.get_bind().dialect.name == "postgresql" else sqlite
    return dialect.insert(model)


def sums(model: type[UsageCounters]) -> list[ColumnElement[Any]]:
    """SUM de cada contador (con 0 si no hay filas) y el máximo de `max_ms`, con su nombre."""
    columns: list[ColumnElement[Any]] = [
        func.coalesce(func.sum(getattr(model, name)), 0).label(name) for name in COUNTERS
    ]
    columns.append(func.coalesce(func.max(model.max_ms), 0).label("max_ms"))
    return columns


def page_rows(db: Session, stmt: Any, *, offset: int, limit: int, total: int) -> tuple[list[Any], int]:
    """Una página de filas de varias columnas (`paginate` entrega solo la primera)."""
    return list(db.execute(stmt.offset(offset).limit(limit)).all()), total


@dataclass(frozen=True)
class StorageSource:
    """Una tabla que cuenta para un grupo del almacenamiento y cómo se sabe de qué empresa es cada fila."""

    category: StorageCategory
    table: str
    company: InstrumentedAttribute[Any]
    #: Tabla por la que se llega a la empresa (las plantillas faciales no guardan la empresa).
    via: Any = None


#: Qué tablas cuentan en cada grupo del almacenamiento de una empresa (foto diaria del mantenimiento).
STORAGE_SOURCES: tuple[StorageSource, ...] = (
    StorageSource(StorageCategory.PEOPLE, "workforce.employees", Employee.company_id),
    StorageSource(StorageCategory.PEOPLE, "workforce.departments", Department.company_id),
    StorageSource(StorageCategory.PEOPLE, "workforce.validators", Validator.company_id),
    StorageSource(StorageCategory.PEOPLE, "workforce.validator_devices", ValidatorDevice.company_id),
    StorageSource(StorageCategory.BIOMETRICS, "biometrics.face_embeddings", Employee.company_id, FaceEmbedding),
    StorageSource(StorageCategory.BIOMETRICS, "biometrics.face_enrollments", FaceEnrollment.company_id),
    StorageSource(StorageCategory.BIOMETRICS, "biometrics.capture_fingerprints", CaptureFingerprint.company_id),
    StorageSource(StorageCategory.ATTENDANCE, "attendance.work_sessions", WorkSession.company_id),
    StorageSource(StorageCategory.ATTENDANCE, "attendance.work_breaks", WorkBreak.company_id),
    StorageSource(StorageCategory.ATTENDANCE, "attendance.attendance_events", AttendanceEvent.company_id),
    StorageSource(StorageCategory.ATTENDANCE, "attendance.verification_logs", VerificationLog.company_id),
    StorageSource(StorageCategory.ATTENDANCE, "workforce.employee_absences", EmployeeAbsence.company_id),
    StorageSource(StorageCategory.SECURITY, "auth.auth_sessions", AuthSession.company_id),
    StorageSource(StorageCategory.SECURITY, "ops.face_attempt_metrics", FaceAttemptMetric.company_id),
    StorageSource(StorageCategory.BILLING, "billing.charges", Charge.company_id),
    StorageSource(StorageCategory.BILLING, "billing.payments", Payment.company_id),
    # Documentos de la empresa (migración 0075): la fila de cada uno; su archivo suma su tamaño real (`document_bytes`).
    StorageSource(StorageCategory.BILLING, "tenancy.company_documents", CompanyDocument.company_id),
)


class UsageRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ---------- Guardado (hilo del medidor) ----------

    def add_counts(self, model: Any, rows: list[dict[str, Any]]) -> None:
        """Suma los contadores de un lote con UN upsert por grano: la fila nueva se inserta y la que ya
        existe suma lo nuevo (`max_ms` se queda con el mayor). Atómico entre instancias."""
        stmt = _upsert(self.db, model)
        keys = [column.name for column in model.__table__.primary_key.columns]
        table = model.__table__.c
        updates: dict[str, Any] = {name: table[name] + stmt.excluded[name] for name in COUNTERS}
        updates["max_ms"] = case((stmt.excluded.max_ms > table.max_ms, stmt.excluded.max_ms), else_=table.max_ms)
        self.db.execute(stmt.on_conflict_do_update(index_elements=keys, set_=updates), rows)

    # ---------- Plataforma ----------

    def platform_totals(self, start: date, end: date) -> Any:
        """Contadores de toda la plataforma en el rango (índice por día)."""
        stmt = select(*sums(UsageDaily)).where(UsageDaily.day.between(start, end))
        return self.db.execute(stmt).one()

    def platform_days(self, start: date, end: date) -> list[Any]:
        """Contadores por día de toda la plataforma (solo los días con tráfico)."""
        stmt = (
            select(UsageDaily.day, *sums(UsageDaily))
            .where(UsageDaily.day.between(start, end))
            .group_by(UsageDaily.day)
            .order_by(UsageDaily.day)
        )
        return list(self.db.execute(stmt).all())

    def companies_with_traffic(self, start: date, end: date) -> int:
        stmt = select(func.count(func.distinct(UsageDaily.company_id))).where(
            UsageDaily.day.between(start, end), UsageDaily.company_id > 0
        )
        return int(self.db.scalar(stmt) or 0)

    def company_usage_page(
        self, start: date, end: date, *, search: str | None, sort: str, offset: int, limit: int
    ) -> tuple[list[Any], int]:
        """Empresas con su consumo del rango y su almacenamiento más reciente, ordenadas por `sort` (la
        de mayor consumo primero). Una consulta agrega el rango por empresa (índice por día) y otra
        cuenta las empresas del filtro."""
        usage = (
            select(UsageDaily.company_id, *sums(UsageDaily))
            .where(UsageDaily.day.between(start, end), UsageDaily.company_id > 0)
            .group_by(UsageDaily.company_id)
            .subquery()
        )
        storage = self._latest_storage_by_company().subquery()
        filters = self._company_filter(search)
        zero = 0
        order_by = {
            "requests": func.coalesce(usage.c.requests, zero),
            "bytes": func.coalesce(usage.c.bytes_in, zero) + func.coalesce(usage.c.bytes_out, zero),
            "duration": func.coalesce(usage.c.duration_ms, zero),
            "errors": func.coalesce(usage.c.server_errors, zero) + func.coalesce(usage.c.client_errors, zero),
            "storage": func.coalesce(storage.c.bytes, zero),
        }[sort]
        stmt = (
            select(
                Company.id,
                Company.name,
                Company.active,
                Company.suspended_at,
                *(func.coalesce(usage.c[name], zero).label(name) for name in (*COUNTERS, "max_ms")),
                func.coalesce(storage.c.rows, zero).label("storage_rows"),
                func.coalesce(storage.c.bytes, zero).label("storage_bytes"),
            )
            .outerjoin(usage, usage.c.company_id == Company.id)
            .outerjoin(storage, storage.c.company_id == Company.id)
            .where(*filters)
            .order_by(order_by.desc(), func.lower(Company.name), Company.id)
        )
        total = int(self.db.scalar(select(func.count()).select_from(Company).where(*filters)) or 0)
        return page_rows(self.db, stmt, offset=offset, limit=limit, total=total)

    @staticmethod
    def _company_filter(search: str | None) -> list[ColumnElement[bool]]:
        term = search_term(search)
        return [contains_text(company_search_text(), term)] if term else []

    def _latest_storage_by_company(self) -> Any:
        """Filas y bytes de la foto más reciente del almacenamiento, por empresa."""
        latest = select(func.max(StorageSnapshot.day)).scalar_subquery()
        return (
            select(
                StorageSnapshot.company_id,
                func.sum(StorageSnapshot.rows).label("rows"),
                func.sum(StorageSnapshot.bytes).label("bytes"),
            )
            .where(StorageSnapshot.day == latest)
            .group_by(StorageSnapshot.company_id)
        )

    def platform_storage(self) -> tuple[date | None, list[Any]]:
        """La foto más reciente del almacenamiento de toda la plataforma, por grupo."""
        day = self.db.scalar(select(func.max(StorageSnapshot.day)))
        if day is None:
            return None, []
        stmt = (
            select(
                StorageSnapshot.category,
                func.sum(StorageSnapshot.rows).label("rows"),
                func.sum(StorageSnapshot.bytes).label("bytes"),
            )
            .where(StorageSnapshot.day == day)
            .group_by(StorageSnapshot.category)
        )
        return day, list(self.db.execute(stmt).all())

    # ---------- Una empresa ----------

    def company_totals(self, company_id: int, start: date, end: date) -> Any:
        stmt = select(*sums(UsageDaily)).where(UsageDaily.company_id == company_id, UsageDaily.day.between(start, end))
        return self.db.execute(stmt).one()

    def company_days(self, company_id: int, start: date, end: date) -> list[Any]:
        stmt = (
            select(UsageDaily.day, *sums(UsageDaily))
            .where(UsageDaily.company_id == company_id, UsageDaily.day.between(start, end))
            .group_by(UsageDaily.day)
            .order_by(UsageDaily.day)
        )
        return list(self.db.execute(stmt).all())

    def routes_page(self, company_id: int, start: date, end: date, *, offset: int, limit: int) -> tuple[list[Any], int]:
        """Rutas de la empresa en el rango, la más usada primero (llave primaria de la empresa)."""
        where = (UsageRoute.company_id == company_id, UsageRoute.day.between(start, end))
        stmt = (
            select(UsageRoute.route, *sums(UsageRoute))
            .where(*where)
            .group_by(UsageRoute.route)
            .order_by(func.sum(UsageRoute.requests).desc(), UsageRoute.route)
        )
        total = int(self.db.scalar(select(func.count(func.distinct(UsageRoute.route))).where(*where)) or 0)
        return page_rows(self.db, stmt, offset=offset, limit=limit, total=total)

    def users_page(self, company_id: int, start: date, end: date, *, offset: int, limit: int) -> tuple[list[Any], int]:
        """Cuentas de la empresa en el rango, la que más consume primero (llave primaria de la empresa)."""
        where = (UsageUser.company_id == company_id, UsageUser.day.between(start, end))
        stmt = (
            select(UsageUser.user_id, *sums(UsageUser))
            .where(*where)
            .group_by(UsageUser.user_id)
            .order_by(func.sum(UsageUser.requests).desc(), UsageUser.user_id)
        )
        total = int(self.db.scalar(select(func.count(func.distinct(UsageUser.user_id))).where(*where)) or 0)
        return page_rows(self.db, stmt, offset=offset, limit=limit, total=total)

    def company_storage(self, company_id: int) -> tuple[date | None, list[Any]]:
        """La foto más reciente del almacenamiento de la empresa, por grupo (llave primaria)."""
        day = self.db.scalar(select(func.max(StorageSnapshot.day)).where(StorageSnapshot.company_id == company_id))
        if day is None:
            return None, []
        stmt = select(StorageSnapshot.category, StorageSnapshot.rows, StorageSnapshot.bytes).where(
            StorageSnapshot.company_id == company_id, StorageSnapshot.day == day
        )
        return day, list(self.db.execute(stmt).all())

    # ---------- Almacenamiento (mantenimiento) ----------

    def save_storage(self, day: date, rows: Sequence[dict[str, Any]]) -> int:
        """Guarda (o rehace) la foto del día: un upsert para todas las empresas y grupos."""
        if not rows:
            return 0
        stmt = _upsert(self.db, StorageSnapshot)
        self.db.execute(
            stmt.on_conflict_do_update(
                index_elements=["company_id", "day", "category"],
                set_={"rows": stmt.excluded.rows, "bytes": stmt.excluded.bytes},
            ),
            [{**row, "day": day} for row in rows],
        )
        return len(rows)

    def table_counts(self) -> dict[str, dict[int, int]]:
        """{tabla: {empresa: filas}} de cada tabla del almacenamiento: un GROUP BY por tabla (una vez al
        día; las filas sin empresa se omiten). También las filas en «Eliminados»: ocupan espacio hasta su
        depuración."""
        counts = {}
        for source in STORAGE_SOURCES:
            stmt = select(source.company, func.count())
            if source.via is not None:
                stmt = stmt.select_from(source.via).join(Employee, Employee.id == source.via.employee_id)
            rows = self.db.execute(with_deleted(stmt.group_by(source.company)))
            counts[source.table] = {int(company): int(count) for company, count in rows if company is not None}
        return counts

    def receipt_bytes(self) -> dict[int, int]:
        """Bytes reales de los comprobantes de pago por empresa."""
        stmt = (
            select(Payment.company_id, func.sum(Payment.receipt_size))
            .where(Payment.receipt_size.is_not(None))
            .group_by(Payment.company_id)
        )
        return {int(company): int(size or 0) for company, size in self.db.execute(stmt)}

    def document_bytes(self) -> dict[int, int]:
        """Bytes reales de los documentos de cada empresa (también los de «Eliminados»: su archivo sigue en el bucket
        hasta que la depuración borra la fila). Un `GROUP BY` por el índice de la empresa."""
        stmt = select(CompanyDocument.company_id, func.sum(CompanyDocument.byte_size)).group_by(
            CompanyDocument.company_id
        )
        return {int(company): int(size) for company, size in self.db.execute(with_deleted(stmt))}

    def accounts(self, company_id: int, ids: list[int]) -> dict[int, tuple[str, str, str | None]]:
        """Correo, rol y (si es empleado de ESA empresa) nombre de varias cuentas: dos consultas. Historial del
        consumo: también las cuentas y los empleos que están en «Eliminados»."""
        users = self.db.execute(with_deleted(select(User.id, User.email, User.role).where(User.id.in_(ids)))).all()
        names = dict(
            self.db.execute(
                with_deleted(
                    select(Employee.user_id, Employee.first_name + " " + Employee.last_name).where(
                        Employee.company_id == company_id, Employee.user_id.in_(ids)
                    )
                )
            ).all()
        )
        return {int(uid): (str(email), str(role.value), names.get(uid)) for uid, email, role in users}

    def relation_bytes(self, tables: Iterable[str]) -> dict[str, int]:
        """Bytes que ocupa cada tabla con sus índices y TOAST (`pg_total_relation_size`), en UNA consulta.
        Solo PostgreSQL; en otro motor (pruebas con SQLite) no hay tamaño: {}."""
        names = sorted(set(tables))
        if self.db.get_bind().dialect.name != "postgresql" or not names:
            return {}
        sizes = [func.pg_total_relation_size(func.to_regclass(name)) for name in names]
        row = self.db.execute(select(*sizes)).one()
        return {name: int(size or 0) for name, size in zip(names, row, strict=True)}
