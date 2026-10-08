"""EXPLAIN (ANALYZE, BUFFERS) de las consultas REALES de los repositorios sobre la base de volumen.

Lo ejecuta `perf/db/run.sh` dentro de la imagen dev contra un PostgreSQL AISLADO sembrado con
`perf/db/seed.sql` (jamás la base de trabajo). Cada caso llama un método de un repositorio con
parámetros realistas (la empresa grande de 20 000 empleados o una de 400); se capturan las sentencias
que emite y se explica cada una con sus mismos parámetros, dentro de una transacción que se revierte
(los UPDATE/DELETE no cambian nada). Cada plan se mide dos veces y se reporta la segunda (caché caliente,
como en producción).

Salida: un resumen ordenado por tiempo con banderas (recorrido completo de una tabla grande, orden en
disco, muchas filas descartadas por filtro o leídas de la tabla) y el plan completo de cada sentencia.

Cada caso corre con su ALCANCE de seguridad por fila, como en producción (`scope_of_case`): la empresa (la grande
o la chica) o la plataforma (ADMIN, autenticación, mantenimiento). `perf/db/run.sh` conecta con el usuario de la
API (sujeto a la política); con `PERF_DB_ROLE=owner`, con el dueño (sin política) para comparar.

Una consulta nueva o cambiada de un repositorio agrega aquí su caso (regla de backend AGENTS.md §3).
"""

