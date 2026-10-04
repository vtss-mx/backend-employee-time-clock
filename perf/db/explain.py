"""EXPLAIN (ANALYZE, BUFFERS) de las consultas REALES de los repositorios sobre la base de volumen.

Lo ejecuta `perf/db/run.sh` dentro de la imagen dev contra un PostgreSQL AISLADO sembrado con
`perf/db/seed.sql` (jamás la base de trabajo). Cada caso llama un método de un repositorio con
parámetros realistas (la empresa grande de 20 000 empleados o una de 400); se capturan las sentencias
que emite y se explica cada una con sus mismos parámetros, dentro de una transacción que se revierte
(los UPDATE/DELETE no cambian nada). Cada plan se mide dos veces y se reporta la segunda (caché caliente,
como en producción).

Salida: un resumen ordenado por tiempo con banderas (recorrido completo de una tabla grande, orden en
disco, muchas filas descartadas por filtro o leídas de la tabla) y el plan completo de cada sentencia.

Una consulta nueva o cambiada de un repositorio agrega aquí su caso (regla de backend AGENTS.md §3).
"""

import re
import sys
import traceback
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.core.clock import business_today
from app.core.database import SessionLocal, engine
from app.models import AuthSession, EnrollmentStatus, FaceAttemptMetric, SessionRevocationReason, ShiftRequestStatus
from app.repositories.api_key_repository import ApiKeyRepository, find_by_hash
from app.repositories.attendance_repository import AttendanceRepository, close_missed_checkouts
from app.repositories.calendar_repository import CalendarRepository
from app.repositories.company_repository import CompanyRepository
from app.repositories.department_repository import DepartmentRepository
from app.repositories.employee_repository import EmployeeRepository
from app.repositories.enrollment_repository import FaceEnrollmentRepository
from app.repositories.error_report_repository import ErrorReportRepository
from app.repositories.face_repository import FaceEmbeddingRepository
from app.repositories.face_security_repository import FaceSecurityRepository
from app.repositories.maintenance_repository import delete_batch
from app.repositories.qr_repository import EmployeeQrRepository
from app.repositories.session_repository import SessionRepository
from app.repositories.shift_repository import ShiftRepository
from app.repositories.user_repository import UserRepository
from app.repositories.validator_device_repository import ValidatorDeviceRepository
from app.repositories.validator_repository import ValidatorRepository
from app.repositories.verification_repository import VerificationLogRepository
from app.services.attempt_guard import FACE_METHODS, MATCH_FAILURES
from app.services.face_service import SECURITY_REASONS
from app.services.maintenance_service import PURGES

#: Empresa grande (20 000 empleados, ids 1..20000) y una de 400 (seed.sql).
BIG = 1
SMALL = 57
MODEL = "sface+facenet"
NOW = datetime.now(UTC)
TODAY = business_today()
#: Tablas que crecen con el uso: un recorrido completo de cualquiera de ellas en una ruta es una bandera.
BIG_TABLES = (
    "verification_logs",
    "work_sessions",
    "attendance_events",
    "work_breaks",
    "employees",
    "users",
    "face_embeddings",
    "face_enrollments",
    "employee_absences",
    "face_attempt_metrics",
    "employee_qr_codes",
    "capture_fingerprints",
    "auth_sessions",
    "shift_assignments",
    "shift_assignment_sites",
    "error_occurrences",
    "remembered_accounts",
    "employee_workdays",
)

type Case = tuple[str, Callable[[Session], object]]


def ids(company: int, count: int) -> list[int]:
    """Los primeros `count` empleados de la empresa (seed.sql los numera en bloques por empresa)."""
    start = 1 if company == BIG else 20001 + (company - 2) * 400
    return list(range(start, start + count))


def a_session(db: Session) -> str:
    return str(db.scalar(select(AuthSession.id).where(AuthSession.revoked_at.is_(None)).limit(1)))


