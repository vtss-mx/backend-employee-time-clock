"""Mantenimiento: depura lo que vence (sesiones, retos, huellas de capturas, evidencia y casos de fraude, QR,
cuentas recordadas, contadores del límite de peticiones, muestras faciales aprendidas que dejaron de servir, errores
solucionados viejos, métricas de intentos faciales, consumo y almacenamiento viejos, rendimiento y alertas
de peticiones lentas resueltas viejas), resume el rendimiento (minutos → horas → días, `perf_rollup`), recalibra la
seguridad facial, corre las tareas de cobranza (`billing_jobs`: plantilla diaria, cargos, suspensión
por falta de pago, pronósticos y la foto del almacenamiento) y copia las imágenes cifradas al bucket
(`storage_jobs`), FUERA de las peticiones de los usuarios. Cada proceso, además, pone al día la base local de IP
(`ip_database`, DB-IP Lite: mensual, con su propio candado de archivo).

Antes cada petición borraba "de paso" lo vencido de todas las empresas: con mucha carga varias
peticiones competían por las mismas filas y se formaban filas de espera. Ahora:

- Una tarea en segundo plano lo ejecuta cada MAINTENANCE_INTERVAL_SECONDS en cada instancia, pero
  solo una a la vez trabaja (candado de transacción `pg_try_advisory_xact_lock`, ver `exclusive`); las
  demás se saltan la vuelta.
- Borra en lotes de MAINTENANCE_BATCH_SIZE filas, cada lote en su propia transacción corta.
- `python -m app.cli purge` hace lo mismo bajo demanda (o desde un cron externo).

Los datos vencidos ya no sirven aunque sigan en la tabla (cada consulta valida su vigencia): la
depuración solo libera espacio.

Lo que una persona eliminó (borrado lógico, regla 20 de la raíz) se depura igual, en `SOFT_DELETE_PURGES`: pasados
`SOFT_DELETE_RETENTION_DAYS` desde que se eliminó se borra de verdad, con lo que su borrado en cascada se lleva (un
empleado: su asistencia y su bitácora de ese año). En lotes chicos (`SOFT_DELETE_PURGE_BATCH_SIZE`) y en orden: primero
lo que otros referencian con RESTRICT se deja para después (cada condición salta lo que aún tiene referencias, así una
fila retenida nunca hace fallar el lote de las demás) y las cuentas y las empresas al final. Una empresa con movimientos
de cobranza nunca se depura (el historial fiscal se conserva; de todos modos no se puede eliminar una con cobranza).
Lo que tiene un archivo en el bucket (los documentos de la empresa) sale también de él: su `Purge` lleva la columna del
objeto (`objects`) y el mismo `DELETE ... RETURNING` lo encola para la cola de borrado (`storage_jobs`); los de una
empresa que se depura salen justo antes que ella (su borrado en cascada no avisaría al bucket).
"""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, and_, exists, or_, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import InstrumentedAttribute, Session

from app.core.config import settings
from app.core.database import engine, platform_session
from app.core.observability import observed
from app.core.partitions import PARTITIONED
from app.models import (
    AttackSignature,
    AttendanceEvent,
    AuthSession,
    CaptureFingerprint,
    CaptureTrace,
    Charge,
    Company,
    CompanyDocument,
    CompanyFraudWeekly,
    CompanyHoliday,
    DailyTask,
    Department,
    Employee,
    EmployeeDocument,
    EmployeeQr,
    EmployeeWorkday,
    EngineVersion,
    EnrollmentVoiceAnswer,
    ErrorOccurrence,
    ErrorReport,
    ErrorStatus,
    FaceAttemptMetric,
    FaceChallenge,
    FaceEmbedding,
    FaceEnrollment,
    FaceEnrollmentDraft,
    HeadcountDay,
    PasskeyChallenge,
    Payment,
    PerfDay,
    PerfHour,
    PerfMinute,
    RateLimitCounter,
    RememberedAccount,
    RiskAssessment,
    Shift,
    ShiftAssignment,
    ShiftSite,
    SignalDrift,
    SiteKiosk,
    SlowAlertStatus,
    SlowRequestAlert,
    StorageSnapshot,
    UsageDaily,
    UsageRoute,
    UsageUser,
    User,
    Validator,
    WorkSession,
    WorkSite,
)
from app.models.face_embedding import last_useful
from app.repositories import partition_repository
from app.repositories.attendance_repository import close_missed_checkouts
from app.repositories.maintenance_repository import PurgeKey, delete_batch
from app.repositories.performance_repository import ALL_KINDS
from app.services import billing_jobs, drift_service, fraud_case_service, ip_database, perf_rollup, storage_jobs
from app.services.face_security import recalibrate_if_due

