from collections.abc import Iterable
from datetime import datetime

from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from app.core.row_security import crossing_tenants
from app.models import CaptureFingerprint


class CaptureFingerprintRepository:
    """Huellas SHA-256 de las capturas recibidas (para rechazar su reenvío)."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def claim(self, digests: Iterable[str], company_id: int, now: datetime, since: datetime) -> bool:
        """Registra las huellas de una toma en UNA sentencia atómica y dice si TODAS eran nuevas.

        Una huella vencida que seguía en la tabla (aún sin depurar) se renueva; una vigente no se toca
        y no vuelve en RETURNING: si falta alguna, esa captura ya se había recibido (reenvío). Dos
        reenvíos simultáneos no pueden pasar los dos, a diferencia de "consultar y luego guardar".
        """
        unique = sorted(set(digests))
        dialect = postgresql if self.db.get_bind().dialect.name == "postgresql" else sqlite
        stmt = dialect.insert(CaptureFingerprint).values(
            [{"digest": d, "company_id": company_id, "created_at": now} for d in unique]
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[CaptureFingerprint.digest],
            set_={"company_id": stmt.excluded.company_id, "created_at": stmt.excluded.created_at},
            where=CaptureFingerprint.created_at < since,
        ).returning(CaptureFingerprint.digest)
        # Un reenvío se detecta en TODA la plataforma (una captura robada en una empresa no sirve en otra) y una
        # huella vencida de otra empresa se renueva para esta: la única escritura que cruza empresas a propósito
        # en una captura facial (solo huellas SHA-256: nunca datos de la otra empresa).
        with crossing_tenants(self.db):
            claimed = self.db.scalars(stmt).all()
        return len(claimed) == len(unique)
