"""Retos de prueba de vida (liveness) emitidos por el servidor.

Flujo:
1. Quien opera la cámara solicita un reto: el servidor elige al azar uno o dos giros
   (TURN_LEFT / TURN_RIGHT), según la política de la empresa.
2. El cliente captura frames frontales y uno con la cabeza girada por cada giro, en orden
   (entre giros la persona vuelve al frente).
3. Al verificar, el reto se consume (uso único), debe pertenecer al usuario y no
   haber expirado. Un video o foto preparado de antemano no conoce la secuencia.

Almacenamiento en PostgreSQL (tabla `face_challenges`): compartido entre todos los procesos
de la API, por lo que se puede escalar horizontalmente sin Redis.
"""

import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.clock import as_utc, has_passed
from app.core.config import settings
from app.facial_recognition import TurnDirection
from app.models import FaceChallenge
from app.repositories.challenge_repository import FaceChallengeRepository
from app.services.face_service import face_rejection


@dataclass(frozen=True)
class Challenge:
    id: str
    user_id: int
    #: Giros en orden (uno o dos).
    directions: tuple[TurnDirection, ...]
    expires_at: datetime

    @property
    def direction(self) -> TurnDirection:
        return self.directions[0]

    @property
    def issued_at(self) -> datetime:
        """Cuándo se emitió (para exigir el tiempo humano mínimo de respuesta)."""
        return as_utc(self.expires_at) - timedelta(seconds=settings.FACE_CHALLENGE_TTL_SECONDS)


class ChallengeStore:
    def issue(self, db: Session, user_id: int, steps: int = 1) -> Challenge:
        now = datetime.now(UTC)
        options = [d.value for d in TurnDirection]
        challenge = Challenge(
            id=secrets.token_urlsafe(24),
            user_id=user_id,
            directions=tuple(TurnDirection(secrets.choice(options)) for _ in range(max(1, min(steps, 2)))),
            expires_at=now + timedelta(seconds=settings.FACE_CHALLENGE_TTL_SECONDS),
        )
        FaceChallengeRepository(db).replace_for_user(
            FaceChallenge(
                id=challenge.id,
                user_id=user_id,
                direction=challenge.directions[0].value,
                second_direction=challenge.directions[1].value if len(challenge.directions) > 1 else None,
                expires_at=challenge.expires_at,
            )
        )
        db.commit()
        return challenge

    def consume(self, db: Session, challenge_id: str, user_id: int) -> Challenge | None:
        """Devuelve el reto si es válido para el usuario; en cualquier caso lo elimina (atómico)."""
        row = FaceChallengeRepository(db).take(challenge_id)
        db.commit()
        if row is None or row.user_id != user_id or has_passed(row.expires_at):
            return None
        directions = tuple(TurnDirection(d) for d in (row.direction, row.second_direction) if d)
        return Challenge(id=challenge_id, user_id=row.user_id, directions=directions, expires_at=row.expires_at)

    def require(
        self,
        db: Session,
        user_id: int,
        challenge_id: str | None,
        challenge_images: Sequence[bytes],
        *,
        required: bool,
    ) -> Challenge | None:
        """Consume el reto obligatorio de prueba de vida (None si la política no lo exige).

        422 LIVENESS_REQUIRED si falta el reto o una captura por giro; 422 CHALLENGE_INVALID si
        expiró, ya se usó o pertenece a otro usuario.
        """
        if not required:
            return None
        if not challenge_id or not challenge_images:
            raise face_rejection("LIVENESS_REQUIRED")
        challenge = self.consume(db, challenge_id, user_id)
        if challenge is None:
            raise face_rejection("CHALLENGE_INVALID")
        if len(challenge_images) != len(challenge.directions):
            raise face_rejection("LIVENESS_REQUIRED")  # falta (o sobra) la captura de algún giro
        return challenge


challenge_store = ChallengeStore()
