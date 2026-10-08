from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, false, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.db_schemas import AUTH, BIOMETRICS, CATALOG, TENANCY


class FaceChallenge(Base):
    """Reto de prueba de vida de uso único.

    Se guarda en la BD (no en memoria) para que funcione con varios procesos/instancias de la
    API: el consumo es atómico (`DELETE ... RETURNING`), así un reto nunca se usa dos veces.

    Su dueño es una CUENTA (`user_id`: el empleado, el validador o la empresa que opera la cámara) o, en la API pública
    de verificación (SDK móviles, migración 0084), un DISPOSITIVO de una llave de la empresa (`api_key_id` + la huella
    SHA-256 de la llave pública del dispositivo, `device_hash`): uno vigente por cuenta o por dispositivo; nunca los
    dos a la vez.
    """

    __tablename__ = "face_challenges"
    __table_args__ = (
        # Exactamente un dueño: la cuenta, o la llave de la API con su dispositivo (los dos juntos).
        CheckConstraint(
            "(user_id IS NULL) <> (api_key_id IS NULL) AND (api_key_id IS NULL) = (device_hash IS NULL)", name="owner"
        ),
        # El reto vigente de un dispositivo de la API (lo reemplaza el siguiente) y la FK a la llave (empieza por ella).
        Index(
            "ix_face_challenges_api_device",
            "api_key_id",
            "device_hash",
            postgresql_where=text("api_key_id IS NOT NULL"),
            sqlite_where=text("api_key_id IS NOT NULL"),
        ),
        {"schema": BIOMETRICS},
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey(f"{AUTH}.users.id", ondelete="CASCADE"), index=True)
    #: La llave de la API de integración que pidió el reto (SDK móviles); None si lo pidió una cuenta.
    api_key_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(f"{TENANCY}.company_api_keys.id", ondelete="CASCADE")
    )
    #: Huella SHA-256 (hex) de la llave pública del dispositivo que pidió el reto por la API; None si es de una cuenta.
    device_hash: Mapped[str | None] = mapped_column(String(64))
    #: Primer movimiento que se pide (catalog.liveness_actions).
    direction: Mapped[str] = mapped_column(String(20), ForeignKey(f"{CATALOG}.liveness_actions.code"), nullable=False)
    #: Segundo y tercer movimiento, si la empresa exige más pasos (verification_policy.liveness_steps).
    second_direction: Mapped[str | None] = mapped_column(String(20), ForeignKey(f"{CATALOG}.liveness_actions.code"))
    third_direction: Mapped[str | None] = mapped_column(String(20), ForeignKey(f"{CATALOG}.liveness_actions.code"))
    #: Cuarto movimiento: solo el reto del registro facial, que pide SIEMPRE los cuatro (derecha, izquierda, arriba,
    #: abajo; decisión del dueño, 2026-10-07, migración 0082). Nulo en los retos de 1 a 3 pasos de la verificación.
    fourth_direction: Mapped[str | None] = mapped_column(String(20), ForeignKey(f"{CATALOG}.liveness_actions.code"))
    #: Colores del destello en orden (códigos de photometry.FLASH_PALETTE separados por coma); None sin destello.
    flash_colors: Mapped[str | None] = mapped_column(String(80))
    #: Cuándo se emitió: el tiempo humano mínimo de respuesta se mide desde aquí.
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    #: Reto de "un paso más" (motor de riesgo, riesgo medio): el máximo de movimientos y el destello OBLIGATORIO
    #: para este intento aunque la empresa solo lo mida. Superarlo cumple el paso extra.
    step_up: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    #: La empresa estaba reforzada por ataques al emitirlo (señal COMPANY_UNDER_ATTACK sin otra consulta).
    reinforced: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    #: Destello dictado por el servidor (antifraude 2a): el reto NO entregó sus colores; se revelan uno por uno por el
    #: canal en vivo (`flash_pacing`) y la respuesta trae el comprobante. `flash_colors` queda para el respaldo sin
    #: canal.
    flash_paced: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