logger = logging.getLogger(__name__)

#: Candado de PostgreSQL que asegura una sola depuración a la vez entre instancias.
MAINTENANCE_LOCK_KEY = 7_420_032


@dataclass(frozen=True)
class Purge:
    name: str
    #: Llave primaria (una columna, o varias en las tablas de consumo por empresa y día).
    key: PurgeKey
    condition: Callable[[datetime], ColumnElement[bool]]
    #: Columna del objeto del bucket de cada fila (una tabla de `STORED_IMAGES`): lo que se borra sale también del
    #: bucket, por la cola de borrado y en la misma transacción (`delete_batch(objects=...)`).
    objects: InstrumentedAttribute[Any] | None = None


def _days_ago(now: datetime, days: int) -> date:
    """El día de hace `days` días (retenciones de tablas por día)."""
    return (now - timedelta(days=days)).date()


def _unfinished_enrollment(now: datetime) -> ColumnElement[bool]:
    """Un registro facial que esperaba sus respuestas en video y ya no llegarán: el paso 3 se retoma otro día
    (decisión del dueño, 2026-10-07), así que un registro a medias vive `FACE_ENROLLMENT_DRAFT_HOURS`, lo mismo que la
    foto inicial; después se borra con su foto y sus respuestas. Por su índice parcial
    `ix_face_enrollments_voice_pending` (la columna booleana tal cual, nunca `IS true`: con `IS TRUE` PostgreSQL no
    deduce el predicado del índice y recorría la tabla)."""
    return and_(
        FaceEnrollment.voice_required,
        FaceEnrollment.voice_passed_at.is_(None),
        FaceEnrollment.submitted_at < now - timedelta(hours=settings.FACE_ENROLLMENT_DRAFT_HOURS),
    )