import hashlib
import re
import sys
import traceback
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.core.clock import business_day_bounds, business_today
from app.core.config import settings
from app.core.database import SessionLocal, engine
from app.core.row_security import PLATFORM, SCOPE_KEY, Scope, apply_scope
from app.models import AuthSession, EnrollmentStatus, FaceAttemptMetric, SessionRevocationReason, ShiftRequestStatus
from app.models.company import TaxId
from app.repositories.api_key_repository import ApiKeyRepository, find_by_hash
from app.repositories.attendance_repository import AttendanceRepository, close_missed_checkouts
from app.repositories.avatar_repository import AvatarRepository
from app.repositories.billing_repository import BillingRepository
from app.repositories.calendar_repository import CalendarRepository
from app.repositories.company_document_repository import CompanyDocumentRepository
from app.repositories.company_repository import CompanyRepository
from app.repositories.department_repository import DepartmentRepository
from app.repositories.drift_repository import DriftRepository
from app.repositories.employee_device_repository import EmployeeDeviceRepository
from app.repositories.employee_repository import EmployeeRepository
from app.repositories.enrollment_draft_repository import FaceEnrollmentDraftRepository
from app.repositories.enrollment_repository import FaceEnrollmentRepository
from app.repositories.error_report_repository import ErrorReportRepository
from app.repositories.face_repository import FaceEmbeddingRepository
from app.repositories.face_security_repository import FaceSecurityRepository
from app.repositories.fraud_repository import FraudCaseRepository
from app.repositories.kiosk_repository import KioskLookup, KioskRepository
from app.repositories.maintenance_repository import delete_batch
from app.repositories.passkey_repository import PasskeyRepository
from app.repositories.performance_repository import DAYS, HOURS, MINUTES, PerformanceRepository
from app.repositories.qr_repository import EmployeeQrRepository
from app.repositories.risk_repository import (
    AttackSignatureRepository,
    CaptureTraceRepository,
    PolicyChangeRepository,
    RiskAssessmentRepository,
    RiskSignalStatRepository,
)
from app.repositories.session_repository import SessionRepository
from app.repositories.shift_repository import ShiftRepository
from app.repositories.storage_repository import StorageRepository
from app.repositories.usage_repository import UsageRepository
from app.repositories.user_repository import UserRepository
from app.repositories.validator_device_repository import ValidatorDeviceRepository
from app.repositories.validator_repository import ValidatorRepository
from app.repositories.verification_repository import VerificationLogRepository
from app.services import drift_service
from app.services.attempt_guard import FACE_METHODS, MATCH_FAILURES
from app.services.face_service import SECURITY_REASONS
from app.services.image_storage import STORED_IMAGES
from app.services.maintenance_service import PURGES, SOFT_DELETE_PURGES

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
    "error_occurrences",
    "remembered_accounts",
    "employee_workdays",
    "employee_status_events",
    "validator_status_events",
    "usage_routes",
    "usage_users",
    "usage_daily",
    "storage_snapshots",
    "headcount_days",
    "perf_minutes",
    "perf_hours",
    "perf_days",
    "risk_assessments",
    "capture_traces",
    "fraud_cases",
    "fraud_case_attempts",
    "fraud_case_events",
    "fraud_evidence",
    "employee_devices",
    "enrollment_voice_answers",
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
        ("employees.number_exists", lambda db: big(db, BIG).unique_exists("employee_number", "E0004242")),
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
        # La ventana del viaje imposible con la velocidad máxima por omisión de la política (200 km/h).
        ("attendance.last_located_event", lambda db: repo(db, BIG).last_located_event(4, NOW - timedelta(hours=100))),
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
        ("shifts.shifts_using_site", lambda db: repo(db, BIG).shifts_using_site(2, limit=6)),
        ("shifts.site_has_records", lambda db: repo(db, BIG).site_has_records(2)),
        ("shifts.site_has_records.unused", lambda db: repo(db, BIG).site_has_records(999_999)),
        ("shifts.sites_of_shifts.page", lambda db: repo(db, BIG).sites_of_shifts([1, 2, 3])),
        ("shifts.shift_site_ids", lambda db: repo(db, BIG).shift_site_ids(1)),
        ("shifts.shift_of", lambda db: repo(db, BIG).shift_of(4)),
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

    def failures(
        db: Session,
        *,
        employee_id: int | None,
        actor_id: int | None,
        window: timedelta,
        device_hash: str | None = None,
    ) -> object:
        reasons = MATCH_FAILURES if employee_id else SECURITY_REASONS
        return logs(db).recent_failures(
            employee_id=employee_id,
            actor_id=actor_id,
            device_hash=device_hash,
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
            # El bloqueo del 1:N de la API pública por dispositivo
            # (índice parcial `ix_verification_logs_device_created`).
            "logs.recent_failures.device",
            lambda db: failures(
                db, employee_id=None, actor_id=None, window=timedelta(minutes=15), device_hash="a" * 64
            ),
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
        # Antifraude 2a: el protocolo de captura en Seguridad facial (una lectura acotada por (created_at, id)).
        ("face_security.protocol_values", lambda db: security(db).protocol_values(since, 20000)),
        ("qr.get_by_token_hash", lambda db: EmployeeQrRepository(db).get_by_token_hash("0" * 64, BIG)),
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
        # Identificador fiscal (migración 0074): alta, edición, restauración y validación en vivo, por el único parcial.
        ("companies.tax_id_exists", lambda db: companies(db).tax_id_exists(TaxId("MX", "MX_RFC", "EMP000042AB2"))),
        (
            "companies.search.tax_id",
            lambda db: companies(db).search(search="emp000042", active=None, offset=0, limit=10),
        ),
        ("companies.counts.page", lambda db: companies(db).counts(range(1, 11))),
        ("companies.stats", lambda db: companies(db).stats()),
        ("companies.admins_page", lambda db: companies(db).admins_page(BIG, offset=0, limit=10)),
        ("departments.search", lambda db: DepartmentRepository(db, BIG).search(search=None, offset=0, limit=10)),
        ("departments.member_counts", lambda db: DepartmentRepository(db, BIG).member_counts(range(1, 11))),
        ("departments.managers_of", lambda db: DepartmentRepository(db, BIG).managers_of(range(1, 11))),
        ("validators.page", lambda db: ValidatorRepository(db, BIG).page(offset=0, limit=10)),
        # El uso del límite ("N de M") y el candado de cada alta o activación: por el índice (company_id, role).
        ("validators.active_count", lambda db: ValidatorRepository(db, BIG).active_count()),
        ("validator_devices.counts", lambda db: ValidatorDeviceRepository(db, BIG).counts([1, 2, 3, 4, 5])),
        ("api_keys.page", lambda db: ApiKeyRepository(db, BIG).page(offset=0, limit=10)),
        ("api_keys.find_by_hash", lambda db: find_by_hash(db, "0" * 64)),
    ]


def purge_cases() -> list[Case]:
    """Cada depuración del mantenimiento con un lote del tamaño por omisión (las de «Eliminados», con el suyo)."""

    def purge(db: Session, index: int) -> object:
        item = PURGES[index]
        return delete_batch(db, item.key, item.condition(NOW), 5000)

    def soft_purge(db: Session, index: int) -> object:
        item = SOFT_DELETE_PURGES[index]
        return delete_batch(
            db, item.key, item.condition(NOW), settings.SOFT_DELETE_PURGE_BATCH_SIZE, objects=item.objects
        )

    return [
        *((f"maintenance.purge.{item.name}", lambda db, i=i: purge(db, i)) for i, item in enumerate(PURGES)),
        *(
            (f"maintenance.soft_purge.{item.name}", lambda db, i=i: soft_purge(db, i))
            for i, item in enumerate(SOFT_DELETE_PURGES)
        ),
    ]