def auth_cases() -> list[Case]:
    users = UserRepository
    return [
        ("auth.session.get", lambda db: SessionRepository(db).get(a_session(db))),
        ("auth.session.active_for_user", lambda db: SessionRepository(db).active_for_user(77, NOW)),
        ("auth.session.page_active", lambda db: SessionRepository(db).page_active(77, NOW, offset=0, limit=10)),
        (
            "auth.session.revoke_all",
            lambda db: SessionRepository(db).revoke_all(77, NOW, SessionRevocationReason.LOGOUT),
        ),
        (
            "auth.session.revoke_company",
            lambda db: SessionRepository(db).revoke_company(SMALL, NOW, SessionRevocationReason.COMPANY_DEACTIVATED),
        ),
        ("auth.user.get_by_email", lambda db: users(db).get_by_email("empleado4242@correo.mx")),
        ("auth.user.emails_by_ids", lambda db: users(db).emails_by_ids(range(1, 51))),
    ]


def employee_cases() -> list[Case]:
    big = EmployeeRepository
    return [
        ("employees.search", lambda db: big(db, BIG).search(search=None, active=None, offset=0, limit=10)),
        ("employees.search.page500", lambda db: big(db, BIG).search(search=None, active=True, offset=5000, limit=10)),
        (
            "employees.search.term",
            lambda db: big(db, BIG).search(search="garcía ruiz", active=None, offset=0, limit=10),
        ),
        ("employees.search.short", lambda db: big(db, BIG).search(search="lu", active=None, offset=0, limit=10)),
        (
            "employees.search.department",
            lambda db: big(db, BIG).search(search=None, active=None, offset=0, limit=10, department_id=3),
        ),
        ("employees.ids", lambda db: big(db, BIG).ids(search=None, active=True, department_id=None, limit=500)),
        ("employees.shared_accounts", lambda db: big(db, BIG).shared_accounts(set(range(1, 51)))),
        ("employees.count", lambda db: big(db, BIG).count()),
        ("employees.number_exists", lambda db: big(db, BIG).number_exists("E0004242")),
        ("employees.lock_many.500", lambda db: big(db, BIG).lock_many(ids(BIG, 500))),
        ("employees.by_ids.50", lambda db: big(db, BIG).by_ids(set(ids(BIG, 50)))),
        ("employees.request_reenrollment.small", lambda db: big(db, SMALL).request_reenrollment("x")),
    ]


def attendance_cases() -> list[Case]:
    repo = AttendanceRepository
    start = datetime.combine(TODAY, datetime.min.time(), UTC) + timedelta(hours=13)
    week = TODAY - timedelta(days=6)

    def history(db: Session, **filters: Any) -> object:
        options = {"employee_id": None, "start": None, "end": None, "status": None} | filters
        return repo(db, BIG).history(**options, offset=0, limit=10)

    return [
        ("attendance.open_session.lock", lambda db: repo(db, BIG).open_session(4, lock=True)),
        ("attendance.session_at", lambda db: repo(db, BIG).session_at(4, start)),
        ("attendance.history", lambda db: history(db)),
        ("attendance.history.week", lambda db: history(db, start=week, end=TODAY)),
        ("attendance.history.status", lambda db: history(db, status="MISSED_CHECKOUT")),
        ("attendance.history.employee", lambda db: history(db, employee_id=4)),
        ("attendance.sessions_on.page", lambda db: repo(db, BIG).sessions_on(ids(BIG, 50), TODAY)),
        ("attendance.day_counts", lambda db: repo(db, BIG).day_counts(TODAY)),
        ("attendance.breaks_of.page", lambda db: repo(db, BIG).breaks_of(range(1, 1001, 20))),
        ("attendance.last_located_event", lambda db: repo(db, BIG).last_located_event(4)),
        ("attendance.events_of", lambda db: repo(db, BIG).events_of(4)),
        ("attendance.close_missed_checkouts", lambda db: close_missed_checkouts(db, NOW)),
    ]


