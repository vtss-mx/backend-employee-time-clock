"""Monitoreo de deriva de las señales del motor facial (antifraude fase 3, I+D §3.5; migración 0081).

Decisión del dueño del producto (2026-10-06). Al cerrar cada ventana (`DRIFT_WINDOW_DAYS`, semanas alineadas al lunes)
el mantenimiento —fuera de toda petición, una instancia a la vez— compara, por señal del motor y por plataforma del
navegador, los intentos GENUINOS (aprobados y sin fraude confirmado) de la ventana con los de la anterior: mediana, la
cola que vigila la señal y el índice de estabilidad de población (`drift_stats`). Un PSI mayor que `DRIFT_PSI_ALERT` o
una cola que se mueve hacia lo sospechoso más de `DRIFT_TAIL_DROP_ALERT` es una ALERTA, que llega al ADMIN por el MISMO
camino que las de los respaldos y el PITR: `logger.error` con un logger estable por señal y plataforma →
`ErrorLogHandler` → "Errores del sistema" (una fila por llave con su contador; se reabre sola si vuelve).

Además, por empresa y ventana (fraude interno, I+D §3.5): intentos, casos de fraude abiertos, revisiones decididas y
cuántas se aprobaron en menos de `DRIFT_QUICK_REVIEW_SECONDS` desde que se abrieron ("aprueba sin mirar"); con al menos
`DRIFT_MIN_REVIEWS` decisiones y una fracción mayor que `DRIFT_QUICK_APPROVAL_RATIO`, alerta al ADMIN igual.

Reglas que no se rodean:
- El destello de colores se retiró (migración 0080): sus señales no se miden ni se muestran (`EXCLUDED_SIGNALS`).
- Una ventana en la que cambió el motor o los modelos faciales (bitácora `engine_log`) se mide pero no se compara
  (`VERSION_CHANGE`): la línea base nunca mezcla versiones incompatibles.
- Solo mide y avisa: ningún umbral ni política cambia aquí (lo automático solo endurece, y vive en `face_security`).
- Repetir el cálculo de una ventana es idempotente (reemplaza sus filas); `compute_window` lo usa también el ADMIN
  («Calcular ahora») y el mantenimiento solo calcula la última ventana completa que falte.
"""

import logging
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.core.clock import business_day_bounds, business_today
from app.core.config import settings
from app.core.devices import PLATFORMS
from app.core.observability import observed
from app.i18n import t
from app.models import CompanyFraudWeekly, SignalDrift
from app.models.drift import DRIFT_ALERT, DRIFT_INSUFFICIENT, DRIFT_OK
from app.repositories.drift_repository import DriftRepository
from app.schemas.common import PageParams
from app.schemas.drift import CompanyDriftList, CompanyDriftRow, DriftList, DriftRow, DriftSummary, EngineVersionRead
from app.services import engine_log
from app.services.drift_stats import Comparison, compare, last_closed_window, quick_approval
from app.services.face_security import SIGNAL_BY_KEY, SIGNALS, Signal

logger = logging.getLogger(__name__)

#: El destello se retiró de la experiencia (0080): no es una señal viva.
EXCLUDED_SIGNALS = frozenset({"FLASH_SCORE", "FLASH_RATIO"})
#: Las señales que se vigilan, en el orden de `face_security.SIGNALS`.
DRIFT_SIGNALS: tuple[Signal, ...] = tuple(signal for signal in SIGNALS if signal.key not in EXCLUDED_SIGNALS)
#: Ventanas que se listan en la pantalla (≈ 60 semanas) y cambios de versión recientes que se muestran.
WEEKS_SHOWN = 60
VERSIONS_SHOWN = 20
#: Tope de revisiones decididas que se leen por ventana (solo lo que el motor dejó en revisión: pocas).
REVIEWS_LIMIT = 50_000


def _bounds(week_start: date) -> tuple[datetime, datetime]:
    """[inicio, fin) de la ventana en UTC, con los días del negocio."""
    start, _ = business_day_bounds(week_start)
    end, _ = business_day_bounds(week_start + timedelta(days=settings.DRIFT_WINDOW_DAYS))
    return start, end


