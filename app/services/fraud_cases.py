"""Casos de fraude que se abren desde un intento facial (docs/rd/antifraude-identidad.md §3.3).

Un intento abre un caso (o se suma al caso activo de su sujeto) si un candado lo rechazó por sospechoso
(`SECURITY_REASONS`: foto, cámara virtual, reenvío, destello plano...) o si el motor de riesgo decidió avisar, dejarlo
en revisión o negarlo. El sujeto es el empleado (si se supo quién era) o la cuenta que operó la cámara (un validador
sin identificar a nadie): mientras su caso siga activo, sus intentos se suman a él (sentencia atómica; a lo más
`FRAUD_CASE_MAX_ATTEMPTS` con detalle).

Cada intento del caso guarda una COPIA de lo medido (motivos, números y huellas pHash): el caso se revisa y aprende
aunque la bitácora y las métricas venzan. La evidencia (decisión D1) se sube DESPUÉS de confirmar el intento, sin
transacción abierta y de mejor esfuerzo: si el bucket falla, el caso queda sin esos fotogramas y la falla se registra
(nunca cambia la respuesta a la persona).
"""

import logging
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.object_storage import get_storage
from app.models import (
    FaceAttemptMetric,
    FraudCaseAttempt,
    FraudCaseEvent,
    FraudCaseEventKind,
    FraudEvidence,
    FraudKind,
    VerificationLog,
)
from app.repositories.fraud_repository import FraudCaseRepository
from app.services import image_storage
from app.services.attack_signatures import capture_value
from app.services.face_service import SECURITY_REASONS
from app.services.face_signals import AttemptSignals
from app.services.image_storage import FRAUD_EVIDENCE, image_type
from app.services.risk_rules import CASE_ACTIONS

logger = logging.getLogger(__name__)

#: Tipo de fraude que sugiere cada rechazo de un candado.
REASON_KINDS: Mapping[str, FraudKind] = {
    "SPOOF_DETECTED": FraudKind.PRESENTATION,
    "FLASH_MISMATCH": FraudKind.PRESENTATION,
    "FLASH_FLAT": FraudKind.PRESENTATION,
    "IMAGE_NOT_FROM_CAMERA": FraudKind.INJECTION,
    "VIRTUAL_CAMERA": FraudKind.INJECTION,
    "CHALLENGE_TOO_FAST": FraudKind.INJECTION,
    "CAPTURE_INCONSISTENT": FraudKind.INJECTION,
    "STATIC_CAPTURE": FraudKind.INJECTION,
    "REPLAY_DETECTED": FraudKind.REPLAY,
    "REPLAY_PERCEPTUAL": FraudKind.REPLAY,
    "KNOWN_ATTACK": FraudKind.REPLAY,
}
#: Los números de las métricas que se copian al intento del caso.
METRIC_FIELDS = (
    "steps",
    "flash_mode",
    "response_seconds",
    "frontal_real_min",
    "frontal_real_mean",
    "step_real_min",
    "yaw_min",
    "pitch_min",
    "closer_min",
    "flash_score",
    "flash_magnitude",
    "flash_background",
    "flash_ratio",
    "quality_mean",
    "brightness_mean",
    # Antifraude 2a: el protocolo de captura (la ráfaga nunca se guarda; solo sus números).
    "burst_frames",
    "burst_motion",
    "pulse_snr",
    "moire",
    "noise_ratio",
    "parallax",
    "flash_pace_ms",
    # El consenso de identidad de la ráfaga (2026-10-06).
    "burst_consensus",
)


@dataclass(frozen=True)
class OpenedCase:
    """El caso al que se sumó el intento (para subir su evidencia después de confirmarlo)."""

    id: int
    company_id: int
    employee_id: int | None
    #: Fotogramas que ya tenía el caso (tope FRAUD_EVIDENCE_MAX_PER_CASE).
    evidence: int


def _kind_and_reason(reason: str | None, signals: AttemptSignals) -> tuple[str, str]:
    """El tipo de fraude y el motivo que abren el caso: el del candado o la señal que más pesó."""
    if reason is not None and reason in REASON_KINDS:
        return REASON_KINDS[reason], reason
    reasons = signals.assessment.reasons if signals.assessment is not None else []
    enforced = [r for r in reasons if r.get("mode") == "ENFORCE"] or reasons
    if enforced:
        return str(enforced[0]["kind"]), str(enforced[0]["code"])
    return FraudKind.OTHER, reason or "RISK_ALERT"