#: Qué se depura y desde cuándo (las retenciones viven en la configuración).
PURGES: tuple[Purge, ...] = (
    Purge("sesiones", AuthSession.id, lambda now: AuthSession.expires_at < now - timedelta(days=1)),
    Purge("retos de prueba de vida", FaceChallenge.id, lambda now: FaceChallenge.expires_at <= now),
    # Llaves de acceso (antifraude fase 3): los retos sellados ya usados sobran en cuanto vencen.
    Purge("retos de llaves de acceso usados", PasskeyChallenge.digest, lambda now: PasskeyChallenge.expires_at <= now),
    # Verificación por voz del registro facial (decisión del dueño, 2026-10-06): los videos se conservan cifrados
    # FACE_VIDEO_RETENTION_DAYS (como la evidencia de fraude) y salen del bucket con su fila; un registro que nunca
    # terminó sus respuestas (la persona cerró la app) se borra al doble de la vida de su sesión: primero las
    # respuestas que sí pasaron (sus clips a la cola), luego el registro con su foto (sus muestras, en cascada).
    Purge(
        "videos de la verificación por voz",
        EnrollmentVoiceAnswer.id,
        lambda now: EnrollmentVoiceAnswer.created_at < now - timedelta(days=settings.FACE_VIDEO_RETENTION_DAYS),
        objects=EnrollmentVoiceAnswer.object_name,
    ),
    Purge(
        "respuestas de registros faciales sin terminar",
        EnrollmentVoiceAnswer.id,
        lambda now: exists().where(
            FaceEnrollment.id == EnrollmentVoiceAnswer.enrollment_id,
            FaceEnrollment.company_id == EnrollmentVoiceAnswer.company_id,
            _unfinished_enrollment(now),
        ),
        objects=EnrollmentVoiceAnswer.object_name,
    ),
    Purge(
        "registros faciales sin terminar",
        FaceEnrollment.id,
        _unfinished_enrollment,
        objects=FaceEnrollment.photo_object,
    ),
    # La foto inicial del registro (paso 1 de 3, decisión del dueño 2026-10-07) vence a las FACE_ENROLLMENT_DRAFT_HOURS:
    # el borrador sale con su objeto del bucket (índice `ix_face_enrollment_drafts_expires`).
    Purge(
        "fotos iniciales del registro facial vencidas",
        FaceEnrollmentDraft.id,
        lambda now: FaceEnrollmentDraft.expires_at <= now,
        objects=FaceEnrollmentDraft.photo_object,
    ),
    Purge(
        "huellas de capturas",
        CaptureFingerprint.digest,
        lambda now: CaptureFingerprint.created_at < now - timedelta(days=settings.FACE_REPLAY_RETENTION_DAYS),
    ),
    # Antifraude (migración 0062): las huellas perceptuales duran lo que las exactas; las firmas de la lista de
    # bloqueo, lo que dice su vencimiento.
    Purge(
        "huellas perceptuales de capturas",
        CaptureTrace.id,
        lambda now: CaptureTrace.created_at < now - timedelta(days=settings.FACE_REPLAY_RETENTION_DAYS),
    ),
    Purge("firmas de ataques vencidas", AttackSignature.id, lambda now: AttackSignature.expires_at < now),
    Purge(
        "decisiones del motor de riesgo",
        RiskAssessment.id,
        lambda now: RiskAssessment.created_at < now - timedelta(days=settings.FACE_METRICS_RETENTION_DAYS),
    ),
    Purge(
        "códigos QR",
        EmployeeQr.id,
        lambda now: EmployeeQr.expires_at < now - timedelta(days=settings.QR_TOKEN_RETENTION_DAYS),
    ),
    Purge("cuentas recordadas", RememberedAccount.id, lambda now: RememberedAccount.expires_at <= now),
    # Evolución del reconocimiento facial: lo aprendido que dejó de servir deja su lugar (face_learning).
    Purge(
        "muestras faciales aprendidas sin uso",
        FaceEmbedding.id,
        lambda now: and_(
            FaceEmbedding.learned, last_useful() < now - timedelta(days=settings.FACE_LEARNING_STALE_DAYS)
        ),
    ),
    Purge(
        "ocurrencias de errores",
        ErrorOccurrence.id,
        lambda now: ErrorOccurrence.occurred_at < now - timedelta(days=settings.ERROR_OCCURRENCE_RETENTION_DAYS),
    ),
    Purge(
        "errores solucionados",
        ErrorReport.id,
        lambda now: and_(
            ErrorReport.status == ErrorStatus.RESOLVED,
            ErrorReport.last_seen_at < now - timedelta(days=settings.ERROR_RESOLVED_RETENTION_DAYS),
        ),
    ),
    Purge(
        "métricas de intentos faciales",
        FaceAttemptMetric.id,
        lambda now: FaceAttemptMetric.created_at < now - timedelta(days=settings.FACE_METRICS_RETENTION_DAYS),
    ),
    Purge(
        "límites de peticiones",
        RateLimitCounter.key,
        lambda now: RateLimitCounter.expires_at < now - timedelta(minutes=5),
    ),
    # Consumo y almacenamiento: totales por empresa y fotos por USAGE_RETENTION_DAYS; el detalle por ruta
    # y por usuario (mucho más grande) por USAGE_DETAIL_RETENTION_DAYS.
    Purge(
        "consumo diario",
        (UsageDaily.company_id, UsageDaily.day),
        lambda now: UsageDaily.day < _days_ago(now, settings.USAGE_RETENTION_DAYS),
    ),
    Purge(
        "consumo por ruta",
        (UsageRoute.company_id, UsageRoute.day, UsageRoute.route),
        lambda now: UsageRoute.day < _days_ago(now, settings.USAGE_DETAIL_RETENTION_DAYS),
    ),
    Purge(
        "consumo por usuario",
        (UsageUser.company_id, UsageUser.day, UsageUser.user_id),
        lambda now: UsageUser.day < _days_ago(now, settings.USAGE_DETAIL_RETENTION_DAYS),
    ),
    Purge(
        "fotos de almacenamiento",
        (StorageSnapshot.company_id, StorageSnapshot.day, StorageSnapshot.category),
        lambda now: StorageSnapshot.day < _days_ago(now, settings.USAGE_RETENTION_DAYS),
    ),
    # Rendimiento: cada grano con su retención (los minutos, además, salen con su partición mensual). La condición
    # recorre la llave primaria tipo por tipo (`kind IN (...)`: empieza por el tipo).
    Purge(
        "rendimiento por minuto",
        (PerfMinute.kind, PerfMinute.minute, PerfMinute.name),
        lambda now: and_(
            PerfMinute.kind.in_(ALL_KINDS),
            PerfMinute.minute < now - timedelta(days=settings.PERF_MINUTE_RETENTION_DAYS),
        ),
    ),
    Purge(
        "rendimiento por hora",
        (PerfHour.kind, PerfHour.hour, PerfHour.name),
        lambda now: and_(
            PerfHour.kind.in_(ALL_KINDS), PerfHour.hour < now - timedelta(days=settings.PERF_HOUR_RETENTION_DAYS)
        ),
    ),
    Purge(
        "rendimiento por día",
        (PerfDay.kind, PerfDay.day, PerfDay.name),
        lambda now: and_(PerfDay.kind.in_(ALL_KINDS), PerfDay.day < _days_ago(now, settings.PERF_DAY_RETENTION_DAYS)),
    ),
    # Deriva de señales (antifraude fase 3): las ventanas y los cambios de versión más viejos que su retención.
    Purge(
        "deriva de señales",
        SignalDrift.id,
        lambda now: SignalDrift.week_start < _days_ago(now, settings.DRIFT_RETENTION_DAYS),
    ),
    Purge(
        "deriva por empresa",
        CompanyFraudWeekly.id,
        lambda now: CompanyFraudWeekly.week_start < _days_ago(now, settings.DRIFT_RETENTION_DAYS),
    ),
    Purge(
        "bitácora del motor",
        EngineVersion.id,
        lambda now: EngineVersion.noted_at < now - timedelta(days=settings.DRIFT_RETENTION_DAYS),
    ),
    Purge(
        "alertas de peticiones lentas resueltas",
        SlowRequestAlert.id,
        lambda now: and_(
            SlowRequestAlert.status == SlowAlertStatus.RESOLVED,
            SlowRequestAlert.last_seen_at < now - timedelta(days=settings.SLOW_ALERT_RESOLVED_RETENTION_DAYS),
        ),
    ),
    # La plantilla diaria y sus marcas se conservan mientras se pueden reponer y cobrar (la ventana de
    # reposición y un mes más); los cargos guardan sus unidades por mes.
    Purge(
        "plantilla diaria",
        (HeadcountDay.company_id, HeadcountDay.day),
        lambda now: HeadcountDay.day < _days_ago(now, settings.BILLING_HEADCOUNT_BACKFILL_DAYS + 31),
    ),
    Purge(
        "tareas diarias",
        (DailyTask.task, DailyTask.day),
        lambda now: DailyTask.day < _days_ago(now, settings.BILLING_HEADCOUNT_BACKFILL_DAYS + 31),
    ),
)