def shift_cases() -> list[Case]:
    repo = ShiftRepository
    return [
        ("shifts.assigned_on", lambda db: repo(db, BIG).assigned_on(TODAY, search=None, offset=0, limit=10)),
        ("shifts.assigned_on.term", lambda db: repo(db, BIG).assigned_on(TODAY, search="lu", offset=0, limit=10)),
        ("shifts.assigned_off_on", lambda db: repo(db, BIG).assigned_off_on(TODAY, holiday=False)),
        ("shifts.assigned_off_on.holiday", lambda db: repo(db, BIG).assigned_off_on(TODAY, holiday=True)),
        ("shifts.assignments_on", lambda db: repo(db, BIG).assignments_on(4, [TODAY - timedelta(days=1), TODAY])),
        ("shifts.current_assignments.page", lambda db: repo(db, BIG).current_assignments(ids(BIG, 50), TODAY)),
        ("shifts.assignments_from.500", lambda db: repo(db, BIG).assignments_from(ids(BIG, 500), TODAY)),
        ("shifts.employees_with_assignments.500", lambda db: repo(db, BIG).employees_with_assignments(ids(BIG, 500))),
        ("shifts.close_before.500", lambda db: repo(db, BIG).close_before(ids(BIG, 500), TODAY + timedelta(days=2))),
        ("shifts.employees_per_shift", lambda db: repo(db, BIG).employees_per_shift([1, 2, 3], TODAY)),
        ("shifts.employees_per_site", lambda db: repo(db, BIG).employees_per_site([1, 2, 3, 4, 5], TODAY)),
        ("shifts.shift_in_use", lambda db: repo(db, BIG).shift_in_use(2)),
        ("shifts.site_in_use", lambda db: repo(db, BIG).site_in_use(2)),
        ("shifts.sites_of.page", lambda db: repo(db, BIG).sites_of(ids(BIG, 50))),
        ("shifts.assignments_of", lambda db: repo(db, BIG).assignments_of(3, offset=0, limit=10)),
        (
            "shifts.requests.pending",
            lambda db: repo(db, BIG).requests(status=ShiftRequestStatus.PENDING, employee_id=None, offset=0, limit=10),
        ),
        ("shifts.requests.all", lambda db: repo(db, BIG).requests(status=None, employee_id=None, offset=0, limit=10)),
        ("shifts.pending_requests", lambda db: repo(db, BIG).pending_requests()),
        ("shifts.has_pending_request", lambda db: repo(db, BIG).has_pending_request(8)),
    ]


def calendar_cases() -> list[Case]:
    repo = CalendarRepository
    year = (date(TODAY.year, 1, 1), date(TODAY.year, 12, 31))

    def absences(db: Session, **filters: Any) -> object:
        options = {"employee_id": None, "type_code": None, "status": None, "start": None, "end": None} | filters
        return repo(db, BIG).absences(**options, offset=0, limit=10)

    return [
        ("calendar.holidays.year", lambda db: repo(db, BIG).holidays(*year, offset=0, limit=10)),
        ("calendar.holiday_names", lambda db: repo(db, BIG).holiday_names(TODAY, TODAY)),
        ("calendar.absences", lambda db: absences(db)),
        ("calendar.absences.pending", lambda db: absences(db, status="PENDING")),
        ("calendar.absences.approved", lambda db: absences(db, status="APPROVED")),
        ("calendar.absences.month", lambda db: absences(db, start=TODAY - timedelta(days=30), end=TODAY)),
        ("calendar.absences.type", lambda db: absences(db, type_code="SICK_LEAVE")),
        ("calendar.absences.employee", lambda db: absences(db, employee_id=4)),
        ("calendar.approved_between.page", lambda db: repo(db, BIG).approved_between(ids(BIG, 50), TODAY, TODAY)),
        (
            "calendar.active_overlapping.500",
            lambda db: repo(db, BIG).active_overlapping(ids(BIG, 500), TODAY, TODAY + timedelta(days=14)),
        ),
        ("calendar.pending_absences", lambda db: repo(db, BIG).pending_absences()),
        (
            "calendar.workdays",
            lambda db: repo(db, BIG).workdays(employee_id=None, start=None, end=None, offset=0, limit=10),
        ),
        ("calendar.workdays_between.page", lambda db: repo(db, BIG).workdays_between(ids(BIG, 50), TODAY, TODAY)),
    ]