def trash_cases() -> list[Case]:
    """La papelera («Eliminados», borrado lógico) de cada listado y lo que eliminar y restaurar consultan."""
    year = (date(TODAY.year, 1, 1), date(TODAY.year, 12, 31))
    return [
        (
            "trash.employees",
            lambda db: EmployeeRepository(db, BIG).search(search=None, active=None, offset=0, limit=10, deleted=True),
        ),
        (
            "trash.employees.term",
            lambda db: EmployeeRepository(db, BIG).search(
                search="garcía", active=None, offset=0, limit=10, deleted=True
            ),
        ),
        ("trash.employees.has_employment", lambda db: EmployeeRepository(db, BIG).has_employment(4242)),
        ("trash.employees.detach_deleted_from", lambda db: EmployeeRepository(db, BIG).detach_deleted_from(3)),
        ("trash.validators", lambda db: ValidatorRepository(db, BIG).page(offset=0, limit=10, deleted=True)),
        (
            "trash.departments",
            lambda db: DepartmentRepository(db, BIG).search(search=None, offset=0, limit=10, deleted=True),
        ),
        (
            "trash.sites",
            lambda db: ShiftRepository(db, BIG).sites(search=None, active=None, offset=0, limit=10, deleted=True),
        ),
        (
            "trash.shifts",
            lambda db: ShiftRepository(db, BIG).shifts(search=None, active=None, offset=0, limit=10, deleted=True),
        ),
        ("trash.shifts.deleted_sites_of", lambda db: ShiftRepository(db, BIG).deleted_sites_of(1)),
        (
            "trash.assignments_of",
            lambda db: ShiftRepository(db, BIG).assignments_of(50, offset=0, limit=10, deleted=True),
        ),
        (
            "trash.holidays",
            lambda db: CalendarRepository(db, BIG).holidays(*year, offset=0, limit=10, deleted=True),
        ),
        (
            "trash.workdays",
            lambda db: CalendarRepository(db, BIG).workdays(
                employee_id=None, start=None, end=None, offset=0, limit=10, deleted=True
            ),
        ),
        ("trash.erase_employee", lambda db: FaceEmbeddingRepository(db).erase_employee(BIG, 4242)),
        (
            "companies.trash",
            lambda db: CompanyRepository(db).search(search=None, active=None, offset=0, limit=10, deleted=True),
        ),
        ("auth.user.deleted_with", lambda db: UserRepository(db).deleted_with(SMALL, NOW)),
    ]


def billing_cases() -> list[Case]:
    """Cobranza: lo que leen sus pantallas y lo que hace el mantenimiento (cargos, suspensión, plantilla)."""
    billing = BillingRepository
    start, end = business_day_bounds(TODAY - timedelta(days=1))

    def companies(db: Session, **filters: Any) -> object:
        options = {"search": None, "suspended": None, "overdue": None} | filters
        return billing(db).company_page(TODAY, **options, offset=0, limit=10)

    return [
        ("billing.overview", lambda db: billing(db).overview(TODAY, TODAY.replace(day=1))),
        ("billing.company_page", lambda db: companies(db)),
        ("billing.company_page.overdue", lambda db: companies(db, overdue=True)),
        ("billing.company_page.term", lambda db: companies(db, search="presa 1")),
        ("billing.balances.page", lambda db: billing(db).balances(range(1, 11), TODAY)),
        ("billing.plans_by_ids", lambda db: billing(db).plans_by_ids(range(1, 11))),
        ("billing.charges_page", lambda db: billing(db).charges_page(BIG, None, offset=0, limit=10)),
        ("billing.charges_page.open", lambda db: billing(db).charges_page(BIG, "OPEN", offset=0, limit=10)),
        ("billing.payments_page", lambda db: billing(db).payments_page(BIG, None, offset=0, limit=10)),
        ("billing.statement_page", lambda db: billing(db).statement_page(BIG, offset=0, limit=10)),
        ("billing.open_charges", lambda db: billing(db).open_charges(BIG)),
        ("billing.payments_with_credit", lambda db: billing(db).payments_with_credit(10)),
        ("billing.overdue_companies", lambda db: billing(db).overdue_companies(TODAY)),
        ("billing.plans_due", lambda db: billing(db).plans_due(TODAY - timedelta(days=1), 500)),
        ("billing.plans_to_forecast", lambda db: billing(db).plans_to_forecast(start, 2000)),
        ("billing.headcount.period", lambda db: billing(db).headcount(BIG, TODAY - timedelta(days=31), TODAY)),
        ("billing.active_headcount", lambda db: billing(db).active_headcount(range(1, 51))),
        ("billing.closed_days", lambda db: billing(db).closed_days(TODAY - timedelta(days=400), TODAY)),
        ("billing.close_headcount", lambda db: billing(db).close_headcount(TODAY - timedelta(days=1), start, end)),
        ("billing.last_cut", lambda db: billing(db).last_cut(BIG)),
        # Moneda fija de una empresa (cambiar el plan o registrar un pago): la primera fila por su índice.
        ("billing.movement_currency", lambda db: billing(db).movement_currency(BIG)),
    ]