def _deleted_before(model: Any, now: datetime) -> ColumnElement[bool]:
    """En «Eliminados» desde hace más de `SOFT_DELETE_RETENTION_DAYS` (lo recorre el índice parcial `ix_*_deleted`)."""
    return model.deleted_at < now - timedelta(days=settings.SOFT_DELETE_RETENTION_DAYS)


def _company_purgeable(now: datetime) -> ColumnElement[bool]:
    """Una empresa eliminada hace más de la retención a la que ya no le queda nada que la detenga: cuentas, empleados,
    validadores, registros faciales ni movimientos de cobranza (sus documentos salen justo antes que ella)."""
    return and_(
        _deleted_before(Company, now),
        *(
            ~exists().where(model.company_id == Company.id)
            for model in (Employee, User, Validator, FaceEnrollment, Charge, Payment)
        ),
    )


def _kept_by(model: Any, now: datetime) -> ColumnElement[bool]:
    """Un registro de `model` que todavía NO se depura (vigente o eliminado hace menos de la retención)."""
    return or_(
        model.deleted_at.is_(None), model.deleted_at >= now - timedelta(days=settings.SOFT_DELETE_RETENTION_DAYS)
    )


#: Lo eliminado por una persona, depurado al vencer su retención (borrado lógico; ver el docstring del módulo). En
#: este orden: cada tabla después de las que la referencian con RESTRICT, y cada condición salta lo que todavía tiene
#: una referencia (así nunca choca con una llave foránea). Las empresas también esperan a que no les quede ninguna
#: cuenta, empleado, registro facial ni movimiento de cobranza.
SOFT_DELETE_PURGES: tuple[Purge, ...] = (
    Purge(
        "cambios de turno cancelados",
        ShiftAssignment.id,
        lambda now: and_(
            _deleted_before(ShiftAssignment, now),
            ~exists().where(
                WorkSession.assignment_id == ShiftAssignment.id, WorkSession.company_id == ShiftAssignment.company_id
            ),
        ),
    ),
    Purge("días laborables eliminados", EmployeeWorkday.id, lambda now: _deleted_before(EmployeeWorkday, now)),
    Purge("festivos eliminados", CompanyHoliday.id, lambda now: _deleted_before(CompanyHoliday, now)),
    # Documentos de identidad del empleado (onboarding con OCR, 2026-10-07): SOLO los que el propio empleado eliminó de
    # su lista (p. ej. al reemplazar un archivo), al vencer su retención. Al eliminar al empleado sus documentos se
    # borran DE VERDAD con la persona (regla 13, LFPDPPP: una identificación lleva su foto y sus datos;
    # `person_erasure.erase_employee_documents`), así que un empleado eliminado ya no tiene ninguno: no hay que
    # depurarlos antes que a él. Cada lote encola sus objetos para salir del bucket en su misma transacción.
    Purge(
        "documentos de empleados eliminados",
        EmployeeDocument.id,
        lambda now: _deleted_before(EmployeeDocument, now),
        objects=EmployeeDocument.object_name,
    ),
    # Con su asistencia, bitácora, ausencias, solicitudes, dispositivos y casos de fraude (CASCADE): lo que la LFT pide
    # conservar hasta un año después de la baja. Sus datos biométricos ya se habían borrado al eliminarlo.
    Purge("empleados eliminados", Employee.id, lambda now: _deleted_before(Employee, now)),
    Purge(
        "turnos eliminados",
        Shift.id,
        lambda now: and_(
            _deleted_before(Shift, now),
            ~exists().where(ShiftAssignment.shift_id == Shift.id, ShiftAssignment.company_id == Shift.company_id),
        ),
    ),
    # Los kioscos de un sitio (antifraude 2b): solo su vinculación; un sitio depurado se los lleva en cascada.
    Purge("kioscos eliminados", SiteKiosk.id, lambda now: _deleted_before(SiteKiosk, now)),
    Purge(
        "sitios eliminados",
        WorkSite.id,
        lambda now: and_(
            _deleted_before(WorkSite, now),
            ~exists().where(ShiftSite.site_id == WorkSite.id, ShiftSite.company_id == WorkSite.company_id),
            ~exists().where(
                WorkSession.company_id == WorkSite.company_id,
                or_(WorkSession.check_in_site_id == WorkSite.id, WorkSession.check_out_site_id == WorkSite.id),
            ),
            ~exists().where(AttendanceEvent.company_id == WorkSite.company_id, AttendanceEvent.site_id == WorkSite.id),
        ),
    ),
    Purge("validadores eliminados", Validator.id, lambda now: _deleted_before(Validator, now)),
    Purge(
        "departamentos eliminados",
        Department.id,
        lambda now: and_(
            _deleted_before(Department, now),
            ~exists().where(Employee.department_id == Department.id, Employee.company_id == Department.company_id),
        ),
    ),
    # Una cuenta sale cuando ya no la usa ningún empleo ni validador que se conserve (borrarla se los llevaría).
    Purge(
        "cuentas eliminadas",
        User.id,
        lambda now: and_(
            _deleted_before(User, now),
            ~exists().where(Employee.user_id == User.id, _kept_by(Employee, now)),
            ~exists().where(Validator.user_id == User.id, _kept_by(Validator, now)),
        ),
    ),
    # Documentos de la empresa (migración 0075): los eliminados al vencer su retención y, antes de su empresa, los de
    # una empresa que ya se puede depurar (su borrado en cascada no avisaría al bucket). Cada lote encola sus
    # objetos para salir del bucket en su misma transacción.
    Purge(
        "documentos de empresas eliminados",
        CompanyDocument.id,
        lambda now: _deleted_before(CompanyDocument, now),
        objects=CompanyDocument.object_name,
    ),
    Purge(
        "documentos de empresas que se depuran",
        CompanyDocument.id,
        lambda now: exists().where(Company.id == CompanyDocument.company_id, _company_purgeable(now)),
        objects=CompanyDocument.object_name,
    ),
    Purge(
        "empresas eliminadas",
        Company.id,
        lambda now: and_(_company_purgeable(now), ~exists().where(CompanyDocument.company_id == Company.id)),
    ),
)