def log_cases() -> list[Case]:
    logs = VerificationLogRepository

    def company(db: Session, company_id: int = BIG, **filters: Any) -> object:
        options = {"since": None, "until": None, "employee_id": None, "success": None} | filters
        return logs(db).page_for_company(company_id, **options, offset=0, limit=10)

    def feed(db: Session, after_id: int | None) -> object:
        options = {"until": None, "employee_id": None, "success": None}
        return logs(db).feed_for_company(BIG, after_id=after_id, settled_before=NOW, **options, limit=500)

    def failures(db: Session, *, employee_id: int | None, actor_id: int | None, window: timedelta) -> object:
        reasons = MATCH_FAILURES if employee_id else SECURITY_REASONS
        return logs(db).recent_failures(
            employee_id=employee_id,
            actor_id=actor_id,
            methods=FACE_METHODS,
            reasons=reasons,
            since=NOW - window,
            limit=5,
        )

    month = {"since": NOW - timedelta(days=300), "until": NOW - timedelta(days=270)}
    return [
        ("logs.page_for_employee", lambda db: logs(db).page_for_employee(4242, offset=0, limit=10)),
        ("logs.page_for_actor", lambda db: logs(db).page_for_actor(110001, offset=0, limit=10)),
        ("logs.page_for_company", lambda db: company(db)),
        ("logs.page_for_company.week", lambda db: company(db, since=NOW - timedelta(days=7), until=NOW)),
        ("logs.page_for_company.old_month", lambda db: company(db, **month)),
        ("logs.page_for_company.failures", lambda db: company(db, success=False)),
        ("logs.page_for_company.employee", lambda db: company(db, SMALL, employee_id=ids(SMALL, 1)[0])),
        ("logs.feed.start", lambda db: feed(db, None)),
        ("logs.feed.cursor", lambda db: feed(db, 4_000_000)),
        (
            "logs.recent_failures.employee",
            lambda db: failures(db, employee_id=4242, actor_id=None, window=timedelta(minutes=15)),
        ),
        (
            "logs.recent_failures.validator",
            lambda db: failures(db, employee_id=None, actor_id=110001, window=timedelta(days=2)),
        ),
        (
            "logs.validator_successes",
            lambda db: ValidatorRepository(db, BIG).successes_since(
                list(range(110001, 110006)), NOW - timedelta(days=1)
            ),
        ),
    ]