def _subject(log: VerificationLog) -> str:
    """De quién es el caso: el empleado; sin empleado (un 1:N que no identificó a nadie), quien operaba la cámara: el
    dispositivo de la API pública (su huella, 32 caracteres: cabe en `subject`) o la cuenta."""
    if log.employee_id is not None:
        return f"employee:{log.employee_id}"
    if log.device_hash is not None:
        return f"device:{log.device_hash[:32]}"
    return f"actor:{log.user_id}"


def observe(
    db: Session,
    *,
    log: VerificationLog,
    signals: AttemptSignals,
    metric: FaceAttemptMetric,
    camera: str | None = None,
) -> OpenedCase | None:
    """Abre o suma el caso del intento si corresponde (en la transacción del intento: se confirma con él)."""
    assessment = signals.assessment
    risky = assessment is not None and assessment.action in CASE_ACTIONS
    if log.reason not in SECURITY_REASONS and not risky:
        return None
    kind, reason = _kind_and_reason(log.reason, signals)
    now = datetime.now(UTC)
    subject = _subject(log)
    repo = FraudCaseRepository(db)
    case_id, attempts, evidence = repo.observe(
        {
            "company_id": log.company_id,
            "subject": subject,
            "kind": kind,
            "employee_id": log.employee_id,
            "actor_id": log.user_id,
            "created_at": now,
            "last_attempt_at": now,
            "max_score": assessment.score if assessment is not None else None,
            "tier": assessment.tier if assessment is not None else None,
            "reason": reason,
        }
    )
    if attempts == 1:
        repo.add_event(
            FraudCaseEvent(
                company_id=log.company_id,
                case_id=case_id,
                created_at=now,
                kind=FraudCaseEventKind.OPENED,
                status_to="OPEN",
                note=reason,
            )
        )
    if attempts <= settings.FRAUD_CASE_MAX_ATTEMPTS:
        repo.add_attempt(_attempt(log, signals, metric, case_id, now, camera))
    return OpenedCase(case_id, log.company_id, log.employee_id, evidence)


def _attempt(
    log: VerificationLog,
    signals: AttemptSignals,
    metric: FaceAttemptMetric,
    case_id: int,
    now: datetime,
    camera: str | None,
) -> FraudCaseAttempt:
    assessment = signals.assessment
    numbers: dict[str, Any] = {name: getattr(metric, name) for name in METRIC_FIELDS}
    return FraudCaseAttempt(
        company_id=log.company_id,
        case_id=case_id,
        attempted_at=log.created_at or now,
        verification_log_id=log.id,
        assessment_id=assessment.id if assessment is not None else None,
        metric_id=metric.id,
        success=log.success,
        reason=log.reason,
        score=assessment.score if assessment is not None else None,
        action=assessment.action if assessment is not None else None,
        signals=list(assessment.reasons) if assessment is not None else [],
        metrics={key: value for key, value in numbers.items() if value is not None},
        phashes=[
            capture_value(f.face_phash, f.frame_phash)
            for f in signals.frontal
            if f.face_phash is not None and f.frame_phash is not None
        ],
        camera=(camera or "")[:120] or None,
        ip_address=log.ip_address,
        user_agent=log.user_agent,
    )


def store_evidence(db: Session, case: OpenedCase, frames: list[tuple[str, int, bytes]], log_id: int) -> int:
    """Sube los fotogramas del intento al bucket (CIFRADOS, sin transacción abierta) y guarda su referencia en una
    transacción corta. De mejor esfuerzo: si el bucket o la BD fallan, el caso queda sin ellos (falla registrada y lo
    subido se borra). Devuelve cuántos se guardaron."""
    room = max(0, settings.FRAUD_EVIDENCE_MAX_PER_CASE - case.evidence)
    frames = frames[:room]
    if not frames or not get_storage().configured:
        return 0
    now = datetime.now(UTC)
    rows = [
        FraudEvidence(
            company_id=case.company_id,
            case_id=case.id,
            employee_id=case.employee_id,
            verification_log_id=log_id,
            created_at=now,
            kind=kind,
            position=position,
            uid=secrets.token_hex(8),
            content_type=image_type(image),
        )
        for kind, position, image in frames
    ]
    try:
        for row, (_, _, image) in zip(rows, frames, strict=True):
            image_storage.store(db, FRAUD_EVIDENCE, row, image)
        repo = FraudCaseRepository(db)
        repo.add_evidence(rows)
        repo.count_evidence(case.id, len(rows))
        db.commit()
    except Exception:
        db.rollback()
        image_storage.abandon(db)
        logger.exception("No se pudo guardar la evidencia del caso de fraude %s (el caso sigue sin ella)", case.id)
        return 0
    return len(rows)