def _purge(db: Session, purge: Purge, moment: datetime, size: int) -> int:
    """Una depuración en lotes de `size` (cada uno en su transacción corta), con el tope de lotes por vuelta."""
    total = 0
    try:
        # Tope de lotes por tabla en una vuelta: el resto sigue en la siguiente.
        for _ in range(settings.MAINTENANCE_MAX_BATCHES_PER_TABLE):
            count = delete_batch(db, purge.key, purge.condition(moment), size, objects=purge.objects)
            db.commit()
            total += count
            if count < size:
                break
    except SQLAlchemyError:
        # Cada tabla por separado: una que falla (bloqueo, statement_timeout) no deja sin depurar a
        # las demás; lo borrado en lotes anteriores ya quedó confirmado y la siguiente vuelta sigue.
        db.rollback()
        logger.exception("Falló la depuración de %s (se reintenta en la siguiente vuelta)", purge.name)
    return total


@observed("maintenance.run")
def purge_expired(db: Session, *, batch_size: int | None = None, now: datetime | None = None) -> dict[str, int]:
    """Depura todo lo vencido en lotes; devuelve cuántas filas salieron de cada tabla."""
    size = batch_size or settings.MAINTENANCE_BATCH_SIZE
    moment = now or datetime.now(UTC)
    removed: dict[str, int] = _maintain_partitions(db, moment)
    for purge in PURGES:
        removed[purge.name] = _purge(db, purge, moment, size)
    for purge in SOFT_DELETE_PURGES:
        removed[purge.name] = _purge(db, purge, moment, settings.SOFT_DELETE_PURGE_BATCH_SIZE)
    removed["jornadas sin salida"] = _close_missed_checkouts(db, moment)
    removed.update(_fraud(db, moment, size))
    removed["umbrales recalibrados"] = _recalibrate(db, moment)
    removed[DRIFT_ROWS] = _drift(db, moment)
    removed[PERF_ROLLUPS] = _rollup_performance(db, moment)
    # Cobranza y consumo: plantilla diaria, cargos, suspensión por falta de pago, pronósticos y la foto
    # del almacenamiento (cada tarea falla sola).
    removed.update(billing_jobs.run(db, moment))
    # Imágenes: copia cifrada al bucket, descarte y borrado de lo que ya no se conserva (cada tarea falla sola).
    removed.update(storage_jobs.run(db, moment))
    return removed


