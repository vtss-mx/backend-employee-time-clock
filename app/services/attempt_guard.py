"""Bloqueo temporal tras intentos de identificación facial fallidos o sospechosos seguidos.

Sin este freno, quien tenga fotos o videos de una persona podría probar una y otra vez hasta
engañar al sistema. Se cuentan los fallos de la bitácora (attendance.verification_logs) en los
últimos minutos que fija la empresa (lockout_minutes); un intento exitoso reinicia la cuenta. Al llegar a
lockout_max_failures se responde 429 `FACE_LOCKED` (con `Retry-After`) hasta que el fallo
más antiguo de esos salga de la ventana.

- Verificación 1:1 (el empleado, o la empresa en persona): por EMPLEADO, todos los fallos (rostro
  que no coincide, prueba de vida, intentos sospechosos). Protege la identidad de esa persona.
- Punto de control (1:N) y registro facial: por QUIEN OPERA LA CÁMARA, solo intentos sospechosos
  (en un acceso es normal que alguien no registrado no coincida).
"""

import math
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.clock import as_utc
from app.core.exceptions import AppError
from app.models import VerificationMethod
from app.repositories.verification_repository import VerificationLogRepository
from app.services.catalog_service import get_catalogs
from app.services.face_service import SECURITY_REASONS
from app.services.policy_service import PolicySnapshot

#: Fallos que cuentan en la verificación 1:1.
MATCH_FAILURES = ("NO_MATCH", "LIVENESS_FAILED", "LIVENESS_MISMATCH", *SECURITY_REASONS)
FACE_METHODS = (VerificationMethod.FACE, VerificationMethod.QR_FACE)


class FaceLockedError(AppError):
    status_code = 429
    code = "FACE_LOCKED"

    def __init__(self, retry_after: int) -> None:
        minutes = max(1, math.ceil(retry_after / 60))
        super().__init__(
            get_catalogs().face_error_message("FACE_LOCKED", {"minutes": minutes}),
            headers={"Retry-After": str(retry_after)},
            details={"retry_after": retry_after},
        )


def ensure_unlocked(
    db: Session,
    policy: PolicySnapshot,
    *,
    employee_id: int | None = None,
    actor_id: int | None = None,
    reasons: tuple[str, ...] = MATCH_FAILURES,
) -> None:
    """429 FACE_LOCKED si hubo demasiados fallos seguidos del empleado (o de quien opera la cámara).

    Intentos y minutos los define la empresa (lockout_max_failures, lockout_minutes); puede desactivarlo.
    """
    key = employee_id if employee_id is not None else actor_id
    if key is None or not policy.lockout_enabled:
        return
    window = timedelta(minutes=policy.lockout_minutes)
    now = datetime.now(UTC)
    limit = policy.lockout_max_failures
    failures = [
        as_utc(at)
        for at in VerificationLogRepository(db).recent_failures(
            employee_id=employee_id,
            actor_id=actor_id,
            methods=FACE_METHODS,
            reasons=reasons,
            since=now - window,
            limit=limit,
        )
    ]
    if len(failures) < limit:
        return
    # Se libera cuando el más antiguo de los `limit` fallos más recientes sale de la ventana.
    unlocks_at = failures[limit - 1] + window
    raise FaceLockedError(max(1, math.ceil((unlocks_at - now).total_seconds())))