def _signal_rows(repo: DriftRepository, week_start: date, now: datetime) -> list[SignalDrift]:
    start, end = _bounds(week_start)
    baseline_start = start - (end - start)
    changed = repo.changed_within(engine_log.INCOMPATIBLE, baseline_start, end)
    columns = [signal.column for signal in DRIFT_SIGNALS]
    rows: list[SignalDrift] = []
    for platform in PLATFORMS:
        # Una lectura por plataforma y ventana con todas las señales; cada señal toma de ahí sus valores medidos.
        current_rows = repo.platform_samples(columns, platform, start, end, settings.DRIFT_MAX_SAMPLES)
        baseline_rows = repo.platform_samples(columns, platform, baseline_start, start, settings.DRIFT_MAX_SAMPLES)
        for index, signal in enumerate(DRIFT_SIGNALS):
            outcome = compare(
                _values(current_rows, index),
                _values(baseline_rows, index),
                upper=signal.upper,
                min_samples=settings.DRIFT_MIN_SAMPLES,
                psi_alert=settings.DRIFT_PSI_ALERT,
                tail_drop_alert=settings.DRIFT_TAIL_DROP_ALERT,
                version_changed=changed,
            )
            rows.append(_signal_row(week_start, signal.key, platform, outcome, now))
    return rows


def _values(rows: Sequence[tuple[Any, ...]], index: int) -> list[float]:
    """Los valores medidos de una señal (una columna) en las filas leídas; lo que no se midió (NULL) no cuenta."""
    return [float(row[index]) for row in rows if row[index] is not None]


def _signal_row(week_start: date, key: str, platform: str, outcome: Comparison, now: datetime) -> SignalDrift:
    return SignalDrift(
        week_start=week_start,
        signal=key,
        platform=platform,
        samples=outcome.samples,
        baseline_samples=outcome.baseline_samples,
        median=outcome.median,
        baseline_median=outcome.baseline_median,
        tail=outcome.tail,
        baseline_tail=outcome.baseline_tail,
        tail_percentile=outcome.tail_percentile,
        tail_change=outcome.tail_change,
        psi=outcome.psi,
        status=outcome.status,
        computed_at=now,
    )


def _company_rows(repo: DriftRepository, week_start: date, now: datetime) -> list[CompanyFraudWeekly]:
    start, end = _bounds(week_start)
    attempts = repo.attempts_by_company(start, end)
    cases = repo.cases_by_company(start, end)
    reviews: dict[int, list[int]] = {}  # empresa → [decididas, aprobadas, aprobadas sin mirar]
    for company_id, status, opened_at, decided_at in repo.reviews(start, end, REVIEWS_LIMIT):
        counts = reviews.setdefault(int(company_id), [0, 0, 0])
        counts[0] += 1
        counts[1] += status == "CONFIRMED"
        counts[2] += quick_approval(status, opened_at, decided_at, settings.DRIFT_QUICK_REVIEW_SECONDS)
    companies = set(attempts) | set(cases) | set(reviews)
    names = repo.company_names(companies)
    rows: list[CompanyFraudWeekly] = []
    for company_id in sorted(companies):
        decided, approved, quick = reviews.get(company_id, [0, 0, 0])
        tried = attempts.get(company_id, 0)
        quick_rate = round(quick / decided, 4) if decided else None
        if decided < settings.DRIFT_MIN_REVIEWS:
            status = DRIFT_INSUFFICIENT
        else:
            status = DRIFT_ALERT if (quick_rate or 0.0) > settings.DRIFT_QUICK_APPROVAL_RATIO else DRIFT_OK
        rows.append(
            CompanyFraudWeekly(
                company_id=company_id,
                company_name=names.get(company_id, str(company_id))[:150],
                week_start=week_start,
                attempts=tried,
                fraud_cases=cases.get(company_id, 0),
                case_rate=round(cases.get(company_id, 0) / tried, 4) if tried else None,
                reviews=decided,
                approved=approved,
                quick_approvals=quick,
                quick_rate=quick_rate,
                status=status,
                computed_at=now,
            )
        )
    return rows


def _alert(rows: list[SignalDrift], companies: list[CompanyFraudWeekly], week_start: date) -> None:
    """Las alertas de la ventana al ADMIN por el camino de siempre: un `logger.error` con un logger estable por señal y
    plataforma (o por empresa), que `ErrorLogHandler` lleva a "Errores del sistema" (una fila por llave con su contador;
    resuelta y vuelve = se reabre sola). El mensaje dice qué se midió."""
    for row in rows:
        if row.status != DRIFT_ALERT:
            continue
        logging.getLogger(f"app.drift.{row.signal}.{row.platform}").error(
            "Deriva de la señal %s en %s (semana del %s): PSI %s, cola p%s %s → %s (%s intentos frente a %s de la "
            "semana anterior). Revisa si cambió el navegador, el sistema o la cámara de esa plataforma.",
            row.signal,
            row.platform,
            week_start.isoformat(),
            row.psi,
            row.tail_percentile,
            row.baseline_tail,
            row.tail,
            row.samples,
            row.baseline_samples,
        )
    for company in companies:
        if company.status != DRIFT_ALERT:
            continue
        logging.getLogger("app.drift.company").error(
            "Posible fraude interno en la empresa #%s (%s), semana del %s: aprobó sin mirar %s de %s revisiones "
            "(%s %%, en menos de %s s); %s casos de fraude en %s intentos.",
            company.company_id,
            company.company_name,
            week_start.isoformat(),
            company.quick_approvals,
            company.reviews,
            round((company.quick_rate or 0.0) * 100),
            settings.DRIFT_QUICK_REVIEW_SECONDS,
            company.fraud_cases,
            company.attempts,
        )