def face_cases() -> list[Case]:
    faces = FaceEmbeddingRepository
    security = FaceSecurityRepository
    since = NOW - timedelta(days=30)
    window = NOW - timedelta(minutes=60)
    return [
        ("face.gallery_fingerprint", lambda db: faces(db).gallery_fingerprint(BIG, MODEL)),
        ("face.gallery_fingerprint.small", lambda db: faces(db).gallery_fingerprint(SMALL, MODEL)),
        ("face.gallery_index", lambda db: faces(db).gallery_index(BIG, MODEL)),
        ("face.gallery_rows.100", lambda db: faces(db).gallery_rows(BIG, MODEL, list(range(1000, 1100)))),
        ("face.approved_without_model", lambda db: faces(db).approved_without_model(BIG, MODEL, 10)),
        # Tras un cambio de motor nadie tiene muestras del modelo nuevo (cada identificación 1:N migra 10).
        ("face.approved_without_model.new_engine", lambda db: faces(db).approved_without_model(BIG, "otro-motor", 10)),
        ("face.list_active", lambda db: faces(db).list_active(4242, MODEL)),
        ("face.sample_stats.page", lambda db: faces(db).sample_stats(ids(BIG, 50))),
        ("face.learning_totals", lambda db: faces(db).learning_totals(BIG)),
        ("face.delete_learned", lambda db: faces(db).delete_learned(4242)),
        ("face.delete_for_company.small", lambda db: faces(db).delete_for_company(SMALL)),
        (
            "enrollments.search.pending",
            lambda db: FaceEnrollmentRepository(db, BIG).search(status=EnrollmentStatus.PENDING, offset=0, limit=10),
        ),
        (
            "enrollments.search.all",
            lambda db: FaceEnrollmentRepository(db, BIG).search(status=None, offset=0, limit=10),
        ),
        ("enrollments.latest_for_employee", lambda db: FaceEnrollmentRepository(db, BIG).latest_for_employee(4242)),
        ("enrollments.reject_pending.small", lambda db: FaceEnrollmentRepository(db, SMALL).reject_pending("x")),
        ("face_security.attacks", lambda db: security(db).attacks(BIG, window, SECURITY_REASONS, 5)),
        (
            "face_security.success_values",
            lambda db: security(db).success_values(FaceAttemptMetric.yaw_min, since, 20000),
        ),
        (
            "face_security.attacked_companies",
            lambda db: security(db).attacked_companies(window, SECURITY_REASONS, 5, 20),
        ),
        ("qr.get_by_token_hash", lambda db: EmployeeQrRepository(db).get_by_token_hash("0" * 64)),
        ("qr.latest_issued", lambda db: EmployeeQrRepository(db).latest_issued(4242)),
        ("qr.latest_used", lambda db: EmployeeQrRepository(db).latest_used(4242)),
        ("qr.revoke_all_for_employee", lambda db: EmployeeQrRepository(db).revoke_all_for_employee(4242)),
    ]


def admin_cases() -> list[Case]:
    errors = ErrorReportRepository
    companies = CompanyRepository

    def error_page(db: Session, **filters: Any) -> object:
        options = {"status": None, "severity": None, "search": None} | filters
        return errors(db).search(**options, offset=0, limit=10)

    return [
        ("errors.search.pending", lambda db: error_page(db, status="PENDING")),
        ("errors.search", lambda db: error_page(db)),
        ("errors.search.term", lambda db: error_page(db, search="code_12")),
        ("errors.counts", lambda db: errors(db).counts()),
        ("errors.occurrences", lambda db: errors(db).occurrences(42, offset=0, limit=10)),
        (
            "errors.resolve_matching",
            lambda db: errors(db).resolve_matching(
                status="PENDING", severity=None, search=None, seen_until=NOW, admin_id=120001, now=NOW
            ),
        ),
        ("companies.search", lambda db: companies(db).search(search=None, active=None, offset=0, limit=10)),
        ("companies.search.term", lambda db: companies(db).search(search="presa 1", active=None, offset=0, limit=10)),
        ("companies.counts.page", lambda db: companies(db).counts(range(1, 11))),
        ("companies.stats", lambda db: companies(db).stats()),
        ("companies.admins_page", lambda db: companies(db).admins_page(BIG, offset=0, limit=10)),
        ("departments.search", lambda db: DepartmentRepository(db, BIG).search(search=None, offset=0, limit=10)),
        ("departments.member_counts", lambda db: DepartmentRepository(db, BIG).member_counts(range(1, 11))),
        ("departments.managers_of", lambda db: DepartmentRepository(db, BIG).managers_of(range(1, 11))),
        ("validators.page", lambda db: ValidatorRepository(db, BIG).page(offset=0, limit=10)),
        ("validator_devices.counts", lambda db: ValidatorDeviceRepository(db, BIG).counts([1, 2, 3, 4, 5])),
        ("api_keys.page", lambda db: ApiKeyRepository(db, BIG).page(offset=0, limit=10)),
        ("api_keys.find_by_hash", lambda db: find_by_hash(db, "0" * 64)),
    ]


def purge_cases() -> list[Case]:
    """Cada depuración del mantenimiento con un lote del tamaño por omisión."""

    def purge(db: Session, index: int) -> object:
        item = PURGES[index]
        return delete_batch(db, item.key, item.condition(NOW), 5000)

    return [(f"maintenance.purge.{item.name}", lambda db, i=i: purge(db, i)) for i, item in enumerate(PURGES)]


