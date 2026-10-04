"""Consultas del asistente de reportes: los datos de UNA empresa según el catálogo, y su memoria.

Aislamiento: el repositorio se construye con la empresa de la sesión y CADA fuente la fija en su
WHERE (y en cada JOIN a otra tabla con empresa). No hay forma de pedirle datos de otra: el plan del
reporte no lleva empresa y solo nombra columnas del catálogo, nunca SQL.

Las expresiones que dependen del motor (día en la hora del negocio, horas entre dos momentos,
textos unidos) viven en `_Sql`: PostgreSQL en producción, SQLite en las pruebas.
"""

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import (
    ColumnElement,
    Date,
    Float,
    Integer,
    Select,
    and_,
    case,
    cast,
    desc,
    extract,
    false,
    func,
    literal,
    or_,
    select,
    true,
    update,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session, aliased

from app.core.clock import business_now
from app.core.config import settings
from app.models import (
    AssistantQuery,
    CompanyApiKey,
    CompanyApiKeyScope,
    Department,
    DepartmentManager,
    DeviceStatus,
    Employee,
    FaceEmbedding,
    FaceEnrollment,
    LearnedPhrase,
    SavedReport,
    User,
    Validator,
    ValidatorDevice,
    VerificationLog,
)
from app.repositories.aggregates import paginate


@dataclass(frozen=True)
class Window:
    """Periodo ya resuelto: [start, end) en UTC; None = sin límite de ese lado."""

    start: datetime | None = None
    end: datetime | None = None


@dataclass(frozen=True)
class Criterion:
    """Un filtro ya validado contra el catálogo (valores con su tipo)."""

    column: str
    op: str
    values: tuple[Any, ...]
    kind: str


@dataclass(frozen=True)
class ReportQuery:
    """Lo que se consulta (el plan ya validado y resuelto por el servicio)."""

    dataset: str
    columns: tuple[str, ...] = ()
    criteria: tuple[Criterion, ...] = ()
    window: Window = field(default_factory=Window)
    group_by: tuple[str, ...] = ()
    metric: str = "count"
    metric_column: str | None = None
    #: Columna del orden, o "metric" (los grupos por su total).
    sort: str | None = None
    descending: bool = False


@dataclass(frozen=True)
class Source:
    """FROM, JOINs y WHERE de la empresa (y del periodo) más las expresiones de cada columna."""

    stmt: Select[Any]
    #: Código de columna del catálogo → su expresión SQL.
    columns: dict[str, Any]
    #: Desempate estable del orden (el id de la fila principal).
    key: Any


class _Sql:
    """Expresiones que dependen del motor de BD."""

    def __init__(self, db: Session) -> None:
        self.postgres = db.get_bind().dialect.name == "postgresql"
        offset = business_now().utcoffset() or timedelta(0)
        self.shift = f"{int(offset.total_seconds() // 3600):+d} hours"
        self.zone = settings.APP_TIMEZONE

    def _local(self, moment: Any) -> Any:
        return func.timezone(self.zone, moment)

    def day(self, moment: Any) -> ColumnElement[date]:
        if self.postgres:
            return cast(self._local(moment), Date)
        return func.date(moment, self.shift, type_=Date)

    def week(self, moment: Any) -> ColumnElement[date]:
        """Lunes de la semana."""
        if self.postgres:
            return cast(func.date_trunc("week", self._local(moment)), Date)
        return func.date(moment, self.shift, "-6 days", "weekday 1", type_=Date)

    def month(self, moment: Any) -> ColumnElement[str]:
        if self.postgres:
            return func.to_char(self._local(moment), "YYYY-MM")
        return func.strftime("%Y-%m", moment, self.shift)

    def hour(self, moment: Any) -> ColumnElement[int]:
        if self.postgres:
            return cast(extract("hour", self._local(moment)), Integer)
        return cast(func.strftime("%H", moment, self.shift), Integer)

    def decimal_hour(self, moment: Any) -> ColumnElement[float]:
        """9:30 → 9.5."""
        if self.postgres:
            local = self._local(moment)
            return cast(extract("hour", local) + extract("minute", local) / 60.0, Float)
        return (
            cast(func.strftime("%H", moment, self.shift), Float)
            + cast(func.strftime("%M", moment, self.shift), Float) / 60.0
        )

    def hours_between(self, first: Any, last: Any) -> ColumnElement[float]:
        if self.postgres:
            return cast(extract("epoch", last - first) / 3600.0, Float)
        return cast((func.julianday(last) - func.julianday(first)) * 24.0, Float)

    def join_text(self, text: Any) -> ColumnElement[str]:
        if self.postgres:
            return func.string_agg(text, literal(", "))
        return func.group_concat(text, ", ")


def _full_name(employee: Any) -> ColumnElement[str]:
    return employee.first_name.concat(" ").concat(employee.last_name)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class ReportRepository:
    """Datos de la empresa `company_id` para el asistente (nunca de otra)."""

    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id
        self.sql = _Sql(db)

    # ---------- Fuentes (una por reporte del catálogo) ----------

    def _within(self, moment: Any, window: Window) -> ColumnElement[bool]:
        conditions = []
        if window.start is not None:
            conditions.append(moment >= window.start)
        if window.end is not None:
            conditions.append(moment < window.end)
        return and_(true(), *conditions)

    def _department_join(self, employee: Any) -> Any:
        return and_(Department.id == employee.department_id, Department.company_id == employee.company_id)

    def _employees(self, window: Window) -> Source:
        e = Employee
        stmt = (
            select(e.id)
            .select_from(e)
            .join(User, User.id == e.user_id)
            .outerjoin(Department, self._department_join(e))
            .where(e.company_id == self.company_id, self._within(e.created_at, window))
        )
        columns: dict[str, Any] = {
            "employee_number": e.employee_number,
            "full_name": _full_name(e),
            "department": Department.name,
            "email": User.email,
            "phone": User.phone,
            "rfc": e.rfc,
            "curp": e.curp,
            "nss": e.nss,
            "birth_date": e.birth_date,
            "active": e.active,
            "face_status": e.face_status,
            "headwear_exempt": e.headwear_exempt,
            "created_at": e.created_at,
        }
        return Source(stmt, columns, e.id)

    def _departments(self, window: Window) -> Source:
        dep, manager = Department, aliased(Employee)
        employees = (
            select(func.count())
            .select_from(Employee)
            .where(Employee.department_id == dep.id, Employee.company_id == self.company_id)
            .correlate(dep)
            .scalar_subquery()
        )
        managers = (
            select(self.sql.join_text(_full_name(manager)))
            .select_from(DepartmentManager)
            .join(manager, manager.id == DepartmentManager.employee_id)
            .where(DepartmentManager.department_id == dep.id, DepartmentManager.company_id == self.company_id)
            .correlate(dep)
            .scalar_subquery()
        )
        stmt = select(dep.id).where(dep.company_id == self.company_id, self._within(dep.created_at, window))
        columns: dict[str, Any] = {
            "name": dep.name,
            "description": dep.description,
            "employees": employees,
            "managers": managers,
            "created_at": dep.created_at,
        }
        return Source(stmt, columns, dep.id)

    def _attendance(self, window: Window) -> Source:
        log, e, v = VerificationLog, Employee, Validator
        validator = case(
            (v.id.is_not(None), v.name),
            (and_(log.user_id.is_not(None), log.user_id == e.user_id), literal("El propio empleado")),
            else_=literal("Sin validador"),
        )
        stmt = (
            select(log.id)
            .select_from(log)
            .outerjoin(e, and_(e.id == log.employee_id, e.company_id == self.company_id))
            .outerjoin(Department, self._department_join(e))
            .outerjoin(v, and_(v.user_id == log.user_id, v.company_id == self.company_id))
            .where(log.company_id == self.company_id, self._within(log.created_at, window))
        )
        moment = log.created_at
        columns: dict[str, Any] = {
            "occurred_at": moment,
            "day": self.sql.day(moment),
            "week": self.sql.week(moment),
            "month": self.sql.month(moment),
            "hour": self.sql.hour(moment),
            "employee": _full_name(e),
            "employee_number": e.employee_number,
            "department": Department.name,
            "method": log.method,
            "success": log.success,
            "reason": log.reason,
            "validator": validator,
            "score": log.score,
        }
        return Source(stmt, columns, log.id)

    def _workdays(self, window: Window) -> Source:
        """Por empleado y día: primera y última identificación exitosa (el periodo se aplica antes de
        agrupar: solo se leen las identificaciones de esos días)."""
        log = VerificationLog
        day = self.sql.day(log.created_at)
        days = (
            select(
                log.employee_id.label("employee_id"),
                day.label("day"),
                func.min(log.created_at).label("entry"),
                func.max(log.created_at).label("exit"),
                func.count().label("checks"),
            )
            .where(
                log.company_id == self.company_id,
                log.success.is_(True),
                log.employee_id.is_not(None),
                self._within(log.created_at, window),
            )
            .group_by(log.employee_id, day)
            .subquery("workdays")
        )
        e = Employee
        stmt = (
            select(e.id)
            .select_from(days)
            .join(e, and_(e.id == days.c.employee_id, e.company_id == self.company_id))
            .outerjoin(Department, self._department_join(e))
        )
        columns: dict[str, Any] = {
            "day": days.c.day,
            "employee": _full_name(e),
            "employee_number": e.employee_number,
            "department": Department.name,
            "entry": days.c.entry,
            "exit": days.c.exit,
            "hours": self.sql.hours_between(days.c.entry, days.c.exit),
            "checks": days.c.checks,
            "entry_hour": self.sql.decimal_hour(days.c.entry),
            "exit_hour": self.sql.decimal_hour(days.c.exit),
        }
        return Source(stmt, columns, e.id)

    def _enrollments(self, window: Window) -> Source:
        f, e, reviewer = FaceEnrollment, Employee, aliased(User)
        stmt = (
            select(f.id)
            .select_from(f)
            .join(e, and_(e.id == f.employee_id, e.company_id == self.company_id))
            .outerjoin(Department, self._department_join(e))
            .outerjoin(reviewer, reviewer.id == f.reviewed_by_id)
            .where(f.company_id == self.company_id, self._within(f.submitted_at, window))
        )
        columns: dict[str, Any] = {
            "submitted_at": f.submitted_at,
            "employee": _full_name(e),
            "employee_number": e.employee_number,
            "department": Department.name,
            "status": f.status,
            "reviewed_at": f.reviewed_at,
            "reviewer": reviewer.email,
            "rejection_reason": f.rejection_reason,
            "in_person": case((f.captured_by_id.is_not(None), true()), else_=false()),
            "quality": f.quality_score,
        }
        return Source(stmt, columns, f.id)

    def _validators(self, window: Window) -> Source:
        v, log = Validator, VerificationLog
        mine = and_(log.company_id == self.company_id, log.user_id == v.user_id)
        identifications = select(func.count()).select_from(log).where(mine).correlate(v).scalar_subquery()
        last = select(func.max(log.created_at)).where(mine).correlate(v).scalar_subquery()
        devices = (
            select(func.count())
            .select_from(ValidatorDevice)
            .where(ValidatorDevice.validator_id == v.id, ValidatorDevice.status == DeviceStatus.APPROVED)
            .correlate(v)
            .scalar_subquery()
        )
        stmt = select(v.id).where(v.company_id == self.company_id, self._within(v.created_at, window))
        columns: dict[str, Any] = {
            "name": v.name,
            "mode": v.mode,
            "city": v.city,
            "state": v.state,
            "location_required": v.location_required,
            "identifications": identifications,
            "last_identification": last,
            "devices": devices,
            "created_at": v.created_at,
        }
        return Source(stmt, columns, v.id)

    def _devices(self, window: Window) -> Source:
        device, v = ValidatorDevice, Validator
        stmt = (
            select(device.id)
            .select_from(device)
            .join(v, and_(v.id == device.validator_id, v.company_id == self.company_id))
            .where(device.company_id == self.company_id, self._within(device.created_at, window))
        )
        columns: dict[str, Any] = {
            "validator": v.name,
            "name": device.name,
            "status": device.status,
            "created_at": device.created_at,
            "last_seen_at": device.last_seen_at,
        }
        return Source(stmt, columns, device.id)

    def _api_keys(self, window: Window) -> Source:
        key = CompanyApiKey
        now = datetime.now(UTC)
        status = case(
            (key.revoked_at.is_not(None), literal("REVOKED")),
            (and_(key.expires_at.is_not(None), key.expires_at <= now), literal("EXPIRED")),
            else_=literal("ACTIVE"),
        )
        scopes = (
            select(self.sql.join_text(CompanyApiKeyScope.scope))
            .where(CompanyApiKeyScope.api_key_id == key.id)
            .correlate(key)
            .scalar_subquery()
        )
        stmt = select(key.id).where(key.company_id == self.company_id, self._within(key.created_at, window))
        columns: dict[str, Any] = {
            "name": key.name,
            "prefix": key.prefix,
            "status": status,
            "scopes": scopes,
            "created_at": key.created_at,
            "expires_at": key.expires_at,
            "last_used_at": key.last_used_at,
        }
        return Source(stmt, columns, key.id)

    def _face_learning(self, _window: Window) -> Source:
        """Conteos por empleado (nunca los vectores): el registro, lo aprendido y su utilidad."""
        sample, e = FaceEmbedding, Employee
        stats = (
            select(
                sample.employee_id.label("employee_id"),
                func.sum(case((sample.learned.is_(False), 1), else_=0)).label("anchors"),
                func.sum(case((sample.learned.is_(True), 1), else_=0)).label("learned"),
                func.coalesce(func.sum(sample.matches), 0).label("matches"),
                func.max(case((sample.learned.is_(True), sample.created_at))).label("last_learned_at"),
            )
            .join(e, and_(e.id == sample.employee_id, e.company_id == self.company_id))
            .where(sample.active.is_(True))
            .group_by(sample.employee_id)
            .subquery("learning")
        )
        stmt = (
            select(e.id)
            .select_from(stats)
            .join(e, and_(e.id == stats.c.employee_id, e.company_id == self.company_id))
            .outerjoin(Department, self._department_join(e))
        )
        columns: dict[str, Any] = {
            "employee": _full_name(e),
            "employee_number": e.employee_number,
            "department": Department.name,
            "anchors": stats.c.anchors,
            "learned": stats.c.learned,
            "matches": stats.c.matches,
            "last_learned_at": stats.c.last_learned_at,
        }
        return Source(stmt, columns, e.id)

    def source(self, dataset: str, window: Window) -> Source:
        return SOURCES[dataset](self, window)

    # ---------- Consultas sobre una fuente ----------

    def _condition(self, expr: ColumnElement[Any], criterion: Criterion) -> ColumnElement[bool]:
        values = criterion.values
        if criterion.op == "contains":
            return or_(*(expr.ilike(f"%{_escape_like(str(v))}%", escape="\\") for v in values))
        compare: dict[str, Callable[[Any, Any], ColumnElement[bool]]] = {
            "gt": lambda a, b: a > b,
            "gte": lambda a, b: a >= b,
            "lt": lambda a, b: a < b,
            "lte": lambda a, b: a <= b,
        }
        if criterion.op in compare:
            return compare[criterion.op](expr, values[0])
        if criterion.kind == "text":
            matched: ColumnElement[bool] = func.lower(expr).in_([str(v).lower() for v in values])
        elif criterion.kind == "bool":
            matched = or_(*(expr.is_(true()) if v else or_(expr.is_(false()), expr.is_(None)) for v in values))
        else:
            matched = expr.in_(list(values))
        return ~matched if criterion.op == "not_in" else matched

    def _filtered(self, query: ReportQuery) -> tuple[Source, Select[Any]]:
        source = self.source(query.dataset, query.window)
        conditions = [self._condition(source.columns[c.column], c) for c in query.criteria]
        return source, source.stmt.where(*conditions)

    def _order(self, source: Source, query: ReportQuery) -> list[Any]:
        if query.sort is None or query.sort not in source.columns:
            return [source.key]
        expr = source.columns[query.sort]
        return [(expr.desc() if query.descending else expr.asc()).nulls_last(), source.key]

    def rows(self, query: ReportQuery, *, offset: int, limit: int) -> list[tuple[Any, ...]]:
        source, stmt = self._filtered(query)
        stmt = stmt.with_only_columns(*(source.columns[c] for c in query.columns))
        return [
            tuple(row)
            for row in self.db.execute(stmt.order_by(*self._order(source, query)).offset(offset).limit(limit))
        ]

    def stream(self, query: ReportQuery, *, limit: int) -> Iterator[tuple[Any, ...]]:
        """Todas las filas (hasta `limit`) por partes: un reporte grande no se carga entero en memoria."""
        source, stmt = self._filtered(query)
        stmt = stmt.with_only_columns(*(source.columns[c] for c in query.columns))
        result = self.db.execute(
            stmt.order_by(*self._order(source, query)).limit(limit).execution_options(yield_per=2000)
        )
        for row in result:
            yield tuple(row)

    def count(self, query: ReportQuery) -> int:
        _source, stmt = self._filtered(query)
        return int(self.db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)

    def _metric(self, source: Source, query: ReportQuery) -> ColumnElement[Any]:
        if query.metric == "count" or query.metric_column is None:
            return func.count()
        expr = source.columns[query.metric_column]
        aggregate: dict[str, Callable[[Any], ColumnElement[Any]]] = {
            "sum": func.sum,
            "avg": func.avg,
            "min": func.min,
            "max": func.max,
        }
        return aggregate[query.metric](expr)

    def aggregate(self, query: ReportQuery) -> Any:
        """Un solo valor sobre todas las filas (p. ej. el promedio de horas)."""
        source, stmt = self._filtered(query)
        return self.db.scalar(stmt.with_only_columns(self._metric(source, query)))

    def groups(self, query: ReportQuery, *, limit: int) -> tuple[list[tuple[Any, ...]], int]:
        """Totales por grupo (valores del grupo..., total) y cuántos grupos hay en total."""
        source, stmt = self._filtered(query)
        keys = [source.columns[c] for c in query.group_by]
        metric = self._metric(source, query).label("metric")
        grouped = stmt.with_only_columns(*keys, metric).group_by(*keys)
        total = int(self.db.scalar(select(func.count()).select_from(grouped.subquery())) or 0)
        if query.sort == "metric":
            order = [desc(metric) if query.descending else metric.asc(), *keys]
        else:
            order = [k.desc() if query.descending else k.asc() for k in keys]
        rows = [tuple(row) for row in self.db.execute(grouped.order_by(*order).limit(limit))]
        return rows, total

    # ---------- Nombres propios (para reconocerlos en una pregunta) ----------

    def department_names(self) -> list[str]:
        stmt = select(Department.name).where(Department.company_id == self.company_id).order_by(Department.name)
        return list(self.db.scalars(stmt))

    def employee_names(self, limit: int = 20_000) -> list[tuple[str, str]]:
        """(nombre completo, número de empleado) de la empresa."""
        stmt = (
            select(_full_name(Employee), Employee.employee_number)
            .where(Employee.company_id == self.company_id)
            .order_by(Employee.id)
            .limit(limit)
        )
        return [(str(name), str(number)) for name, number in self.db.execute(stmt)]

    def validator_names(self) -> list[str]:
        stmt = select(Validator.name).where(Validator.company_id == self.company_id).order_by(Validator.name)
        return list(self.db.scalars(stmt))

    # ---------- Memoria del asistente ----------

    def record(self, query: AssistantQuery) -> AssistantQuery:
        query.company_id = self.company_id
        self.db.add(query)
        self.db.flush()
        return query

    def query(self, query_id: int) -> AssistantQuery | None:
        stmt = select(AssistantQuery).where(AssistantQuery.id == query_id, AssistantQuery.company_id == self.company_id)
        return self.db.scalar(stmt)

    def popular_questions(self, since: datetime, limit: int) -> list[str]:
        """Lo que más pregunta la empresa (respondido y no marcado como inútil), lo más reciente primero."""
        count = func.count().label("times")
        latest = func.max(AssistantQuery.created_at).label("latest")
        stmt = (
            select(func.max(AssistantQuery.question), count, latest)
            .where(
                AssistantQuery.company_id == self.company_id,
                AssistantQuery.created_at >= since,
                AssistantQuery.dataset.is_not(None),
                or_(AssistantQuery.helpful.is_(None), AssistantQuery.helpful.is_(True)),
            )
            .group_by(AssistantQuery.normalized)
            .order_by(desc(count), desc(latest))
            .limit(limit)
        )
        return [str(row[0]) for row in self.db.execute(stmt)]

    def learned(self) -> dict[str, dict[str, int]]:
        """Vocabulario aprendido de la empresa: {palabra: {reporte: aciertos}}."""
        stmt = select(LearnedPhrase.phrase, LearnedPhrase.dataset, LearnedPhrase.hits).where(
            LearnedPhrase.company_id == self.company_id, LearnedPhrase.hits > 0
        )
        vocabulary: dict[str, dict[str, int]] = {}
        for phrase, dataset, hits in self.db.execute(stmt):
            vocabulary.setdefault(phrase, {})[dataset] = int(hits)
        return vocabulary

    def reinforce(self, phrases: Iterable[str], dataset: str, delta: int) -> None:
        """Suma (o resta) aciertos a cada palabra para ese reporte, en una sentencia por palabra."""
        for phrase in phrases:
            if delta < 0:
                self.db.execute(
                    update(LearnedPhrase)
                    .where(
                        LearnedPhrase.company_id == self.company_id,
                        LearnedPhrase.phrase == phrase,
                        LearnedPhrase.dataset == dataset,
                    )
                    .values(hits=case((LearnedPhrase.hits + delta < 0, 0), else_=LearnedPhrase.hits + delta))
                )
                continue
            dialect = postgresql if self.sql.postgres else sqlite
            stmt = dialect.insert(LearnedPhrase).values(
                company_id=self.company_id, phrase=phrase, dataset=dataset, hits=delta
            )
            self.db.execute(
                stmt.on_conflict_do_update(
                    index_elements=[LearnedPhrase.company_id, LearnedPhrase.phrase, LearnedPhrase.dataset],
                    set_={"hits": LearnedPhrase.hits + delta, "updated_at": func.now()},
                )
            )

    # ---------- Reportes guardados ----------

    def saved_page(self, *, offset: int, limit: int) -> tuple[list[SavedReport], int]:
        stmt = select(SavedReport).where(SavedReport.company_id == self.company_id)
        return paginate(self.db, stmt, (func.lower(SavedReport.name), SavedReport.id), offset=offset, limit=limit)

    def saved(self, report_id: int) -> SavedReport | None:
        stmt = select(SavedReport).where(SavedReport.id == report_id, SavedReport.company_id == self.company_id)
        return self.db.scalar(stmt)

    def add_saved(self, report: SavedReport) -> SavedReport:
        report.company_id = self.company_id
        self.db.add(report)
        self.db.flush()
        return report

    def delete_saved(self, report: SavedReport) -> None:
        self.db.delete(report)
        self.db.flush()


#: Fuente de cada reporte del catálogo (`app/services/reporting/datasets.py`).
SOURCES: dict[str, Callable[[ReportRepository, Window], Source]] = {
    "employees": ReportRepository._employees,
    "departments": ReportRepository._departments,
    "attendance": ReportRepository._attendance,
    "workdays": ReportRepository._workdays,
    "enrollments": ReportRepository._enrollments,
    "validators": ReportRepository._validators,
    "devices": ReportRepository._devices,
    "api_keys": ReportRepository._api_keys,
    "face_learning": ReportRepository._face_learning,
}