@observed("drift.compute")
def compute_window(db: Session, week_start: date, now: datetime) -> int:
    """Calcula (o recalcula) una ventana completa: anota las versiones, reemplaza sus filas y avisa las alertas.
    Devuelve cuántas filas quedaron (señales y empresas). Una sola transacción."""
    repo = DriftRepository(db)
    engine_log.record_versions(db, now)
    db.flush()
    signals = _signal_rows(repo, week_start, now)
    companies = _company_rows(repo, week_start, now)
    total = repo.replace_signals(week_start, signals) + repo.replace_companies(week_start, companies)
    db.commit()
    _alert(signals, companies, week_start)
    return total


def run_if_due(db: Session, now: datetime) -> int:
    """Desde el mantenimiento: la última ventana completa que aún no se calculó (0 si ya está o está apagado)."""
    if not settings.DRIFT_ENABLED:
        return 0
    week_start = last_closed_window(business_today(), settings.DRIFT_WINDOW_DAYS)
    if DriftRepository(db).has_week(week_start):
        return 0
    return compute_window(db, week_start, now)


# ---------------------------------------------------------------- lecturas del ADMIN


def _row(row: SignalDrift) -> DriftRow:
    signal = SIGNAL_BY_KEY[row.signal]
    return DriftRow.model_validate(row).model_copy(update={"signal_name": t(signal.name), "upper": signal.upper})


def summary(db: Session) -> DriftSummary:
    repo = DriftRepository(db)
    weeks = repo.weeks(WEEKS_SHOWN)
    latest = weeks[0] if weeks else None
    alerts, insufficient, companies = repo.counts(latest) if latest else (0, 0, 0)
    return DriftSummary(
        window_days=settings.DRIFT_WINDOW_DAYS,
        psi_alert=settings.DRIFT_PSI_ALERT,
        tail_drop_alert=settings.DRIFT_TAIL_DROP_ALERT,
        min_samples=settings.DRIFT_MIN_SAMPLES,
        quick_review_seconds=settings.DRIFT_QUICK_REVIEW_SECONDS,
        quick_approval_ratio=settings.DRIFT_QUICK_APPROVAL_RATIO,
        weeks=weeks,
        latest_week=latest,
        alerts=alerts,
        insufficient=insufficient,
        companies_alerted=companies,
        platforms=list(PLATFORMS),
        versions=[EngineVersionRead.model_validate(row) for row in repo.versions(VERSIONS_SHOWN)],
        computed_at=repo.computed_at(latest) if latest else None,
    )


def _week(repo: DriftRepository, week: date | None) -> date | None:
    """La ventana pedida o, sin ella, la más reciente calculada (None si no hay ninguna)."""
    if week is not None:
        return week
    weeks = repo.weeks(1)
    return weeks[0] if weeks else None


def signals_page(
    db: Session, *, week: date | None, platform: str | None, status: str | None, page: PageParams
) -> DriftList:
    repo = DriftRepository(db)
    chosen = _week(repo, week)
    if chosen is None:
        return DriftList.of([], 0, page, week_start=None)
    rows, total = repo.signals_page(chosen, platform=platform, status=status, offset=page.offset, limit=page.size)
    return DriftList.of([_row(row) for row in rows], total, page, week_start=chosen)


def companies_page(db: Session, *, week: date | None, search: str | None, page: PageParams) -> CompanyDriftList:
    repo = DriftRepository(db)
    chosen = _week(repo, week)
    if chosen is None:
        return CompanyDriftList.of([], 0, page, week_start=None)
    rows, total = repo.companies_page(chosen, search=search, offset=page.offset, limit=page.size)
    return CompanyDriftList.of([CompanyDriftRow.model_validate(row) for row in rows], total, page, week_start=chosen)
