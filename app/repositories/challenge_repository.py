from datetime import datetime

from sqlalchemy import Row, delete
from sqlalchemy.orm import Session

from app.models import FaceChallenge


class FaceChallengeRepository:
    """Retos de prueba de vida: uno vigente por usuario y de un solo uso."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def replace_for_user(self, challenge: FaceChallenge) -> None:
        """El reto nuevo sustituye a los anteriores del usuario (los vencidos de los demás los depura
        el mantenimiento: aquí nunca se recorre la tabla completa)."""
        self.db.execute(delete(FaceChallenge).where(FaceChallenge.user_id == challenge.user_id))
        self.db.add(challenge)

    def take(
        self, challenge_id: str
    ) -> Row[int, str, str | None, str | None, str | None, datetime, datetime, bool, bool, bool] | None:
        """Lo elimina y devuelve sus datos en una sola sentencia (un reto no se puede usar dos veces)."""
        return self.db.execute(
            delete(FaceChallenge)
            .where(FaceChallenge.id == challenge_id)
            .returning(
                FaceChallenge.user_id,
                FaceChallenge.direction,
                FaceChallenge.second_direction,
                FaceChallenge.third_direction,
                FaceChallenge.flash_colors,
                FaceChallenge.issued_at,
                FaceChallenge.expires_at,
                FaceChallenge.step_up,
                FaceChallenge.reinforced,
                FaceChallenge.flash_paced,
            )
        ).first()