def usage_cases() -> list[Case]:
    """Consumo: la pantalla del ADMIN (un mes de toda la plataforma y de una empresa) y la foto diaria."""
    usage = UsageRepository
    month = TODAY - timedelta(days=30)

    def company_page(db: Session, sort: str) -> object:
        return usage(db).company_usage_page(month, TODAY, search=None, sort=sort, offset=0, limit=10)

    return [
        ("usage.platform_totals", lambda db: usage(db).platform_totals(month, TODAY)),
        ("usage.platform_days", lambda db: usage(db).platform_days(month, TODAY)),
        ("usage.companies_with_traffic", lambda db: usage(db).companies_with_traffic(month, TODAY)),
        ("usage.company_page.requests", lambda db: company_page(db, "requests")),
        ("usage.company_page.storage", lambda db: company_page(db, "storage")),
        ("usage.platform_storage", lambda db: usage(db).platform_storage()),
        ("usage.company_totals", lambda db: usage(db).company_totals(BIG, month, TODAY)),
        ("usage.company_days", lambda db: usage(db).company_days(BIG, month, TODAY)),
        ("usage.routes_page", lambda db: usage(db).routes_page(BIG, month, TODAY, offset=0, limit=10)),
        ("usage.users_page", lambda db: usage(db).users_page(BIG, month, TODAY, offset=0, limit=10)),
        ("usage.company_storage", lambda db: usage(db).company_storage(BIG)),
        ("usage.accounts", lambda db: usage(db).accounts(BIG, list(range(1, 11)))),
        ("usage.table_counts.daily", lambda db: usage(db).table_counts()),
    ]


def storage_cases() -> list[Case]:
    """Bucket de imágenes: la cola de borrado del mantenimiento, liberar las fotos de un empleado al
    borrarlo y los conteos con tope del estado del ADMIN."""
    storage = StorageRepository
    return [
        ("storage.deletions", lambda db: storage(db).deletions("", 200)),
        ("storage.count_deletions", lambda db: storage(db).count_deletions(10_000)),
        ("storage.status", lambda db: storage(db).status("delete")),
        # Los conteos con tope de TODOS los tipos de `STORED_IMAGES` en una sola consulta (un subconteo por tipo).
        ("storage.count_stored", lambda db: storage(db).count_stored(STORED_IMAGES, 10_000)),
        (
            "enrollments.reject_pending.employee",
            lambda db: FaceEnrollmentRepository(db, BIG).reject_pending("x", 4241),
        ),
    ]


def avatar_cases() -> list[Case]:
    """Foto de perfil: la propia, la de un empleado vista por su empresa (permiso y referencia en una consulta), la
    de una cuenta vista por la plataforma y reemplazarla (bloquear la cuenta y borrar la referencia; encolar la
    anterior es un INSERT ... SELECT por la llave primaria, que aquí no se explica)."""
    avatars = AvatarRepository
    employee = ids(BIG, 2)[1]  # su cuenta (en seed.sql user_id = id) tiene foto: la tienen las pares
    return [
        ("avatars.visible.self", lambda db: avatars(db).visible(employee, 96)),
        ("avatars.visible.company", lambda db: avatars(db).visible(employee, 96, company_id=BIG)),
        ("avatars.visible.platform", lambda db: avatars(db).visible(100_000 + BIG, 512, staff_only=True)),
        ("avatars.replace.lock", lambda db: avatars(db).lock_person(employee)),
        ("avatars.replace.remove", lambda db: avatars(db).remove(employee)),
    ]


