from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models import DeviceStatus, ValidatorDevice
from app.repositories.aggregates import paginate


class ValidatorDeviceRepository:
    """Dispositivos de los validadores de UNA empresa."""

    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id

    def find(self, validator_id: int, key_hash: str) -> ValidatorDevice | None:
        return self.db.scalar(
            select(ValidatorDevice).where(
                ValidatorDevice.company_id == self.company_id,
                ValidatorDevice.validator_id == validator_id,
                ValidatorDevice.key_hash == key_hash,
            )
        )

    def get(self, validator_id: int, device_id: int) -> ValidatorDevice | None:
        device = self.db.get(ValidatorDevice, device_id)
        if device is None or device.company_id != self.company_id or device.validator_id != validator_id:
            return None
        return device

    def add(self, device: ValidatorDevice) -> ValidatorDevice:
        device.company_id = self.company_id
        self.db.add(device)
        self.db.flush()
        return device

    def page(self, validator_id: int, *, offset: int, limit: int) -> tuple[list[ValidatorDevice], int]:
        """Los por autorizar primero; después, el más reciente."""
        stmt = select(ValidatorDevice).where(
            ValidatorDevice.company_id == self.company_id, ValidatorDevice.validator_id == validator_id
        )
        pending_first = case((ValidatorDevice.status == DeviceStatus.PENDING, 0), else_=1)
        order = (pending_first, ValidatorDevice.created_at.desc(), ValidatorDevice.id.desc())
        return paginate(self.db, stmt, order, offset=offset, limit=limit)

    def counts(self, validator_ids: list[int]) -> dict[int, dict[str, int]]:
        """{validador: {estado: dispositivos}} para toda una página de validadores (sin N+1)."""
        if not validator_ids:
            return {}
        rows = self.db.execute(
            select(ValidatorDevice.validator_id, ValidatorDevice.status, func.count())
            .where(ValidatorDevice.company_id == self.company_id, ValidatorDevice.validator_id.in_(validator_ids))
            .group_by(ValidatorDevice.validator_id, ValidatorDevice.status)
        ).all()
        result: dict[int, dict[str, int]] = {}
        for validator_id, status, count in rows:
            result.setdefault(int(validator_id), {})[str(status)] = int(count)
        return result
