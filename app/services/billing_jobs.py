"""Tareas de cobranza y consumo del mantenimiento (fuera de las peticiones; una instancia a la vez por
el candado del mantenimiento). Cada tarea falla sola: una que falla se registra y las demás siguen; lo
que no alcanzó a hacerse sigue en la siguiente vuelta.

1. **Plantilla diaria** (`close_headcount_days`): cierra los días que faltan (el de ayer y, hacia atrás
   hasta BILLING_HEADCOUNT_BACKFILL_DAYS, los que no se cerraron por una instancia caída), a lo más
   BILLING_DAYS_PER_ROUND por vuelta. Repetible: recalcular un día da lo mismo.
2. **Cargos** (`issue_charges`): emite el cargo de cada empresa cuyo corte ya pasó (uno por corte; cada
   empresa en su transacción) y le aplica su saldo a favor.
3. **Suspensión automática** (`suspend_overdue`): suspende a la empresa con un cargo vencido sin pagar
   después de sus días de gracia (o de la gracia extra que dio una reactivación manual) y cierra al
   momento sus sesiones.
4. **Pronósticos** (`refresh_forecasts`): el pronóstico del periodo en curso de cada plan, una vez al día.
5. **Almacenamiento** (`snapshot_storage`): la foto diaria del almacenamiento de cada empresa.
"""

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.clock import business_date, business_day_bounds
from app.core.config import settings
from app.core.observability import observed
from app.i18n import stored
from app.models import SuspensionReason
from app.repositories.billing_repository import BillingRepository
from app.services.billing_ledger import Ledger
from app.services.billing_rules import suspension_due
from app.services.billing_service import BillingService
from app.services.usage_service import capture_storage

logger = logging.getLogger(__name__)

STORAGE_TASK = "STORAGE"


def close_headcount_days(db: Session, today: date) -> int:
    """Cierra la plantilla de los días que faltan, el más antiguo primero. Siempre ayer; hacia atrás solo
    desde el inicio del plan más antiguo (antes no hay nada que cobrar)."""
    repo = BillingRepository(db)
    yesterday = today - timedelta(days=1)
    floor = today - timedelta(days=settings.BILLING_HEADCOUNT_BACKFILL_DAYS)
    earliest = repo.earliest_start()
    first = max(floor, earliest) if earliest is not None else yesterday
    first = min(first, yesterday)
    closed = repo.closed_days(first, yesterday)
    missing = [first + timedelta(days=offset) for offset in range((yesterday - first).days + 1)]
    pending = [day for day in missing if day not in closed][: settings.BILLING_DAYS_PER_ROUND]
    for day in pending:
        repo.close_headcount(day, *business_day_bounds(day))
        db.commit()
    return len(pending)


def issue_charges(db: Session, today: date) -> int:
    """Emite los cargos de los cortes que ya pasaron (cada empresa en su transacción: una que falla no
    detiene a las demás)."""
    closed_from = today - timedelta(days=settings.BILLING_HEADCOUNT_BACKFILL_DAYS)
    issued = 0
    for company_id in BillingRepository(db).plans_due(today - timedelta(days=1), settings.BILLING_CHARGES_PER_ROUND):
        try:
            charge = Ledger(db).issue_due_charge(company_id, today, closed_from)
            db.commit()
        except Exception:
            db.rollback()
            logger.exception(
                "No se pudo emitir el cargo de la empresa %s (se reintenta en la siguiente vuelta)", company_id
            )
            continue
        issued += charge is not None
    return issued


def suspend_overdue(db: Session, today: date) -> int:
    """Suspende a las empresas con un cargo vencido después de su gracia y cierra sus sesiones."""
    suspended = 0
    for overdue in BillingRepository(db).overdue_companies(today):
        if overdue.grace_until is not None and today <= overdue.grace_until:
            continue  # el ADMIN la reactivó a mano: tiene su gracia extra
        if not suspension_due(overdue.oldest_due_on, overdue.grace_days, today):
            continue
        ledger = Ledger(db)
        company = ledger.lock(overdue.company_id)
        # La nota se guarda como llave y datos: el ADMIN la lee en su idioma (`app/i18n/stored.py`).
        note = stored("SUSPENSION_NON_PAYMENT_NOTE", {"due_on": overdue.oldest_due_on, "count": overdue.grace_days})
        ledger.suspend(company, SuspensionReason.NON_PAYMENT, note, None)
        db.commit()
        logger.info("Empresa %s suspendida por falta de pago", overdue.company_id)
        suspended += 1
    return suspended


def refresh_forecasts(db: Session, today: date) -> int:
    """Refresca el pronóstico de los planes que no se han refrescado hoy (por lotes, con un tope por vuelta)."""
    repo = BillingRepository(db)
    day_start = business_day_bounds(today)[0]
    plans = repo.plans_to_forecast(day_start, settings.BILLING_FORECASTS_PER_ROUND)
    active = repo.active_headcount(p.company_id for p in plans)  # empleados + validadores de todas en una consulta
    service = BillingService(db)
    for plan in plans:  # marcados con el día de la vuelta: el mismo día no se repiten
        service.refresh_forecast(plan, today, active[plan.company_id], max(day_start, datetime.now(UTC)))
    db.commit()
    return len(plans)


def snapshot_storage(db: Session, today: date) -> int:
    """La foto del almacenamiento de hoy, una vez al día."""
    repo = BillingRepository(db)
    if repo.daily_task_done(STORAGE_TASK, today):
        return 0
    saved = capture_storage(db, today)
    repo.mark_daily_task(STORAGE_TASK, today)
    db.commit()
    return saved


#: Tareas en orden (la plantilla antes que los cargos; los cargos antes que la suspensión).
TASKS: tuple[tuple[str, Callable[[Session, date], int]], ...] = (
    ("plantilla diaria (días cerrados)", close_headcount_days),
    ("cargos emitidos", issue_charges),
    ("empresas suspendidas por falta de pago", suspend_overdue),
    ("pronósticos de cobro", refresh_forecasts),
    ("fotos de almacenamiento", snapshot_storage),
)


def run(db: Session, now: datetime | None = None) -> dict[str, int]:
    """Una vuelta de las tareas de cobranza y consumo; cada una falla sola (se registra y sigue)."""
    today = business_date(now or datetime.now(UTC))
    done: dict[str, int] = {}
    for name, task in TASKS:
        try:
            with observed(f"billing.{task.__name__}"):
                done[name] = task(db, today)
        except Exception:
            db.rollback()
            logger.exception("Falló la tarea de %s (se reintenta en la siguiente vuelta)", name)
            done[name] = 0
    return done
