from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.db_schemas import AUTH, BIOMETRICS, CATALOG


class FaceChallenge(Base):
    """Reto de prueba de vida de uso único.

    Se guarda en la BD (no en memoria) para que funcione con varios procesos/instancias de la
    API: el consumo es atómico (`DELETE ... RETURNING`), así un reto nunca se usa dos veces.
    """

    __tablename__ = "face_challenges"
    __table_args__ = ({"schema": BIOMETRICS},)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey(f"{AUTH}.users.id", ondelete="CASCADE"), index=True, nullable=False)
    #: Giro que se pide (catalog.liveness_actions).
    direction: Mapped[str] = mapped_column(String(20), ForeignKey(f"{CATALOG}.liveness_actions.code"), nullable=False)
    #: Segundo giro, si la empresa exige dos (verification_policy.liveness_steps = 2).
    second_direction: Mapped[str | None] = mapped_column(String(20), ForeignKey(f"{CATALOG}.liveness_actions.code"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