CASES: list[Case] = [
    *auth_cases(),
    *employee_cases(),
    *attendance_cases(),
    *shift_cases(),
    *calendar_cases(),
    *log_cases(),
    *face_cases(),
    *admin_cases(),
    *purge_cases(),
]

_captured: list[tuple[str, Any]] | None = None


@event.listens_for(engine, "before_cursor_execute")
def _record(_conn: object, _cursor: object, statement: str, parameters: Any, _context: object, many: bool) -> None:
    if _captured is not None and not many:
        _captured.append((statement, parameters))


TIME = re.compile(r"Execution Time: ([\d.]+) ms")


def flags_of(plan: str) -> list[str]:
    """Lo que hay que revisar en un plan: recorridos completos de tablas grandes, orden en disco, muchas
    filas descartadas por filtro o leídas de la tabla en un recorrido "solo índice"."""
    found = [f"SEQ SCAN {table}" for table in BIG_TABLES if re.search(rf"Seq Scan on {table}\b", plan)]
    if "external merge" in plan:
        found.append("ORDEN EN DISCO")
    found += [f"filtra {n} filas" for n in re.findall(r"Rows Removed by Filter: (\d+)", plan) if int(n) > 5000]
    found += [f"heap fetches {n}" for n in re.findall(r"Heap Fetches: (\d+)", plan) if int(n) > 5000]
    return found


def explain(statement: str, parameters: Any) -> tuple[str, float]:
    """El plan con caché caliente (segunda corrida) dentro de una transacción que se revierte."""
    plan = ""
    for _ in range(2):
        with engine.connect() as conn:
            transaction = conn.begin()
            try:
                # El texto es una sentencia que ya generó SQLAlchemy (no entra nada del usuario).
                rows = conn.exec_driver_sql("EXPLAIN (ANALYZE, BUFFERS) " + statement, parameters).all()
            finally:
                transaction.rollback()
        plan = "\n".join(str(row[0]) for row in rows)
    found = TIME.search(plan)
    return plan, float(found.group(1)) if found else -1.0


def run_case(name: str, call: Callable[[Session], object]) -> tuple[list[str], list[str]]:
    """(renglones del resumen, bloques del detalle) de un caso."""
    global _captured
    summary: list[str] = []
    detail: list[str] = []
    _captured = []
    with SessionLocal() as db:
        try:
            call(db)
        except Exception:  # un caso roto se reporta y los demás siguen
            detail.append(f"### {name}\nERROR\n{traceback.format_exc()}\n")
        finally:
            statements, _captured = list(_captured), None
            db.rollback()
    for index, (statement, parameters) in enumerate(statements):
        if not statement.lstrip().upper().startswith(("SELECT", "UPDATE", "DELETE", "WITH")):
            continue
        plan, ms = explain(statement, parameters)
        flags = "; ".join(flags_of(plan))
        summary.append(f"{ms:10.3f} ms  {name}[{index}]  {flags}")
        detail.append(f"### {name} [{index}]  {ms:.3f} ms  {flags}\n{statement}\n{parameters}\n{plan}\n")
    return summary, detail


def main() -> None:
    out_path = sys.argv[1]
    only = sys.argv[2] if len(sys.argv) > 2 else ""
    summary: list[str] = []
    detail: list[str] = []
    for name, call in CASES:
        if only in name:
            lines, blocks = run_case(name, call)
            summary += lines
            detail += blocks
    summary.sort(key=lambda line: -float(line.split(" ms", 1)[0]))
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("# Resumen (la sentencia más lenta primero): ms, caso[sentencia], banderas\n")
        fh.write("\n".join(summary))
        fh.write("\n\n# Detalle\n\n")
        fh.write("\n".join(detail))
    print("\n".join(summary[:40]))
    print(f"\n{len(summary)} sentencias; detalle completo en {out_path}")


if __name__ == "__main__":
    main()
