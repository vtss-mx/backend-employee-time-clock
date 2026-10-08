from datetime import datetime

from sqlalchemy import ColumnElement, Row, delete
from sqlalchemy.orm import Session

from app.models import FaceChallenge

#: Lo que devuelve consumir un reto: dueño (cuenta, o llave + dispositivo), movimientos, colores, vigencia y marcas.
type TakenChallenge = Row[
    int | None,
    int | None,
    str | None,
    str,
    str | None,
    str | None,
    str | None,
    str | None,
    datetime,
    datetime,
    bool,
    bool,
    bool,
]


class FaceChallengeRepository:
    """Retos de prueba de vida: uno vigente por dueño (una cuenta, o un dispositivo de una llave de la API) y de un solo
    uso."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def replace_for_owner(self, challenge: FaceChallenge) -> None:
        """El reto nuevo sustituye a los anteriores de su dueño: la cuenta (`ix_face_challenges_user_id`) o el
        dispositivo de la llave (`ix_face_challenges_api_device`). Los vencidos de los demás los depura el
        mantenimiento: aquí nunca se recorre la tabla completa."""
        owner: ColumnElement[bool]
        if challenge.user_id is not None:
            owner = FaceChallenge.user_id == challenge.user_id
        else:
            owner = (FaceChallenge.api_key_id == challenge.api_key_id) & (
                FaceChallenge.device_hash == challenge.device_hash
            )
        self.db.execute(delete(FaceChallenge).where(owner))
        self.db.add(challenge)

    def take(self, challenge_id: str) -> TakenChallenge | None:
        """Lo elimina y devuelve sus datos en una sola sentencia (un reto no se puede usar dos veces)."""
        return self.db.execute(
            delete(FaceChallenge)
            .where(FaceChallenge.id == challenge_id)
            .returning(
                FaceChallenge.user_id,
                FaceChallenge.api_key_id,
                FaceChallenge.device_hash,
                FaceChallenge.direction,
                FaceChallenge.second_direction,
                FaceChallenge.third_direction,
                FaceChallenge.fourth_direction,
                FaceChallenge.flash_colors,
                FaceChallenge.issued_at,
                FaceChallenge.expires_at,
                FaceChallenge.step_up,
                FaceChallenge.reinforced,
                FaceChallenge.flash_paced,
            )
        ).first()
