"""Retos de prueba de vida (liveness) emitidos por el servidor.

Flujo:
1. El EMPLOYEE solicita un reto: el servidor elige al azar TURN_LEFT o TURN_RIGHT.
2. El cliente captura frames frontales y un frame con la cabeza girada.
3. Al verificar, el reto se consume (uso único), debe pertenecer al usuario y no
   haber expirado. Un video o foto preparado de antemano no conoce la dirección.

Almacenamiento en PostgreSQL (tabla `face_challenges`): compartido entre todos los procesos
de la API, por lo que se puede escalar horizontalmente sin Redis.
"""

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, or_
from sqlalchemy.orm import Session

from app.core.clock import as_utc
from app.core.config import settings
from app.core.exceptions import UnprocessableError
from app.facial_recognition import TurnDirection
from app.models import FaceChallenge


@dataclass(frozen=True)
class Challenge:
    id: str
    user_id: int
    direction: TurnDirection
    expires_at: datetime


class ChallengeStore:
    def issue(self, db: Session, user_id: int) -> Challenge:
        now = datetime.now(UTC)
        challenge = Challenge(
            id=secrets.token_urlsafe(24),
            user_id=user_id,
            direction=TurnDirection(secrets.choice([d.value for d in TurnDirection])),
            expires_at=now + timedelta(seconds=settings.FACE_CHALLENGE_TTL_SECONDS),
        )
        # Un solo reto vigente por usuario + limpieza de expirados (índice en expires_at).
        db.execute(delete(FaceChallenge).where(or_(FaceChallenge.user_id == user_id, FaceChallenge.expires_at <= now)))
        db.add(
            FaceChallenge(
                id=challenge.id,
                user_id=user_id,
                direction=challenge.direction.value,
                expires_at=challenge.expires_at,
            )
        )
        db.commit()
        return challenge

    def consume(self, db: Session, challenge_id: str, user_id: int) -> Challenge | None:
        """Devuelve el reto si es válido para el usuario; en cualquier caso lo elimina (atómico)."""
        row = db.execute(
            delete(FaceChallenge)
            .where(FaceChallenge.id == challenge_id)
            .returning(FaceChallenge.user_id, FaceChallenge.direction, FaceChallenge.expires_at)
        ).first()
        db.commit()
        if row is None or row.user_id != user_id or as_utc(row.expires_at) <= datetime.now(UTC):
            return None
        return Challenge(
            id=challenge_id, user_id=row.user_id, direction=TurnDirection(row.direction), expires_at=row.expires_at
        )

    def require(
        self,
        db: Session,
        user_id: int,
        challenge_id: str | None,
        challenge_image: bytes | None,
        *,
        required: bool,
    ) -> Challenge | None:
        """Consume el reto obligatorio de prueba de vida (None si la política no lo exige).

        422 LIVENESS_REQUIRED si falta el reto o su captura; 422 CHALLENGE_INVALID si expiró,
        ya se usó o pertenece a otro usuario.
        """
        if not required:
            return None
        if not challenge_id or challenge_image is None:
            raise UnprocessableError("Se requiere completar la prueba de vida", code="LIVENESS_REQUIRED")
        challenge = self.consume(db, challenge_id, user_id)
        if challenge is None:
            raise UnprocessableError(
                "El reto de verificación expiró o no es válido. Inténtalo de nuevo", code="CHALLENGE_INVALID"
            )
        return challenge


challenge_store = ChallengeStore()
