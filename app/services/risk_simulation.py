"""Simulación de una política de riesgo: "¿qué habría pasado en los últimos días con ella?" (docs/rd §3.6 y §5.4).

Vuelve a decidir cada intento guardado de la empresa (`ops.risk_assessments`: sus motivos son TODAS las señales que
se dispararon, también las de "solo medir") con la configuración vigente y con la candidata, con las MISMAS reglas
del motor en vivo (`risk_rules.replay`, sin duplicar lógica). Reporta cuántos intentos terminarían en cada acción,
cuántos fraudes confirmados detendría, cuántos intentos no marcados como fraude ya no se permitirían directo
(estimación de molestias) y qué señales pesaron más.

Acotada (regla 8): una consulta por índice (company_id, created_at) con tope `RISK_SIMULATION_MAX_ATTEMPTS` sobre
`RISK_SIMULATION_DAYS` días; el cálculo es aritmética sobre esas filas (milisegundos). Límite visible en la pantalla:
solo se simula lo que se midió en su momento (una señal nueva empieza en "solo medir" para tener historia) y los
intentos que los candados rechazaron antes del motor (foto, cámara virtual...) no tienen decisión que simular.
"""

from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import RiskAction
from app.repositories.face_security_repository import FRAUD_LABEL
from app.repositories.risk_repository import RiskAssessmentRepository
from app.schemas.policy import RiskSimulationRead, SimulationActions, SimulationReason
from app.services.policy_service import PolicySnapshot
from app.services.risk_engine import config_for
from app.services.risk_rules import replay

#: Acciones que detienen un intento (no lo dejan pasar directo).
STOPPING = frozenset({RiskAction.STEP_UP, RiskAction.REVIEW, RiskAction.DENY})
#: Cuántas señales se reportan como las que más pesaron.
TOP_REASONS = 5


def merge_signals(current: dict[str, dict[str, Any]], wanted: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Los ajustes por señal con los nuevos encima (lo omitido de cada señal queda como estaba)."""
    merged = {code: dict(setting) for code, setting in current.items()}
    for code, setting in wanted.items():
        merged.setdefault(code, {}).update({key: value for key, value in setting.items() if value is not None})
    return merged


def candidate(policy: PolicySnapshot, changes: dict[str, Any]) -> PolicySnapshot:
    """La política con los cambios de riesgo aplicados (sin guardar nada)."""
    values = {key: value for key, value in changes.items() if value is not None and key != "risk_signals"}
    signals = merge_signals(policy.risk_signals, changes.get("risk_signals") or {})
    return replace(policy, **values, risk_signals=signals)


def _actions(counter: Counter[str]) -> SimulationActions:
    return SimulationActions(**{action.value.lower(): counter[action.value] for action in RiskAction})


def simulate(db: Session, company_id: int, current: PolicySnapshot, wanted: PolicySnapshot) -> RiskSimulationRead:
    since = datetime.now(UTC) - timedelta(days=settings.RISK_SIMULATION_DAYS)
    rows = RiskAssessmentRepository(db).window(company_id, since, settings.RISK_SIMULATION_MAX_ATTEMPTS)
    now_config, new_config = config_for(current), config_for(wanted)
    before: Counter[str] = Counter()
    after: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    stricter = looser = frauds = stopped = affected = 0
    order = list(RiskAction)
    for stored, _action, label, step_up in rows:
        old = replay(stored, now_config, step_up_done=step_up)
        new = replay(stored, new_config, step_up_done=step_up)
        before[old.action.value] += 1
        after[new.action.value] += 1
        stricter += order.index(new.action) > order.index(old.action)
        looser += order.index(new.action) < order.index(old.action)
        if label == FRAUD_LABEL:
            frauds += 1
            stopped += new.action in STOPPING
        elif new.action in STOPPING:
            affected += 1
        if new.action in STOPPING:
            reasons.update(r.code for r in new.enforced)
    return RiskSimulationRead(
        days=settings.RISK_SIMULATION_DAYS,
        evaluated=len(rows),
        capped=len(rows) >= settings.RISK_SIMULATION_MAX_ATTEMPTS,
        current=_actions(before),
        candidate=_actions(after),
        stricter=stricter,
        looser=looser,
        frauds_stopped=stopped,
        frauds=frauds,
        genuine_affected=affected,
        top_reasons=[SimulationReason(code=code, count=count) for code, count in reasons.most_common(TOP_REASONS)],
    )