def performance_cases() -> list[Case]:
    """Rendimiento (pantalla del ADMIN y mantenimiento): cada periodo lee su grano (1 h y 6 h por minuto, 24 h y 7 días
    por hora, 90 días por día) por la llave primaria `(kind, <tiempo>, name)`; la bandeja de alertas por su índice."""
    perf = PerformanceRepository
    hour, six, day, week = (NOW - timedelta(hours=n) for n in (1, 6, 24, 168))
    route = "GET /api/route/7"
    current_hour = NOW.replace(minute=0, second=0, microsecond=0)
    start, end = business_day_bounds(TODAY)
    screens, recent = ["/screen/1", "/screen/2"], timedelta(minutes=10)

    def page(db: Session, grain: Any, kind: str, since: Any, sort: str, search: str | None = None) -> object:
        return perf(db).metrics_page(grain, kind, since, NOW, sort=sort, search=search, offset=0, limit=10)

    return [
        ("performance.series.minutes_6h", lambda db: perf(db).series(MINUTES, ("HTTP",), six, NOW)),
        ("performance.series.hours_7d", lambda db: perf(db).series(HOURS, ("HTTP",), week, NOW)),
        ("performance.series.days_90d", lambda db: perf(db).series(DAYS, ("HTTP",), TODAY - timedelta(days=89), TODAY)),
        ("performance.series.route_1h", lambda db: perf(db).series(MINUTES, ("HTTP",), hour, NOW, name=route)),
        ("performance.metrics.p95_1h", lambda db: page(db, MINUTES, "HTTP", hour, "p95")),
        ("performance.metrics.impact_24h", lambda db: page(db, HOURS, "HTTP", day, "impact")),
        ("performance.metrics.functions_7d", lambda db: page(db, HOURS, "FUNCTION", week, "mean")),
        ("performance.metrics.search_24h", lambda db: page(db, HOURS, "HTTP", day, "count", "route/1")),
        ("performance.screens_page", lambda db: perf(db).screens_page(MINUTES, six, NOW, offset=0, limit=10)),
        ("performance.screen_metrics", lambda db: perf(db).screen_metrics(HOURS, screens, week, NOW)),
        ("performance.alerts_page", lambda db: perf(db).alerts_page(status="OPEN", search=None, offset=0, limit=10)),
        ("performance.alerts_page.all", lambda db: perf(db).alerts_page(status=None, search=None, offset=0, limit=10)),
        ("performance.alert_counts", lambda db: perf(db).alert_counts()),
        ("performance.latest_open", lambda db: perf(db).latest_open()),
        ("performance.route_totals", lambda db: perf(db).route_totals(HOURS, route, day, NOW)),
        ("performance.error_of_trace", lambda db: perf(db).error_report_of_trace("t1", NOW - recent, NOW)),
        ("performance.rollup_hour", lambda db: perf(db).rollup_hour(current_hour)),
        ("performance.rollup_day", lambda db: perf(db).rollup_day(TODAY, start, end)),
    ]


def antifraud_cases() -> list[Case]:
    """Antifraude (migración 0062): el reenvío perceptual en CADA verificación 1:1 (las huellas recientes del empleado),
    la carga de las firmas de ataque (cada proceso, cada ATTACK_SIGNATURE_CACHE_SECONDS), la simulación de la política
    (las decisiones de 30 días de una empresa), la bandeja y el detalle de los casos (ADMIN), los registros "en
    revisión" de la empresa y las depuraciones de la evidencia y de los casos decididos."""
    replay_since = NOW - timedelta(days=settings.FACE_REPLAY_RETENTION_DAYS)
    simulation_since = NOW - timedelta(days=settings.RISK_SIMULATION_DAYS)
    fraud = FraudCaseRepository

    def cases_page(db: Session, **filters: Any) -> object:
        options = {"statuses": None, "company_id": None, "kind": None} | filters
        return fraud(db).search(**options, offset=0, limit=10)

    def review_history(db: Session) -> object:
        options = {"employee_id": None, "start": None, "end": None, "status": None, "in_review": True}
        return AttendanceRepository(db, BIG).history(**options, offset=0, limit=10)

    return [
        (
            "antifraud.capture_traces.recent",
            lambda db: CaptureTraceRepository(db, BIG).recent(
                4242, replay_since, settings.FACE_PERCEPTUAL_MAX_CAPTURES
            ),
        ),
        (
            "antifraud.signatures.active",
            lambda db: AttackSignatureRepository(db).active(NOW, settings.ATTACK_SIGNATURE_MAX_LOADED),
        ),
        (
            "antifraud.signatures.companies_blocking",
            lambda db: AttackSignatureRepository(db).companies_blocking("CAPTURE_PHASH", ["0" * 16 + ":" + "0" * 16]),
        ),
        (
            "antifraud.assessments.window",
            lambda db: RiskAssessmentRepository(db).window(
                BIG, simulation_since, settings.RISK_SIMULATION_MAX_ATTEMPTS
            ),
        ),
        ("antifraud.cases.pending", lambda db: cases_page(db, statuses=["OPEN", "IN_REVIEW"])),
        ("antifraud.cases.all", lambda db: cases_page(db)),
        ("antifraud.cases.company", lambda db: cases_page(db, company_id=BIG)),
        ("antifraud.cases.active_count", lambda db: fraud(db).active_count(settings.FRAUD_CASE_MAX_ATTEMPTS * 20)),
        ("antifraud.cases.attempts_of", lambda db: fraud(db).attempts_of(4242, settings.FRAUD_CASE_MAX_ATTEMPTS)),
        ("antifraud.cases.events_of", lambda db: fraud(db).events_of(4242, 50)),
        ("antifraud.cases.evidence_of", lambda db: fraud(db).evidence_of(4242, settings.FRAUD_EVIDENCE_MAX_PER_CASE)),
        ("antifraud.cases.expired_evidence", lambda db: fraud(db).expired_evidence(NOW - timedelta(days=90), 1000)),
        ("antifraud.cases.stale", lambda db: fraud(db).stale_cases(NOW - timedelta(days=365), 1000)),
        (
            "antifraud.policy_changes.search",
            lambda db: PolicyChangeRepository(db, BIG).search(status=None, offset=0, limit=5),
        ),
        ("antifraud.policy_changes.pending_count", lambda db: PolicyChangeRepository(db, BIG).pending_count()),
        ("antifraud.signal_stats.of_company", lambda db: RiskSignalStatRepository(db, BIG).of_company()),
        ("antifraud.reviews.history", review_history),
        ("antifraud.reviews.pending", lambda db: AttendanceRepository(db, BIG).pending_reviews(10_000)),
    ]


