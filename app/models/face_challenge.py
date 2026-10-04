from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
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
    #: Primer movimiento que se pide (catalog.liveness_actions).
    direction: Mapped[str] = mapped_column(String(20), ForeignKey(f"{CATALOG}.liveness_actions.code"), nullable=False)
    #: Segundo y tercer movimiento, si la empresa exige más pasos (verification_policy.liveness_steps).
    second_direction: Mapped[str | None] = mapped_column(String(20), ForeignKey(f"{CATALOG}.liveness_actions.code"))
    third_direction: Mapped[str | None] = mapped_column(String(20), ForeignKey(f"{CATALOG}.liveness_actions.code"))
    #: Colores del destello en orden (códigos de photometry.FLASH_PALETTE separados por coma); None sin destello.
    flash_colors: Mapped[str | None] = mapped_column(String(80))
    #: Cuándo se emitió: el tiempo humano mínimo de respuesta se mide desde aquí.
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