#: Lo que reporta el mantenimiento de particiones (solo PostgreSQL).
PARTITIONS_CREATED = "particiones creadas"
PARTITIONS_DROPPED = "particiones vencidas borradas"


@observed("maintenance.partitions")
def _maintain_partitions(db: Session, now: datetime) -> dict[str, int]:
    """Tablas particionadas por mes (app/core/partitions.py): deja creados los meses que vienen y BORRA de un golpe
    los meses vencidos (`DROP TABLE` de su partición, en lugar de un DELETE fila por fila). Cada tabla falla sola
    (p. ej. un candado ocupado: `lock_timeout` de la función) y la siguiente vuelta lo vuelve a intentar; lo que
    quede vencido en `_legacy` o `_default` lo borra después su depuración por lotes (`PURGES`). Solo PostgreSQL."""
    if db.get_bind().dialect.name != "postgresql":
        return {}
    created = dropped = 0
    for table, spec in PARTITIONED.items():
        keep_from = _days_ago(now, spec.retention_days()) if spec.retention_days else None
        try:
            made, gone = partition_repository.ensure(db, table, now.date(), settings.PARTITION_MONTHS_AHEAD, keep_from)
            db.commit()
        except SQLAlchemyError:
            db.rollback()
            logger.exception(
                "Falló el mantenimiento de las particiones de %s (se reintenta en la siguiente vuelta)", table
            )
            continue
        created, dropped = created + made, dropped + gone
    return {PARTITIONS_CREATED: created, PARTITIONS_DROPPED: dropped}