def drift_cases() -> list[Case]:
    """Deriva de las señales (antifraude fase 3): lo que lee el mantenimiento al cerrar una ventana (los valores
    genuinos por señal y plataforma, los intentos y casos por empresa, las revisiones decididas), lo que lista el ADMIN
    (una ventana: señales, empresas, conteos, ventanas) y la bitácora de versiones."""
    week = date(2026, 9, 28)
    start, end = NOW - timedelta(days=14), NOW - timedelta(days=7)
    drift = DriftRepository

    return [
        (
            "drift.platform_samples",
            lambda db: drift(db).platform_samples(
                [signal.column for signal in drift_service.DRIFT_SIGNALS],
                "IOS_SAFARI",
                start,
                end,
                settings.DRIFT_MAX_SAMPLES,
            ),
        ),
        ("drift.attempts_by_company", lambda db: drift(db).attempts_by_company(start, end)),
        ("drift.cases_by_company", lambda db: drift(db).cases_by_company(start, end)),
        ("drift.reviews", lambda db: drift(db).reviews(start, end, 50_000)),
        ("drift.company_names", lambda db: drift(db).company_names(range(1, 60))),
        ("drift.weeks", lambda db: drift(db).weeks(60)),
        ("drift.has_week", lambda db: drift(db).has_week(week)),
        (
            "drift.signals_page",
            lambda db: drift(db).signals_page(week, platform=None, status=None, offset=0, limit=10),
        ),
        (
            "drift.signals_page.filtered",
            lambda db: drift(db).signals_page(week, platform="DESKTOP", status="ALERT", offset=0, limit=10),
        ),
        ("drift.companies_page", lambda db: drift(db).companies_page(week, search=None, offset=0, limit=10)),
        (
            "drift.companies_page.search",
            lambda db: drift(db).companies_page(week, search="presa 1", offset=0, limit=10),
        ),
        ("drift.counts", lambda db: drift(db).counts(week)),
        ("drift.computed_at", lambda db: drift(db).computed_at(week)),
        ("drift.latest_version", lambda db: drift(db).latest_version("risk_engine")),
        ("drift.versions", lambda db: drift(db).versions(20)),
        ("drift.changed_within", lambda db: drift(db).changed_within(("risk_engine", "face_models"), start, end)),
    ]


def passkey_cases() -> list[Case]:
    """Llaves de acceso (WebAuthn): entrar con una (la llave por su credencial, con la cuenta y sus empleos), las de
    la cuenta (Mi perfil y el tope) y el reto usado (inserción atómica)."""
    passkeys = PasskeyRepository

    return [
        ("passkeys.by_credential", lambda db: passkeys(db).by_credential("0" * 43)),
        ("passkeys.page", lambda db: passkeys(db).page(120001, offset=0, limit=10)),
        ("passkeys.for_user", lambda db: passkeys(db).for_user(120001, settings.PASSKEYS_MAX_PER_USER)),
        ("passkeys.claim_challenge", lambda db: passkeys(db).claim_challenge("f" * 64, NOW)),
        ("passkeys.touch", lambda db: passkeys(db).touch(1, 2, NOW)),
    ]


def device_key(seed: str) -> str:
    """El hash de llave que siembra seed.sql (`md5('dev-' || e) || md5(e || '-dev')`)."""
    prefix, suffix = (hashlib.md5(part.encode(), usedforsecurity=False).hexdigest() for part in seed.split(":"))
    return prefix + suffix


