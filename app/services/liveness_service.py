"""Retos de prueba de vida (liveness) emitidos por el servidor.

Flujo:
1. Quien opera la cámara solicita un reto: el servidor elige al azar de uno a tres movimientos
   (girar a la izquierda o a la derecha, mirar arriba o abajo, acercarse; nunca el mismo dos veces
   seguidas) y, si la empresa lo usa, una secuencia de colores para el destello de la pantalla.
2. El cliente captura frames frontales, uno por cada color del destello y uno por cada movimiento,
   en orden (entre movimientos la persona vuelve al frente).
3. Al verificar, el reto se consume (uso único), debe pertenecer al usuario y no haber vencido (la
   empresa decide cuánto dura). Un video o una foto preparados de antemano no conocen la secuencia,
   y un video inyectado no puede reflejar en el rostro colores que no conocía.

Almacenamiento en PostgreSQL (tabla `face_challenges`): compartido entre todos los procesos
de la API, por lo que se puede escalar horizontalmente sin Redis.
"""

import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.clock import as_utc, has_passed
from app.facial_recognition import LivenessAction
from app.facial_recognition.photometry import FLASH_PALETTE
from app.models import FaceChallenge
from app.repositories.challenge_repository import FaceChallengeRepository
from app.services.catalog_service import get_catalogs
from app.services.face_service import face_rejection

#: Movimientos que puede pedir un reto (verification_policy.liveness_steps).
MAX_STEPS = 3
#: Sin movimientos activos en el catálogo (configuración rota) se piden los giros.
FALLBACK_ACTIONS = (LivenessAction.TURN_LEFT, LivenessAction.TURN_RIGHT)


@dataclass(frozen=True)
class LivenessResponse:
    """Lo que la app envía como respuesta al reto (en el mismo formulario que las frontales)."""

    challenge_id: str | None = None
    #: Una captura por movimiento del reto, en orden.
    steps: tuple[bytes, ...] = ()
    #: Una captura por color del destello, en orden.
    flash: tuple[bytes, ...] = ()


#: Sin respuesta al reto (la empresa no exige prueba de vida).
NO_RESPONSE = LivenessResponse()


@dataclass(frozen=True)
class Challenge:
    id: str
    user_id: int
    #: Movimientos en orden (de uno a tres).
    actions: tuple[LivenessAction, ...]
    #: Cuándo se emitió (para exigir el tiempo humano mínimo de respuesta).
    issued_at: datetime
    expires_at: datetime
    #: Colores del destello en orden (códigos de FLASH_PALETTE); vacío sin destello.
    flash: tuple[str, ...] = ()


def random_sequence[T](options: Sequence[T], count: int) -> tuple[T, ...]:
    """`count` elementos al azar (criptográfico), nunca el mismo dos veces seguidas si hay de dónde
    elegir: un paso repetido se podría "cumplir" sin moverse entre uno y otro."""
    chosen: list[T] = []
    for _ in range(count):
        pool = [o for o in options if not chosen or o != chosen[-1]] or list(options)
        chosen.append(secrets.choice(pool))
    return tuple(chosen)


def active_actions() -> tuple[LivenessAction, ...]:
    """Movimientos activos del catálogo (la plataforma puede retirar uno sin desplegar código)."""
    catalogs = get_catalogs()
    return tuple(a for a in LivenessAction if catalogs.is_active("liveness_actions", a.value)) or FALLBACK_ACTIONS


class ChallengeStore:
    def issue(self, db: Session, user_id: int, *, steps: int, lifetime_seconds: int, flash: int = 0) -> Challenge:
        """Reto nuevo de `steps` movimientos (y `flash` colores) que vence en `lifetime_seconds`."""
        now = datetime.now(UTC)
        challenge = Challenge(
            id=secrets.token_urlsafe(24),
            user_id=user_id,
            actions=random_sequence(active_actions(), max(1, min(steps, MAX_STEPS))),
            issued_at=now,
            expires_at=now + timedelta(seconds=lifetime_seconds),
            flash=random_sequence(tuple(FLASH_PALETTE), flash) if flash > 0 else (),
        )
        actions = [a.value for a in challenge.actions] + [None] * (MAX_STEPS - len(challenge.actions))
        FaceChallengeRepository(db).replace_for_user(
            FaceChallenge(
                id=challenge.id,
                user_id=user_id,
                direction=actions[0],
                second_direction=actions[1],
                third_direction=actions[2],
                flash_colors=",".join(challenge.flash) or None,
                issued_at=challenge.issued_at,
                expires_at=challenge.expires_at,
            )
        )
        db.commit()
        return challenge

    def consume(self, db: Session, challenge_id: str, user_id: int) -> Challenge | None:
        """Devuelve el reto si es válido para el usuario; en cualquier caso lo elimina (atómico)."""
        row = FaceChallengeRepository(db).take(challenge_id)
        db.commit()
        if row is None:
            return None
        owner, first, second, third, colors, issued_at, expires_at = row
        if owner != user_id or has_passed(expires_at):
            return None
        return Challenge(
            id=challenge_id,
            user_id=owner,
            actions=tuple(LivenessAction(a) for a in (first, second, third) if a),
            issued_at=as_utc(issued_at),
            expires_at=as_utc(expires_at),
            flash=tuple(colors.split(",")) if colors else (),
        )

    def require(
        self, db: Session, user_id: int, response: LivenessResponse, *, required: bool, flash_required: bool = False
    ) -> Challenge | None:
        """Consume el reto obligatorio de prueba de vida (None si la política no lo exige).

        422 LIVENESS_REQUIRED si falta el reto o la captura de algún movimiento (o del destello, si la
        empresa lo exige); 422 CHALLENGE_INVALID si venció, ya se usó o pertenece a otro usuario.
        Mientras el destello solo se observa, una app que no lo envía (versión anterior) no se bloquea.
        """
        if not required:
            return None
        if not response.challenge_id or not response.steps:
            raise face_rejection("LIVENESS_REQUIRED")
        challenge = self.consume(db, response.challenge_id, user_id)
        if challenge is None:
            raise face_rejection("CHALLENGE_INVALID")
        if len(response.steps) != len(challenge.actions):
            raise face_rejection("LIVENESS_REQUIRED")  # falta (o sobra) la captura de algún movimiento
        flash_missing = bool(challenge.flash) and not response.flash and flash_required
        if flash_missing or (response.flash and len(response.flash) != len(challenge.flash)):
            raise face_rejection("LIVENESS_REQUIRED")
        return challenge


challenge_store = ChallengeStore()