#: Lo que reporta el resumen del rendimiento.
PERF_ROLLUPS = "resúmenes de rendimiento"


@observed("maintenance.perf_rollup")
def _rollup_performance(db: Session, now: datetime) -> int:
    """Las horas y los días recientes del rendimiento vuelven a sumarse (`perf_rollup`); falla solo (no detiene el
    resto del mantenimiento) y la siguiente vuelta lo rehace."""
    try:
        return perf_rollup.run(db, now)
    except SQLAlchemyError:
        db.rollback()
        logger.exception("Falló el resumen del rendimiento (se reintenta en la siguiente vuelta)")
        return 0


def _fraud(db: Session, now: datetime, batch: int) -> dict[str, int]:
    """La evidencia vencida de los casos de fraude (sale del bucket por la cola de borrado) y los casos decididos
    hace más de su retención; falla sola y se reintenta en la siguiente vuelta."""
    try:
        return fraud_case_service.purge(db, now, batch)
    except SQLAlchemyError:
        db.rollback()
        logger.exception("Falló la depuración de los casos de fraude (se reintenta en la siguiente vuelta)")
        return {}


#: Lo que reporta el monitoreo de deriva (filas calculadas de la última ventana completa que faltaba).
DRIFT_ROWS = "deriva de señales calculada"