def device_cases() -> list[Case]:
    """Antifraude 1b (migración 0065): el dispositivo de CADA verificación o registro de un empleado (su fila y quién
    más usó la llave en la ventana de DEVICE_SHARED, y su uso en un upsert), la lista de la ficha del empleado y de Mi
    perfil, y la fila que la empresa aprueba o revoca."""
    repo = EmployeeDeviceRepository
    window = NOW - timedelta(minutes=settings.RISK_DEVICE_SHARED_WINDOW_MINUTES)
    limit = settings.RISK_DEVICE_SHARED_MIN_EMPLOYEES + 1
    known, shared = device_key("dev-4242:4242-dev"), device_key("dev-4299:4299-dev")
    name = "iPhone · Safari"
    return [
        ("devices.sightings", lambda db: repo(db, BIG).sightings(known, 4242, window, limit)),
        # El teléfono del 4299 que también usa el 4300 (seed.sql): las dos filas con la misma llave.
        ("devices.sightings.shared", lambda db: repo(db, BIG).sightings(shared, 4300, window, limit)),
        ("devices.sightings.unknown", lambda db: repo(db, BIG).sightings("f" * 64, 4242, window, limit)),
        ("devices.record.known", lambda db: repo(db, BIG).record(4242, known, name, NOW, stepped_up=True)),
        ("devices.record.new", lambda db: repo(db, BIG).record(4242, "e" * 64, name, NOW, stepped_up=False)),
        ("devices.page", lambda db: repo(db, BIG).page(4245, offset=0, limit=10)),
        ("devices.get", lambda db: repo(db, BIG).get(4242, 4242)),
    ]


def presence_cases() -> list[Case]:
    """Antifraude 2b (migración 0070): los kioscos de un sitio (su listado, su papelera y el conteo de la página de
    sitios), lo que pide su tableta (por el código de vinculación y por su id: de la plataforma, aún sin empresa), la
    última vez que se vio y ligar la sesión de un validador a su llave (una vez por sesión). La firma y la ubicación de
    cada identificación no consultan nada."""
    # El código de vinculación pendiente del kiosco 2 (sitio 1) de seed.sql: `md5('p' || sitio) || md5(sitio || 'p')`.
    pairing = "".join(hashlib.md5(part, usedforsecurity=False).hexdigest() for part in (b"p1", b"1p"))
    return [
        ("kiosks.page", lambda db: KioskRepository(db, BIG).page(3, offset=0, limit=10)),
        ("kiosks.trash", lambda db: KioskRepository(db, BIG).page(3, offset=0, limit=10, deleted=True)),
        ("kiosks.per_site", lambda db: KioskRepository(db, BIG).per_site(range(1, 6))),
        ("kiosks.get", lambda db: KioskRepository(db, BIG).get(2, 4)),
        ("kiosks.seen", lambda db: KioskRepository(db, BIG).seen(5, NOW)),
        ("kiosks.lookup.by_pairing", lambda db: KioskLookup(db).by_pairing(pairing)),
        ("kiosks.lookup.by_id", lambda db: KioskLookup(db).by_id(4)),
        ("auth.sessions.bind_device", lambda db: SessionRepository(db).bind_device(a_session(db), "e" * 64)),
    ]


def document_cases() -> list[Case]:
    """Documentos de la empresa (migración 0075): su listado (lo vigente, el más reciente primero; también una página
    profunda) y su papelera, cada uno por id (descargar) y bloqueado (eliminar y restaurar), el conteo con tope del
    estado del bucket y los bytes de la foto diaria del almacenamiento. Su depuración está en `purge_cases`."""
    documents = CompanyDocumentRepository
    return [
        ("documents.page", lambda db: documents(db, BIG).page(offset=0, limit=10)),
        ("documents.page.deep", lambda db: documents(db, BIG).page(offset=1500, limit=50)),
        ("documents.trash", lambda db: documents(db, BIG).page(offset=0, limit=10, deleted=True)),
        ("documents.get", lambda db: documents(db, BIG).get(1500)),
        ("documents.get.lock", lambda db: documents(db, BIG).get(1500, include_deleted=True, lock=True)),
        ("usage.document_bytes", lambda db: UsageRepository(db).document_bytes()),
    ]


