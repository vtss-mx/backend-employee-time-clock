"""Gobierno de la política de verificación de una empresa (solo el ADMIN de la plataforma).

Fase 0 del antifraude (docs/rd/antifraude-identidad.md §5; decisión D12 del dueño del producto):

- **Historial**: cada cambio queda en `ops.policy_changes` con quién, cuándo, el motivo y cada campo antes → después
  (también el ajuste de cada señal del motor de riesgo, `risk_signals.<código>.<mode|points>`). Solo se inserta.
- **Regla de dos personas**: endurecer aplica al momento; lo que RELAJA la seguridad (`policy_rules.relaxes`) queda
  "por aprobar" y la política sigue como estaba hasta que OTRO ADMIN lo apruebe (o lo rechace con su motivo); quien
  lo pidió puede retirarlo. Vence a las `POLICY_CHANGE_APPROVAL_HOURS` y, si la política cambió después de pedirlo,
  ya no se aplica (se pide de nuevo sobre lo vigente). `POLICY_TWO_PERSON_RULE=false` la apaga (una plataforma con
  un solo ADMIN).
- **Niveles predefinidos** (Estándar, Alto, Máximo; `policy_rules.PRESETS`): un clic que pasa por el mismo camino.
- **Simulación**: un cambio del motor de riesgo lleva el resumen de "qué habría pasado" en los últimos días.

Todo cambio pasa por aquí: valida contra los catálogos, bloquea la fila de la política (dos ADMIN no se pisan) y
limpia la caché del proceso (las demás réplicas lo ven en `POLICY_CACHE_SECONDS`).
"""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.core.clock import as_utc
from app.core.config import settings
from app.core.exceptions import ConflictError, NotFoundError, UnprocessableError
from app.i18n import stored
from app.models import PolicyChange, PolicyChangeStatus, RiskAction, SignalMode, User
from app.repositories.policy_repository import PolicyRepository
from app.repositories.risk_repository import PolicyChangeRepository, RiskSignalStatRepository
from app.repositories.user_repository import UserRepository
from app.schemas.common import PageParams
from app.schemas.policy import (
    AdminPolicyRead,
    PolicyChangeList,
    PolicyChangeRead,
    PolicyFieldChange,
    PolicyUpdateResult,
    RiskPolicyCandidate,
    RiskSignalSettingRead,
    RiskSimulationRead,
    VerificationPolicyRead,
    VerificationPolicyUpdate,
)
from app.services import risk_simulation
from app.services.catalog_service import get_catalogs
from app.services.policy_rules import PRESETS, FieldChange, diff, flatten_signals
from app.services.policy_service import PolicyService, PolicySnapshot, clear_policy_cache
from app.services.risk_rules import MEASURE_ONLY_SIGNALS

#: Campos que deben ser un código activo de su catálogo: (campo, catálogo y código del error, que nombra su mensaje).
CATALOG_FIELDS = (
    ("anti_spoofing_level", "antispoof_levels", "INVALID_ANTISPOOF_LEVEL"),
    ("flash_liveness", "flash_modes", "INVALID_FLASH_MODE"),
    ("employee_device_mode", "employee_device_modes", "INVALID_DEVICE_MODE"),
    ("risk_medium_action", "risk_actions", "INVALID_RISK_ACTION"),
    ("risk_high_action", "risk_actions", "INVALID_RISK_ACTION"),
    ("risk_critical_action", "risk_actions", "INVALID_RISK_ACTION"),
    ("risk_fallback_action", "risk_actions", "INVALID_RISK_ACTION"),
    # Antifraude 2b: firma y ubicación de los validadores y código de sitio (modos de una señal).
    ("validator_signing", "signal_modes", "INVALID_SIGNAL_MODE"),
    ("validator_location", "signal_modes", "INVALID_SIGNAL_MODE"),
    ("site_codes", "signal_modes", "INVALID_SIGNAL_MODE"),
)
#: Niveles de confianza (catalog.confidence_levels).
CONFIDENCE_FIELDS = ("min_confidence", "identify_confidence", "duplicate_confidence")
#: Si el motor falla nunca se niega ni se deja en revisión a ciegas (también un CHECK de la tabla).
FALLBACK_ACTIONS = (RiskAction.ALLOW, RiskAction.ALERT, RiskAction.STEP_UP)
#: Campos del motor de riesgo: cambiarlos adjunta la simulación al historial (el modo del dispositivo del empleado
#: también decide: pide un paso más o deja "en revisión" lo que viene de uno desconocido).
RISK_FIELDS = frozenset(
    {
        "risk_engine",
        "risk_medium_score",
        "risk_high_score",
        "risk_critical_score",
        "risk_medium_action",
        "risk_high_action",
        "risk_critical_action",
        "employee_device_mode",
    }
)
SIGNALS = "risk_signals"
SIGNAL_PREFIX = "risk_signals."
#: Notas con que el sistema cierra un cambio pendiente que ya no se puede aplicar: se guardan como llave y se
#: traducen al leerse (`app/i18n/stored.py`); sus códigos de error dicen lo mismo al responder.
EXPIRED_NOTE = stored("POLICY_CHANGE_EXPIRED")
STALE_NOTE = stored("POLICY_CHANGED_SINCE")