def _drift(db: Session, now: datetime) -> int:
    """La deriva de las señales (antifraude fase 3, `drift_service`), si falta la última ventana completa; falla sola
    (no detiene el resto del mantenimiento) y la siguiente vuelta lo vuelve a intentar."""
    try:
        return drift_service.run_if_due(db, now)
    except SQLAlchemyError:
        db.rollback()
        logger.exception("Falló el monitoreo de deriva de las señales (se reintenta en la siguiente vuelta)")
        return 0


def _recalibrate(db: Session, now: datetime) -> int:
    """La autocalibración de la seguridad facial (face_security), si ya toca; falla sola (no detiene el
    resto del mantenimiento) y se reintenta en la siguiente vuelta."""
    try:
        return recalibrate_if_due(db, now)
    except SQLAlchemyError:
        db.rollback()
        logger.exception("Falló la autocalibración de la seguridad facial (se reintenta en la siguiente vuelta)")
        return 0


def _close_missed_checkouts(db: Session, now: datetime) -> int:
    """Las jornadas abiertas cuyo límite de salida venció quedan "sin salida" (falla sola: no
    detiene el resto del mantenimiento)."""
    try:
        closed = close_missed_checkouts(db, now)
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        logger.exception("Falló el cierre de jornadas sin salida (se reintenta en la siguiente vuelta)")
        return 0
    return closed


def run_once() -> dict[str, int] | None:
    """Una vuelta de mantenimiento si ninguna otra instancia la está haciendo (None si se saltó)."""
    return exclusive(purge_expired)


def exclusive[T](work: Callable[[Session], T]) -> T | None:
    """Ejecuta `work` con el candado del mantenimiento (una instancia a la vez); None si otra lo tiene.
    También lo toman los comandos del almacenamiento (`python -m app.cli storage ...`): nunca chocan con
    una vuelta del mantenimiento.

    El candado es de TRANSACCIÓN (`pg_try_advisory_xact_lock`) en una conexión propia que deja su
    transacción abierta mientras `work` trabaja con otras: se suelta solo al terminar esa transacción (al
    salir del `with`, también si `work` falla) o si la conexión se cae (proceso muerto). Así funciona igual
    directo a PostgreSQL que detrás de PgBouncer en modo transacción, donde un candado de SESIÓN se quedaría
    pegado a una conexión del servidor que luego usa otro cliente (y ninguna instancia volvería a depurar).
    La transacción del candado no toma una foto de los datos (READ COMMITTED, sin consultas): no frena al
    autovacuum."""
    if engine.dialect.name != "postgresql":
        with platform_session() as db:
            return work(db)
    with engine.connect() as lock:
        if not lock.execute(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": MAINTENANCE_LOCK_KEY}).scalar():
            return None
        # Esta transacción espera sin consultas mientras dura la vuelta: el límite de transacción inactiva del
        # rol de la API (DB_IDLE_IN_TRANSACTION_TIMEOUT_MS) no aplica a ella (solo a ella: SET LOCAL).
        lock.execute(text("SET LOCAL idle_in_transaction_session_timeout = 0"))
        with platform_session() as db:
            return work(db)


class MaintenanceScheduler:
    """Hilo en segundo plano que ejecuta `run_once` cada `interval` segundos (se detiene al apagar)."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="maintenance", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            # La base local de IP es de cada proceso (o del volumen de la réplica): se pone al día aquí, fuera del
            # candado del mantenimiento (tiene su propio candado de archivo) y sin la BD. Nunca lanza.
            ip_database.refresh_if_due()
            try:
                removed = run_once()
            except Exception:  # la BD puede estar caída: se reintenta en la siguiente vuelta
                logger.exception("Falló el mantenimiento programado")
                continue
            if removed and any(removed.values()):
                logger.info("Mantenimiento: %s", ", ".join(f"{k}={v}" for k, v in removed.items() if v))