def voice_cases() -> list[Case]:
    """Verificación por voz y video del registro facial (migración 0079): el registro sin terminar con que trabaja la
    verificación (`get_any`) y el terminado bloqueado para decidirlo (`get`), sus respuestas en orden y una por id
    (reproducir el video), el intento fallido (una sentencia atómica), liberar los clips de un registro que se rechaza
    o reemplazar el que un empleado dejó a medias, la foto inicial de los tres pasos del registro (0083) y los bytes de
    la foto diaria del almacenamiento (el conteo con tope del estado del bucket está en `storage_cases`, con todos los
    tipos en una consulta). Sus depuraciones (videos vencidos, registros sin terminar) están en `purge_cases`. La
    marca para el revisor (`add_flag`) y lo que sale del bucket al eliminar a la persona (`release_employee_images`)
    son solo `INSERT`s (la cola del bucket lee por el índice `(company_id, employee_id)`), que este banco no mide."""
    enrollments = FaceEnrollmentRepository
    return [
        ("enrollments.get_any.unfinished", lambda db: enrollments(db, BIG).get_any(1007)),
        ("enrollments.get.lock", lambda db: enrollments(db, BIG).get(42, for_update=True)),
        ("enrollments.voice_answers", lambda db: enrollments(db, BIG).voice_answers(42)),
        ("enrollments.voice_answer", lambda db: enrollments(db, BIG).voice_answer(42, 1)),
        ("enrollments.count_failed_attempt", lambda db: enrollments(db, BIG).count_failed_attempt(1007)),
        ("enrollments.release_voice_clips", lambda db: enrollments(db, BIG).release_voice_clips(42)),
        ("enrollments.delete_unfinished", lambda db: enrollments(db, BIG).delete_unfinished(1007)),
        # Los tres pasos independientes del registro (migración 0083): la foto inicial del empleado (índice único
        # `(company_id, employee_id)`), «Repetir foto» (su objeto a la cola y la fila fuera) y el paso 2 que la toma.
        ("enrollment_drafts.of_employee", lambda db: FaceEnrollmentDraftRepository(db, BIG).of_employee(10)),
        ("enrollment_drafts.discard", lambda db: FaceEnrollmentDraftRepository(db, BIG).discard(20, NOW)),
        ("enrollment_drafts.take", lambda db: FaceEnrollmentDraftRepository(db, BIG).take(3)),
        ("usage.voice_clip_bytes", lambda db: UsageRepository(db).voice_clip_bytes()),
    ]


CASES: list[Case] = [
    *auth_cases(),
    *employee_cases(),
    *attendance_cases(),
    *shift_cases(),
    *calendar_cases(),
    *log_cases(),
    *face_cases(),
    *admin_cases(),
    *billing_cases(),
    *usage_cases(),
    *storage_cases(),
    *avatar_cases(),
    *performance_cases(),
    *antifraud_cases(),
    *device_cases(),
    *presence_cases(),
    *document_cases(),
    *voice_cases(),
    *drift_cases(),
    *passkey_cases(),
    *trash_cases(),
    *purge_cases(),
]

#: Casos del código de la plataforma (cruzan empresas a propósito): corren con el rol de la plataforma.
PLATFORM_CASES = (
    "auth.",
    "errors.",
    "companies.",
    "billing.",
    "usage.",
    "storage.count_",
    "storage.deletions",
    "storage.status",
    "avatars.visible.platform",
    "maintenance.",
    "performance.",
    "face_security.success_values",
    "face_security.attacked_companies",
    "face_security.protocol_values",
    "attendance.close_missed_checkouts",
    "api_keys.find_by_hash",
    "face.learning_totals",
    "face.delete_learned",
    "employees.shared_accounts",
    "antifraud.signatures.",
    "antifraud.cases.",
    "kiosks.lookup.",
    # Deriva de señales y llaves de acceso: datos de la plataforma (el mantenimiento y las lecturas del ADMIN; la
    # cuenta de una persona).
    "drift.",
    "passkeys.",
)
#: Casos de la empresa chica (los demás son de la grande).
SMALL_CASES = ("logs.page_for_company.employee",)


def scope_of_case(name: str) -> Scope:
    if name.startswith(PLATFORM_CASES):
        return PLATFORM
    return SMALL if name.endswith(".small") or name in SMALL_CASES else BIG


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


def explain(statement: str, parameters: Any, scope: Scope) -> tuple[str, float]:
    """El plan con caché caliente (segunda corrida) dentro de una transacción que se revierte, con el alcance del
    caso declarado como lo hace la API al empezar cada transacción."""
    plan = ""
    for _ in range(2):
        with engine.connect() as conn:
            transaction = conn.begin()
            try:
                apply_scope(conn, scope)
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
    scope = scope_of_case(name)
    with SessionLocal(info={SCOPE_KEY: scope}) as db:
        try:
            call(db)
        except Exception:  # un caso roto se reporta y los demás siguen
            detail.append(f"### {name}\nERROR\n{traceback.format_exc()}\n")
        finally:
            statements, _captured = list(_captured), None
            db.rollback()
    for index, (statement, parameters) in enumerate(statements):
        query = statement.lstrip().upper().startswith(("SELECT", "UPDATE", "DELETE", "WITH"))
        if not query or "set_config(" in statement:  # el alcance de la transacción no es una consulta que medir
            continue
        plan, ms = explain(statement, parameters, scope)
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