def _rank(catalog: str, code: str) -> int:
    """Orden de un código en su catálogo (mayor = más estricto en los catálogos con orden)."""
    row = get_catalogs().get(catalog, code)
    return int(row["sort_order"]) if row else 0


def signal_defaults() -> dict[str, dict[str, Any]]:
    """El modo y los puntos de la plataforma de cada señal activa (catalog.risk_signals)."""
    return {
        row["code"]: {"mode": row["mode"], "points": int(row["points"])}
        for row in get_catalogs().entries["risk_signals"]
        if row["active"]
    }


def effective_signals(overrides: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Cada señal con los ajustes de la empresa encima de los de la plataforma."""
    return {code: {**default, **overrides.get(code, {})} for code, default in signal_defaults().items()}


def minimal_overrides(effective: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Solo lo que difiere de la plataforma (lo igual no se guarda: si la plataforma cambia una señal, la empresa
    la sigue)."""
    defaults = signal_defaults()
    result = {}
    for code, setting in effective.items():
        own = {key: value for key, value in setting.items() if value != defaults[code][key]}
        if own:
            result[code] = own
    return result


class PolicyGovernance:
    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id
        self.service = PolicyService(db, company_id)
        self.changes = PolicyChangeRepository(db, company_id)

    # ------------------------------------------------------------------ lectura

    def read(self) -> AdminPolicyRead:
        """La política completa (con el motor de riesgo, la línea base de cada señal y lo pendiente)."""
        row = PolicyRepository(self.db, self.company_id).get() or self.service.ensure()
        base = VerificationPolicyRead.model_validate(row)
        updated_by = UserRepository(self.db).emails_by_ids((row.updated_by_id,)).get(row.updated_by_id or 0)
        stats = RiskSignalStatRepository(self.db, self.company_id).of_company()
        effective = effective_signals(row.risk_signals)
        signals = [
            RiskSignalSettingRead(
                code=item["code"],
                name=item["name"],
                description=item["description"],
                kind=item["kind"],
                hard=item["hard"],
                client=item["client"],
                default_points=item["points"],
                default_mode=item["mode"],
                points=effective[item["code"]]["points"],
                mode=effective[item["code"]]["mode"],
                confirmed=stats.get(item["code"], (0, 0))[0],
                false_positive=stats.get(item["code"], (0, 0))[1],
                measure_only=item["code"] in MEASURE_ONLY_SIGNALS,
            )
            for item in get_catalogs().entries["risk_signals"]
            if item["active"]
        ]
        return AdminPolicyRead(
            **base.model_dump(exclude={"updated_by"}),
            updated_by=updated_by,
            duplicate_confidence=row.duplicate_confidence,
            employee_device_mode=row.employee_device_mode,
            preset=row.preset,
            risk_engine=row.risk_engine,
            risk_medium_score=row.risk_medium_score,
            risk_high_score=row.risk_high_score,
            risk_critical_score=row.risk_critical_score,
            risk_medium_action=row.risk_medium_action,
            risk_high_action=row.risk_high_action,
            risk_critical_action=row.risk_critical_action,
            risk_fallback_action=row.risk_fallback_action,
            fraud_evidence=row.fraud_evidence,
            flash_paced=row.flash_paced,
            capture_burst=row.capture_burst,
            validator_signing=row.validator_signing,
            validator_location=row.validator_location,
            site_codes=row.site_codes,
            risk_signals=signals,
            pending_changes=self.changes.pending_count(),
            two_person_rule=settings.POLICY_TWO_PERSON_RULE,
        )

    def history(self, *, status: str | None, page: PageParams, viewer: User) -> PolicyChangeList:
        items, total = self.changes.search(status=status, offset=page.offset, limit=page.size)
        return PolicyChangeList.of([self._change_read(change, viewer) for change in items], total, page)

    def simulate(self, wanted: RiskPolicyCandidate) -> RiskSimulationRead:
        """Lo que habría pasado con la configuración de riesgo candidata (sin guardar nada)."""
        values = self._validated(wanted.model_dump(exclude_none=True))
        current = self.service.current()
        target = risk_simulation.candidate(current, values)
        self._ensure_scores(target.risk_medium_score, target.risk_high_score, target.risk_critical_score)
        return risk_simulation.simulate(self.db, self.company_id, current, target)

    # ------------------------------------------------------------------ pedir un cambio

    def request(self, data: VerificationPolicyUpdate, user: User) -> PolicyUpdateResult:
        values = data.model_dump(exclude_unset=True, exclude_none=True, exclude={"reason"})
        return self._submit(self._validated(values), user, reason=data.reason)

    def apply_preset(self, preset: str, reason: str | None, user: User) -> PolicyUpdateResult:
        """Un nivel predefinido (catalog.policy_presets): fija sus campos; las señales que no nombra vuelven a su valor
        de la plataforma."""
        if preset not in PRESETS or not get_catalogs().is_active("policy_presets", preset):
            raise UnprocessableError(code="INVALID_POLICY_PRESET", field="preset")
        values = {key: value for key, value in PRESETS[preset].items() if key != SIGNALS}
        effective = effective_signals(PRESETS[preset][SIGNALS])
        return self._submit({**values, SIGNALS: effective}, user, reason=reason, preset=preset, replace_signals=True)

    def _validated(self, values: dict[str, Any]) -> dict[str, Any]:
        catalogs = get_catalogs()
        for name in CONFIDENCE_FIELDS:
            if name in values:
                level = catalogs.confidence_level(values[name])
                if level is None:
                    raise UnprocessableError(code="INVALID_CONFIDENCE_LEVEL", field=name)
                values[name] = level["value"]
        for name, catalog, code in CATALOG_FIELDS:
            if name in values and not catalogs.is_active(catalog, values[name]):
                raise UnprocessableError(code=code, field=name)
        if values.get("risk_fallback_action", RiskAction.ALLOW) not in FALLBACK_ACTIONS:
            raise UnprocessableError(
                code="INVALID_RISK_FALLBACK",
                field="risk_fallback_action",
            )
        for code, setting in values.get(SIGNALS, {}).items():
            if code not in signal_defaults():
                raise UnprocessableError(code="INVALID_RISK_SIGNAL", field=SIGNALS)
            if "mode" in setting and not catalogs.is_active("signal_modes", setting["mode"]):
                raise UnprocessableError(code="INVALID_SIGNAL_MODE", field=SIGNALS)
            if code in MEASURE_ONLY_SIGNALS and setting.get("mode") == SignalMode.ENFORCE:
                # El pulso por video solo se mide hasta calibrarlo (docs/rd §8 P4): exigirlo no tendría efecto.
                raise UnprocessableError(code="SIGNAL_MEASURE_ONLY", field=SIGNALS)
        return values

    @staticmethod
    def _ensure_scores(medium: int, high: int, critical: int) -> None:
        if not medium < high < critical:
            raise UnprocessableError(
                code="RISK_SCORES_ORDER",
                field="risk_high_score",
            )

    def _submit(
        self,
        wanted: dict[str, Any],
        user: User,
        *,
        reason: str | None,
        preset: str | None = None,
        replace_signals: bool = False,
    ) -> PolicyUpdateResult:
        current = self.service.values(lock=True)
        now_signals = effective_signals(current[SIGNALS])
        asked = wanted.pop(SIGNALS, None)
        if asked is None:
            new_signals = now_signals
        elif replace_signals:
            new_signals = asked
        else:
            new_signals = effective_signals(risk_simulation.merge_signals(minimal_overrides(now_signals), asked))
        target = {**current, **wanted}
        self._ensure_scores(target["risk_medium_score"], target["risk_high_score"], target["risk_critical_score"])
        changes = diff(current, wanted, _rank) + diff(flatten_signals(now_signals), flatten_signals(new_signals), _rank)
        if not changes and (preset is None or current["preset"] == preset):
            self.db.rollback()  # suelta el candado: no hay nada que guardar
            return PolicyUpdateResult(policy=self.read())
        relaxes = any(change.relaxes for change in changes)
        record = PolicyChange(
            company_id=self.company_id,
            created_at=datetime.now(UTC),
            status=PolicyChangeStatus.PENDING
            if relaxes and settings.POLICY_TWO_PERSON_RULE
            else PolicyChangeStatus.APPLIED,
            relaxes=relaxes,
            preset=preset,
            changes=[change.as_dict() for change in changes],
            reason=reason,
            simulation=self._simulation(current, target, new_signals, changes),
            requested_by_id=user.id,
            requested_by=user.email,
        )
        if record.status == PolicyChangeStatus.APPLIED:
            self.service.apply({**wanted, SIGNALS: minimal_overrides(new_signals), "preset": preset}, user.id)
        self.changes.add(record)
        self.db.commit()
        clear_policy_cache(self.company_id)
        return PolicyUpdateResult(policy=self.read(), change=self._change_read(record, user))

    def _simulation(
        self,
        current: Mapping[str, Any],
        target: Mapping[str, Any],
        signals: Mapping[str, Mapping[str, Any]],
        changes: list[FieldChange],
    ) -> dict[str, Any] | None:
        """El resumen de la simulación si el cambio toca el motor de riesgo (consulta acotada). Solo importan los
        campos del motor: el resto de la política no cambia la decisión."""
        if not any(c.field in RISK_FIELDS or c.field.startswith(SIGNAL_PREFIX) for c in changes):
            return None
        fallback = {"risk_fallback_action": current["risk_fallback_action"]}
        before = PolicySnapshot(**{k: current[k] for k in RISK_FIELDS}, **fallback, risk_signals=dict(current[SIGNALS]))
        after = PolicySnapshot(
            **{k: target[k] for k in RISK_FIELDS}, **fallback, risk_signals=minimal_overrides(signals)
        )
        return risk_simulation.simulate(self.db, self.company_id, before, after).model_dump()

    # ------------------------------------------------------------------ decidir un cambio pendiente

    def approve(self, change_id: int, user: User) -> PolicyUpdateResult:
        change = self._pending(change_id)
        if change.requested_by_id == user.id:
            raise ConflictError(
                code="POLICY_SELF_APPROVAL",
            )
        current = self.service.values(lock=True)
        now_signals = effective_signals(current[SIGNALS])
        flat = {**current, **flatten_signals(now_signals)}
        if any(flat.get(item["field"]) != item["before"] for item in change.changes):
            self._close(change, PolicyChangeStatus.CANCELLED, user, STALE_NOTE)
            raise ConflictError(code="POLICY_CHANGED_SINCE")
        scalars = {
            item["field"]: item["after"] for item in change.changes if not item["field"].startswith(SIGNAL_PREFIX)
        }
        for item in change.changes:
            if item["field"].startswith(SIGNAL_PREFIX):
                _, code, key = item["field"].split(".")
                now_signals[code][key] = item["after"]
        # Quien cambió la política es quien lo pidió (el que aprobó queda en el historial del cambio).
        values = {**scalars, SIGNALS: minimal_overrides(now_signals), "preset": change.preset}
        self.service.apply(values, change.requested_by_id)
        self._close(change, PolicyChangeStatus.APPLIED, user, None)
        clear_policy_cache(self.company_id)
        return PolicyUpdateResult(policy=self.read(), change=self._change_read(change, user))

    def reject(self, change_id: int, user: User, note: str) -> PolicyChangeRead:
        change = self._pending(change_id)
        if change.requested_by_id == user.id:
            raise ConflictError(code="POLICY_SELF_DECISION")
        self._close(change, PolicyChangeStatus.REJECTED, user, note)
        return self._change_read(change, user)

    def cancel(self, change_id: int, user: User) -> PolicyChangeRead:
        change = self._pending(change_id)
        if change.requested_by_id != user.id:
            raise ConflictError(code="POLICY_NOT_REQUESTER")
        self._close(change, PolicyChangeStatus.CANCELLED, user, None)
        return self._change_read(change, user)

    def _pending(self, change_id: int) -> PolicyChange:
        """El cambio por decidir, bloqueado; uno vencido se cancela (y se avisa)."""
        change = self.changes.locked(change_id)
        if change is None:
            raise NotFoundError(code="POLICY_CHANGE_NOT_FOUND")
        if change.status != PolicyChangeStatus.PENDING:
            raise ConflictError(code="POLICY_CHANGE_NOT_PENDING")
        if datetime.now(UTC) >= _expires(change):
            self._close(change, PolicyChangeStatus.CANCELLED, None, EXPIRED_NOTE)
            raise ConflictError(code="POLICY_CHANGE_EXPIRED")
        return change

    def _close(self, change: PolicyChange, status: PolicyChangeStatus, user: User | None, note: str | None) -> None:
        change.status = status
        change.decided_at = datetime.now(UTC)
        change.decided_by_id = user.id if user else None
        change.decided_by = user.email if user else None
        change.decision_note = note
        self.db.commit()

    @staticmethod
    def _change_read(change: PolicyChange, viewer: User) -> PolicyChangeRead:
        pending = change.status == PolicyChangeStatus.PENDING
        return PolicyChangeRead(
            id=change.id,
            status=change.status,
            relaxes=change.relaxes,
            preset=change.preset,
            changes=[PolicyFieldChange(**item) for item in change.changes],
            reason=change.reason,
            simulation=RiskSimulationRead.model_validate(change.simulation) if change.simulation else None,
            requested_by=change.requested_by,
            requested_by_me=change.requested_by_id == viewer.id,
            created_at=change.created_at,
            expires_at=_expires(change) if pending else None,
            decided_by=change.decided_by,
            decided_at=change.decided_at,
            decision_note=change.decision_note,
        )


def _expires(change: PolicyChange) -> datetime:
    return as_utc(change.created_at) + timedelta(hours=settings.POLICY_CHANGE_APPROVAL_HOURS)
